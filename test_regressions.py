import csv
import json
import threading
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import fitz
import pytest
from PIL import Image, ImageDraw

import asa_dashboard as dashboard
import asa_invoice_processor as processor


def fixture(tmp_path, amount=1000):
    po = tmp_path / "pos.csv"
    po.write_text(f"po_number,vendor,po_amount,invoiced_to_date\nPO-1,Acme Supplies,{amount},0\n")
    history = tmp_path / "history.json"
    history.write_text("[]")
    return po, history


def invoice(number="INV-1", total=1000, vendor="Acme Supplies", po="PO-1"):
    return {"invoice_number": number, "po_number": po, "vendor": vendor, "date": "2026-11-10",
            "total": total, "subtotal": total, "tax": 0, "discount": None,
            "gstin": None, "details": {}, "line_items": []}


def gstin(prefix="29ABCDE1234F1Z"):
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    factor, total = 2, 0
    for char in reversed(prefix):
        product = chars.index(char) * factor
        total += product // 36 + product % 36
        factor = 1 if factor == 2 else 2
    return prefix + chars[(36 - total % 36) % 36]


def test_gstin_checksum_and_vendor_master(tmp_path):
    po, history = fixture(tmp_path)
    good = gstin()
    assert processor.valid_gstin(good)
    assert not processor.valid_gstin(good[:-1] + ("0" if good[-1] != "0" else "1"))
    (tmp_path / "vendor_master.csv").write_text(f"vendor,gstin\nAcme Supplies,{good}\n")
    inv = invoice(); inv["gstin"] = good
    assert processor.decide(inv, po, history).status == "APPROVED"
    inv["gstin"] = gstin("27ABCDE1234F1Z")
    assert processor.decide(inv, po, history).status == "REJECTED"


def test_document_title_precedes_cross_references():
    assert processor.classify_document("TAX INVOICE\nPurchase Order: PO-1001") == "TAX_INVOICE"
    assert processor.classify_document("PURCHASE ORDER\nPlease send a tax invoice after delivery") == "PURCHASE_ORDER"


def test_missing_or_ambiguous_vendor_pending(tmp_path):
    po, history = fixture(tmp_path)
    inv = invoice(vendor="Unknown")
    assert processor.decide(inv, po, history).status == "PENDING_REVIEW"
    inv = invoice(); inv["details"] = {"identity_ambiguous": True}
    assert processor.decide(inv, po, history).status == "PENDING_REVIEW"


def test_duplicate_vendor_and_invoice_normalization(tmp_path):
    po, history = fixture(tmp_path)
    history.write_text(json.dumps([{"vendor": "ACME  Supplies", "invoice_number": "inv-1", "status": "PENDING_REVIEW"}]))
    result = processor.decide(invoice(), po, history)
    assert result.status == "PENDING_REVIEW"
    assert next(c for c in result.checks if c["check"] == "duplicate_detection")["passed"]
    assert not next(c for c in result.checks if c["check"] == "duplicate_submission")["passed"]
    history.write_text(json.dumps([{"vendor": "ACME  Supplies", "invoice_number": "inv-1", "status": "APPROVED"}]))
    assert processor.decide(invoice(), po, history).status == "REJECTED"
    other = invoice(vendor="Other Vendor")
    assert next(c for c in processor.decide(other, po, history).checks if c["check"] == "duplicate_detection")["passed"]


def test_partial_po_cumulative_and_overage(tmp_path):
    po, history = fixture(tmp_path)
    first = invoice("A", 400)
    assert processor.decide(first, po, history).status == "APPROVED"
    history.write_text(json.dumps([{"vendor":"Acme Supplies", "invoice_number":"A", "po_number":"PO-1", "total":400, "status":"APPROVED"}]))
    assert processor.decide(invoice("B", 600), po, history).status == "APPROVED"
    assert processor.decide(invoice("B", 601), po, history).status == "REJECTED"
    assert processor.decide(invoice("C", 1001), po, history).status == "REJECTED"


def test_ocr_arithmetic_discrepancy_pending(tmp_path):
    po, history = fixture(tmp_path)
    inv = invoice(); inv.update(subtotal=800, tax=100)
    assert processor.decide(inv, po, history, method="ocr").status == "PENDING_REVIEW"
    assert processor.decide(inv, po, history, method="text").status == "REJECTED"
    assert processor.decide(invoice(), po, history, method="ocr").status == "PENDING_REVIEW"


def test_gst_component_reconciliation(tmp_path):
    po, history = fixture(tmp_path)
    inv = invoice(); inv["tax"] = 180; inv["subtotal"] = 820
    inv["details"] = {"tax_breakdown":{"CGST":{"rate":9,"amount":90},"SGST":{"rate":9,"amount":90}}}
    assert next(c for c in processor.decide(inv,po,history).checks if c["check"] == "gst_components")["passed"]
    inv["details"]["tax_breakdown"]["SGST"]["amount"] = 80
    assert processor.decide(inv,po,history,method="text").status == "REJECTED"
    assert processor.decide(inv,po,history,method="ocr").status == "PENDING_REVIEW"


def test_run_monitor_reports_check_outcomes(tmp_path):
    po, history = fixture(tmp_path)
    happy = dashboard.decision_stages(processor.decide(invoice(), po, history))
    assert happy["PO matching"] == "Matched" and happy["Financial validation"] == "Passed"
    missing = dashboard.decision_stages(processor.decide(invoice(po="MISSING"), po, history))
    assert missing["PO matching"] == "Needs review"
    over = dashboard.decision_stages(processor.decide(invoice(total=1001), po, history))
    assert over["Financial validation"] == "Flagged"


def test_machine_readable_pdf_skips_ocr():
    doc = fitz.open(); page = doc.new_page()
    page.insert_text((72,72), "Vendor: Acme Supplies Invoice Number: INV-1 PO Number: PO-1 Total: 1000")
    with patch.object(processor, "_ocr_image", side_effect=AssertionError("OCR called")):
        text, method, pages = processor.read_document(doc.tobytes(), ".pdf")
    assert "Acme Supplies" in text and method == "pdf_text" and pages == 1


def test_scanned_pdf_renders_then_ocr():
    image = Image.new("RGB", (700, 120), "white")
    ImageDraw.Draw(image).text((10, 10), "Invoice Number INV-1 Vendor Acme Supplies Total 1000", fill="black")
    buf = BytesIO(); image.save(buf, format="PNG")
    doc = fitz.open(); page = doc.new_page(width=700,height=120)
    page.insert_image(page.rect, stream=buf.getvalue())
    with patch.object(processor, "_ocr_image", return_value="Invoice Number INV-1 Vendor Acme Supplies Total 1000") as ocr:
        _, method, pages = processor.read_document(doc.tobytes(), ".pdf")
    assert method == "pdf+ocr" and pages == 1 and ocr.call_count == 1


def test_image_pipeline_and_ocr_failure():
    with patch.object(processor, "_ocr_image", return_value="Invoice Number INV-1 Vendor Acme Supplies Total 1000") as ocr:
        assert processor.read_document(b"image", ".jpg")[1] == "ocr"
        assert processor.read_document(b"image", ".png")[1] == "ocr"
        assert ocr.call_count == 2
    with patch.object(processor, "_ocr_image", side_effect=RuntimeError("OCR failed")):
        with pytest.raises(RuntimeError, match="OCR failed"):
            processor.read_document(b"image", ".jpg")


def test_header_style_invoice_uses_following_value_row():
    text = """TAX INVOICE
ExpoHub
Date of Issue
Invoice Number
Amount Due (INR)
22/04/2020
Rahul Jignesh
873100.
Rs.89,600.00
Due Date
22/05/2020
Subtotal
89,600.00
Tax
0.00
Total
89,600.00
"""
    extracted = processor.extract_invoice(text)
    assert extracted["invoice_number"] == "873100"
    assert extracted["date"] == "22/04/2020"
    assert extracted["total"] == 89600.0


def test_bad_invoice_heading_is_not_an_invoice_number():
    extracted = processor.extract_invoice("TAX INVOICE\nInvoice No\nDated\n6-May-2021\n")
    assert extracted["invoice_number"] is None


def test_delivery_challan_uses_customer_order_as_document_reference():
    extracted = processor.extract_invoice("""DELIVERY CHALLAN
Cust Order No
:13542319324
Date: 10-04-2024
""")
    assert extracted["document_type"] == "DELIVERY_CHALLAN"
    assert extracted["details"]["customer_order_number"] == "13542319324"
    assert extracted["invoice_number"] == "13542319324"


def test_split_invoice_number_and_dated_header():
    extracted = processor.extract_invoice("""TAX INVOICE
Invoice No.
Dated
Billers Name: Example Supplier
12
6-May-2021
""")
    assert extracted["invoice_number"] == "12"
    assert extracted["date"] == "6-May-2021"


def test_selective_ocr_preprocessing_only_targets_low_contrast_images():
    white = Image.new("RGB", (180, 80), "white")
    high = BytesIO(); white.save(high, format="PNG")
    assert processor._enhanced_ocr_candidate(high.getvalue()) is None
    low = Image.new("RGB", (180, 80), (132, 132, 132))
    drawing = ImageDraw.Draw(low)
    drawing.rectangle((20, 20, 160, 60), fill=(145, 145, 145))
    drawing.text((28, 30), "Invoice 100", fill=(156, 156, 156))
    encoded = BytesIO(); low.save(encoded, format="PNG")
    assert processor._enhanced_ocr_candidate(encoded.getvalue()) is not None


def test_ocr_spaced_vendor_label_has_direct_evidence():
    inv = processor.extract_invoice("FICTIONAL DEMO DOCUMENT\nTAX INVOICE\nVe ndor. Acme Supplies\nInvoice Number: INV-1")
    assert inv["vendor"] == "Acme Supplies"
    assert inv["field_metadata"]["vendor"]["reliability"] == "DIRECT_LABEL"


def test_extended_extraction_schema_uses_document_labels_only():
    inv = processor.extract_invoice("""TAX INVOICE
Invoice Number: INV-9
PO Number: PO-1
PO Date: 2026-11-01
Invoice Date: 2026-11-02
Taxable Value: 100.00
Description of Goods
Cable HSN: 8544 Qty: 2 Rate: 50.00
Grand Total: 118.00""")
    assert inv["details"]["po_date"] == "2026-11-01"
    assert inv["details"]["taxable_amount"] == 100.0
    assert inv["line_items"][0]["hsn_sac"] == "8544"
    assert inv["line_items"][0]["quantity"] == 2.0
    assert inv["line_items"][0]["unit_price"] == 50.0


def test_purchase_order_total_is_not_invoice_total():
    text = """Hindalco Industries Limited
PURCHASE ORDER (Domestic)
Vendor: VIGIL SAFETY TECHNOLOGIES
PO. Number: 13542310381
Item Total Value
6,080.00
Total Purchase Order Value
6,080.00
"""
    inv = processor.extract_invoice(text)
    assert inv["document_type"] == "PURCHASE_ORDER"
    assert inv["po_total"] == 6080.0
    assert inv["total"] is None
    assert inv["po_number"] == "13542310381"


def test_seller_and_buyer_gstin_separate():
    seller, buyer = gstin(), gstin("27ABCDE1234F1Z")
    text = f"Vendor: Acme Supplies\nGSTIN: {seller}\nBill To GSTIN: {buyer}\nInvoice Number: INV-1\nPO Number: PO-1\nTotal: 1000"
    inv = processor.extract_invoice(text)
    assert inv["gstin"] == seller and inv["details"]["buyer_gstin"] == buyer


def test_dataset_evaluation_labels_and_missing():
    pred = invoice(); rows, accuracy = dashboard.evaluate_dataset(pred, {"invoice_no":"INV-1", "amount":"1000.00", "cgst":"90", "gstin_no":"unknown-role"})
    by_field = {r[0]:r[3] for r in rows}
    assert by_field["invoice_number"] == "Exact match"
    assert by_field["total"] == "Exact match"
    assert by_field["cgst"] == "Extraction missing"
    assert by_field["date"] == "Label unavailable"
    assert by_field["gstin (role unspecified)"] == "Ambiguous label"
    assert accuracy == pytest.approx(2/3)


def test_dataset_image_uses_original_ocr_not_labels():
    row = dashboard.dataset_rows()[1]["row"]
    raw, error = dashboard.dataset_image_bytes(1, row)
    assert raw and error is None
    text, method, _ = processor.read_document(raw, ".jpg")
    assert method == "ocr" and "VST/783" in text


def test_existing_history_and_verification_preserved(tmp_path, monkeypatch):
    # run_history.json is gitignored local state, so build a legacy fixture
    # rather than depending on whichever file happens to exist on this machine.
    legacy = [{"time": f"2026-11-0{i}T10:00:00", "scenario": f"legacy_{i}.txt", "invoice_number": f"OLD-{i}",
               "status": "APPROVED" if i % 2 else "PENDING_REVIEW"} for i in range(1, 7)]
    runs_path = tmp_path / "run_history.json"; runs_path.write_text(json.dumps(legacy))
    monkeypatch.setattr(dashboard, "RUNS", runs_path)
    monkeypatch.setattr(dashboard, "VERIFIED", tmp_path / "manual_verification.json")
    assert len(dashboard.load_runs()) >= 6
    assert isinstance(dashboard.load_verified(), dict)
    markup = dashboard.page()
    assert "payment approval is separate" in markup
    assert markup.count("value='legacy-") >= 4


def test_legacy_history_vendor_is_recovered_without_rewriting(tmp_path):
    po, history = fixture(tmp_path)
    samples = tmp_path / "samples"; samples.mkdir()
    (samples / "old.txt").write_text("Vendor: Acme Supplies\nInvoice Number: INV-1\nPO Number: PO-1\nInvoice Date: 2026-11-10\nTotal: 1000")
    original = [{"scenario":"old.txt", "invoice_number":"INV-1", "status":"APPROVED"}]
    history.write_text(json.dumps(original))
    assert processor.decide(invoice(), po, history).status == "REJECTED"
    assert json.loads(history.read_text()) == original


def test_dashboard_repeated_upload_history_and_manual_check(tmp_path, monkeypatch):
    po, _ = fixture(tmp_path)
    monkeypatch.setattr(dashboard, "RUNS", tmp_path / "run_history.json")
    monkeypatch.setattr(dashboard, "VERIFIED", tmp_path / "manual_verification.json")
    monkeypatch.setattr(dashboard, "SAMPLES", tmp_path)
    with patch.object(dashboard, "read_document", return_value=(
        "Vendor: Acme Supplies\nInvoice Number: INV-1\nPO Number: PO-1\nInvoice Date: 2026-11-10\nTotal: 1000", "ocr", 1)):
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            boundary = "invoice-test"
            body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"invoice_file\"; filename=\"bill.jpg\"\r\n"
                    "Content-Type: image/jpeg\r\n\r\nimage-content\r\n"
                    f"--{boundary}--\r\n").encode()
            for _ in range(2):
                request = urllib.request.Request(url, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
                assert urllib.request.urlopen(request).status == 200
            rows = json.loads(dashboard.RUNS.read_text())
            assert len(rows) == 2 and rows[0]["status"] == "PENDING_REVIEW" and rows[1]["status"] == "PENDING_REVIEW"
            assert rows[0]["vendor"] == rows[1]["vendor"] == "Acme Supplies"
            invalid = urllib.parse.urlencode({"review_run":rows[0]["run_id"], "review_outcome":"VERIFIED"}).encode()
            urllib.request.urlopen(urllib.request.Request(url, invalid))
            assert not dashboard.load_verified().get(rows[0]["run_id"])
            verification = urllib.parse.urlencode({"review_run":rows[0]["run_id"], "reviewer":"Test Reviewer", "review_outcome":"NEEDS_CORRECTION", "review_note":"Amount requires checking", "fields_confirmed":"1"}).encode()
            urllib.request.urlopen(urllib.request.Request(url, verification))
            assert dashboard.load_verified()[rows[0]["run_id"]]["reviewer"] == "Test Reviewer"
            assert dashboard.load_verified()[rows[0]["run_id"]]["outcome"] == "NEEDS_CORRECTION"
            assert not any("reviewer" in r or "authorised" in r for r in rows)
        finally:
            server.shutdown(); server.server_close(); worker.join()


def test_dataset_fetch_records_history_without_polluting_decisions(tmp_path, monkeypatch):
    po, history = fixture(tmp_path)
    monkeypatch.setattr(dashboard, "RUNS", history)
    monkeypatch.setattr(dashboard, "VERIFIED", tmp_path / "verification.json")
    monkeypatch.setattr(dashboard, "dataset_rows", lambda: [{"row": {"ground_truth": json.dumps({"gt_parse":{"header":{"invoice_no":"LABEL-ONLY","amount":"9999"}}})}}])
    monkeypatch.setattr(dashboard, "dataset_image_bytes", lambda index, row: (b"original-image", None))
    extracted = "Vendor: Acme Supplies\nInvoice Number: OCR-1\nPO Number: PO-1\nInvoice Date: 2026-11-10\nTotal: 1000"
    with patch.object(dashboard, "read_document", return_value=(extracted, "ocr", 1)):
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            body = urllib.parse.urlencode({"dataset_index":"0"}).encode()
            for _ in range(2):
                page = urllib.request.urlopen(urllib.request.Request(url, body)).read().decode()
                assert "Field-level exact-match accuracy" in page
                assert "PENDING_REVIEW" in page
                assert "Seller GSTIN" in page
                assert "Field accuracy" in page
            rows = json.loads(history.read_text())
            assert len(rows) == 2
            assert all(r["event_type"] == "dataset_evaluation" and r["invoice_number"] == "OCR-1" for r in rows)
            assert all(r["status"] == "PENDING_REVIEW" for r in rows)
            assert all(r["reasons"] for r in rows)
            assert processor.decide(invoice("OCR-1"), po, history).status == "APPROVED"
        finally:
            server.shutdown(); server.server_close(); worker.join()


def test_old_evaluation_backfills_decision_without_labels(tmp_path, monkeypatch):
    fixture(tmp_path)
    images = tmp_path / "dataset_images"; images.mkdir()
    (images / "invoice_01.jpg").write_bytes(b"original-image")
    history = tmp_path / "run_history.json"
    original = {"run_id":"old", "time":"2026-11-12T10:00:00", "event_type":"dataset_evaluation",
                "source":"Indian GST invoice 01", "status":"EVALUATED", "field_accuracy":0.5}
    history.write_text(json.dumps([original]))
    monkeypatch.setattr(dashboard, "RUNS", history)
    monkeypatch.setattr(dashboard, "DATASET_IMAGES", images)
    monkeypatch.setattr(dashboard, "SAMPLES", tmp_path)
    text = "Vendor: Acme Supplies\nInvoice Number: OCR-1\nPO Number: PO-1\nInvoice Date: 2026-11-10\nTotal: 1000"
    with patch.object(dashboard, "read_document", return_value=(text,"ocr",1)):
        dashboard.reconcile_legacy_dataset_runs()
    updated = json.loads(history.read_text())[0]
    assert updated["status"] == "PENDING_REVIEW"
    assert updated["run_id"] == original["run_id"] and updated["field_accuracy"] == 0.5
    assert updated["invoice_number"] == "OCR-1"
    assert updated["reasons"]


def test_freight_discount_and_rounding_reconcile(tmp_path):
    po, history = fixture(tmp_path, amount=2000)
    inv = invoice(total=1080)
    inv.update(subtotal=1000, discount=100, tax=162)
    inv["details"] = {"shipping_amount": 18, "rounded_off": 0}
    result = processor.decide(inv, po, history)
    assert next(c for c in result.checks if c["check"] == "invoice_math")["passed"]
    inv["total"] = 1100
    result = processor.decide(inv, po, history)
    assert not next(c for c in result.checks if c["check"] == "invoice_math")["passed"]


def test_duplicate_marking_is_not_supplier():
    inv = processor.extract_invoice("Duplicalt\nAcme Supplies\nTAX INVOICE\nInvoice No: A-1\nSubtotal: 1000\nFreight Charges: 18\nTotal Tax: 180\nGrand Total: 1198")
    assert inv["vendor"] == "Acme Supplies"
    assert inv["details"]["shipping_amount"] == 18


def test_payment_change_requires_review_but_future_date_is_advisory(tmp_path):
    po, history = fixture(tmp_path)
    inv = invoice()
    inv["date"] = "2099-11-10"
    result = processor.decide(inv, po, history)
    assert result.status == "APPROVED"
    assert next(c for c in result.checks if c["check"] == "invoice_date_future")["outcome"] == "ADVISORY"
    inv["raw_text"] = "TAX INVOICE\nPlease use our new bank account for payment"
    result = processor.decide(inv, po, history)
    assert result.status == "PENDING_REVIEW"
    import workflow
    assert workflow.route(result.status, result.checks)["team"] == "Treasury & Fraud"


def test_freight_line_is_not_counted_twice():
    inv = processor.extract_invoice("Vendor: Acme\nTAX INVOICE\nFreight Charges: 18\nSubtotal: 1000\nTotal Tax: 180\nGrand Total: 1180")
    assert inv["details"]["shipping_amount"] is None

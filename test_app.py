import io
import json
import time
from concurrent.futures import ThreadPoolExecutor

import fitz
import pytest
from PIL import Image
from sqlalchemy import create_engine

import app as web
import storage
import asa_invoice_processor as processor
import ai_extraction


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "test.db").replace("\\", "/"), future=True)
    monkeypatch.setattr(storage, "engine", engine)
    storage.initialize()
    monkeypatch.setattr(web, "ROOT", tmp_path)
    monkeypatch.setattr(web, "AI_RUNTIME_CONFIG", {"endpoint": "", "model": "", "api_key": ""})
    (tmp_path / "samples").mkdir()
    (tmp_path / "samples" / "pos.csv").write_text(
        "po_number,vendor,po_amount,invoiced_to_date\nPO-1,Acme Supplies,1000,0\n", encoding="utf-8")
    with web.app.test_client() as c:
        c.get("/")
        yield c
    engine.dispose()


def invoice_text(number="INV-1", **changes):
    fields = {"heading":"TAX INVOICE", "vendor":"Acme Supplies", "number":number,
              "date":"2026-11-10", "po":"PO-1", "subtotal":"820.00", "tax":"180.00", "total":"1000.00"}
    fields.update(changes)
    return "\n".join(filter(None, [fields["heading"], f"Vendor: {fields['vendor']}" if fields["vendor"] else "",
                    f"Invoice Number: {fields['number']}" if fields["number"] else "",
                    f"Invoice Date: {fields['date']}" if fields["date"] else "",
                    f"PO Number: {fields['po']}" if fields["po"] else "",
                    f"Subtotal: {fields['subtotal']}" if fields["subtotal"] else "",
                    f"Tax: {fields['tax']}" if fields["tax"] else "",
                    f"Grand Total: {fields['total']}" if fields["total"] else ""]))


def pdf(text):
    doc = fitz.open(); page = doc.new_page()
    page.insert_text((60,60), text, fontsize=11)
    return doc.tobytes()


def submit(client, raw, name="bill.pdf"):
    client.get("/")
    with client.session_transaction() as sess: csrf = sess["csrf"]
    response = client.post("/api/runs", data={"invoice_file":(io.BytesIO(raw), name)},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 202, response.json
    rid = response.json["run_id"]
    for _ in range(100):
        row = client.get("/api/runs/" + rid).json
        if row["status"] != "RUNNING": return row
        time.sleep(.05)
    raise AssertionError("Run did not finish")


def test_clean_pdf_duplicate_and_persistence(client):
    raw = pdf(invoice_text())
    first = submit(client, raw)
    assert first["status"] == "APPROVED"
    assert first["decision"]["po_matching"]["status"] == "MATCHED"
    assert first["decision"]["financial_validation"]["status"] == "PASSED"
    assert first["method"] == "pdf_text"
    assert client.get("/api/runs/"+first["run_id"]+"/document").data == raw
    assert submit(client, raw)["status"] == "REJECTED"
    # Reconnect as a fresh process would; the DB remains the source of truth.
    storage.engine.dispose()
    assert client.get("/api/runs/" + first["run_id"]).json["status"] == "APPROVED"


@pytest.mark.parametrize("change,expected", [
    ({"number":""}, "PENDING_REVIEW"), ({"total":""}, "PENDING_REVIEW"),
    ({"date":"31/02/2026"}, "PENDING_REVIEW"), ({"vendor":"Wrong Vendor"}, "PENDING_REVIEW"),
    ({"po":""}, "PENDING_REVIEW"), ({"po":"UNKNOWN-44"}, "PENDING_REVIEW"),
    ({"total":"1001.00"}, "REJECTED"), ({"subtotal":"800.00"}, "REJECTED"),
    ({"heading":"DELIVERY CHALLAN"}, "PENDING_REVIEW"),
])
def test_decisions(client, change, expected):
    row = submit(client, pdf(invoice_text(**change)))
    assert row["status"] == expected
    assert row["reasons"]
    assert row["stages"]
    if change.get("heading") == "DELIVERY CHALLAN": assert row["document_type"] == "DELIVERY_CHALLAN"


def test_scanned_pdf_image_and_ocr_failure(client, monkeypatch):
    image = Image.new("RGB", (600,400), "white"); out=io.BytesIO(); image.save(out, format="PNG")
    scan = fitz.open(); p=scan.new_page(); p.insert_image(p.rect, stream=out.getvalue())
    monkeypatch.setattr(processor, "_ocr_image", lambda data: invoice_text(number="SCAN-1"))
    scanned = submit(client, scan.tobytes())
    assert scanned["method"] == "pdf+ocr" and scanned["status"] == "APPROVED"
    image_run = submit(client, out.getvalue(), "bill.png")
    assert image_run["method"] == "ocr"
    monkeypatch.setattr(processor, "_ocr_image", lambda data: (_ for _ in ()).throw(RuntimeError("OCR unavailable")))
    failure = submit(client, out.getvalue(), "broken.png")
    assert failure["status"] == "FAILED"
    assert any(s["status"] == "FAILED" for s in failure["stages"])


def test_invalid_uploads_and_corrupt_pdf(client):
    with client.session_transaction() as sess: csrf = sess["csrf"]
    bad=client.post("/api/runs",data={"invoice_file":(io.BytesIO(b"hello"),"x.jpg")},
                    headers={"X-CSRF-Token":csrf})
    assert bad.status_code == 400
    corrupt = submit(client, b"%PDF-1.7 garbage")
    assert corrupt["status"] == "FAILED"


def test_financial_components_and_partial_po(client):
    base = invoice_text(number="PART-1", subtotal="400.00", tax="0.00", total="400.00")
    assert submit(client,pdf(base))["status"] == "APPROVED"
    second = invoice_text(number="PART-2",subtotal="600.00",tax="0.00",total="600.00")
    assert submit(client,pdf(second))["status"] == "APPROVED"
    excess = invoice_text(number="PART-3",subtotal="1.00",tax="0.00",total="1.00")
    assert submit(client,pdf(excess))["status"] == "REJECTED"


def test_manual_review_preserves_automated_decision(client):
    row=submit(client,pdf(invoice_text(po="")))
    rid=row["run_id"]
    with client.session_transaction() as sess: csrf = sess["csrf"]
    response=client.post("/api/runs/"+rid+"/reviews",json={"reviewer":"Test Reviewer",
        "outcome":"NEEDS_CORRECTION","note":"PO visible on source","corrections":{"po_number":"PO-1"}},
        headers={"X-CSRF-Token":csrf})
    assert response.status_code == 200
    saved=client.get("/api/runs/"+rid).json
    assert saved["status"] == "PENDING_REVIEW"
    assert saved["reviews"][0]["reviewer"] == "Test Reviewer"
    assert json.loads(saved["reviews"][0]["corrections"])["po_number"] == "PO-1"


def test_ai_evidence_review_is_recorded_without_overriding_decision(client):
    approved = submit(client, pdf(invoice_text(number="AI-OK")))
    assert approved["status"] == "APPROVED"
    review = approved["ai_review"]
    assert review["recommendation"] == "AUTO_APPROVE"
    assert review["decision_preserved"] == "APPROVED"
    assert any(stage["stage"] == "AI evidence review" and stage["status"] == "COMPLETED"
               for stage in approved["stages"])
    pending = submit(client, pdf(invoice_text(number="AI-NEEDS-REVIEW", po="")))
    assert pending["status"] == "PENDING_REVIEW"
    assert pending["ai_review"]["recommendation"] == "ESCALATE_TO_HUMAN"


def test_ai_configuration_masks_key_and_is_advisory(client):
    with client.session_transaction() as sess: csrf = sess["csrf"]
    saved = client.post("/api/ai/config", json={
        "endpoint": "https://example.test/v1/chat/completions", "model": "review-model", "api_key": "top-secret"
    }, headers={"X-CSRF-Token": csrf})
    assert saved.status_code == 200
    assert "top-secret" not in saved.get_data(as_text=True)
    configured = client.get("/api/ai/config")
    assert configured.status_code == 200
    assert configured.json["endpoint"] == "https://example.test/v1/chat/completions"
    assert configured.json["model"] == "review-model"
    assert configured.json["api_key_configured"] is True
    assert "top-secret" not in configured.get_data(as_text=True)


def test_ai_ocr_reconciliation_requires_local_ocr_evidence(monkeypatch):
    invoice = {"invoice_number": None, "vendor": "Acme", "po_number": None, "date": None,
               "total": None, "subtotal": None, "tax": None, "discount": None, "details": {},
               "field_metadata": {"vendor": {"reliability": "CONTEXTUAL"}}}
    text = "Invoice No: A-1\nPO Number: PO-1\nInvoice Date: 10/11/2026\nGrand Total: 1,000.00"
    monkeypatch.setattr(ai_extraction, "_call_model", lambda *_: {"status": "COMPLETED", "model": "test", "fields": {
        "invoice_number": {"value": "A-1", "evidence": "Invoice No: A-1"},
        "po_number": {"value": "PO-1", "evidence": "PO Number: PO-1"},
        "date": {"value": "10/11/2026", "evidence": "Invoice Date: 10/11/2026"},
        "total": {"value": "1000.00", "evidence": "Grand Total: 1,000.00"},
        "vendor": {"value": "Invented", "evidence": "not in source"},
    }})
    merged, result = ai_extraction.reconcile_ocr(invoice, text, {"endpoint": "x", "model": "x", "api_key": "x"})
    assert result["accepted_fields"] == ["invoice_number", "po_number", "date", "total"]
    assert "vendor" in result["rejected_fields"]
    assert merged["total"] == 1000.0
    assert merged["field_metadata"]["total"]["reliability"] == "AI_CORROBORATED"


def test_concurrent_duplicate_decision(client):
    raw=pdf(invoice_text(number="RACE-1"))
    with ThreadPoolExecutor(max_workers=2) as executor:
        rows=list(executor.map(lambda _: submit(web.app.test_client(),raw),range(2)))
    assert sorted(r["status"] for r in rows)==["APPROVED","REJECTED"]


def test_classification_and_reference_separation():
    inv=processor.extract_invoice("DELIVERYCHALLAN\nCustomer Order No: CUST-789\nDelivery No: DEL-444\nPO No\nPO-1001\nInvoice No: INV-2")
    assert inv["document_type"] == "DELIVERY_CHALLAN"
    assert inv["po_number"] == "PO-1001"
    assert inv["details"]["customer_order_number"] == "CUST-789"
    assert inv["details"]["delivery_number"] == "DEL-444"


def test_pending_resubmission_is_not_business_duplicate(client):
    raw=pdf(invoice_text(number="PENDING-1",po=""))
    assert submit(client,raw)["status"] == "PENDING_REVIEW"
    second=submit(client,raw)
    assert second["status"] == "PENDING_REVIEW"
    assert next(c for c in second["decision"]["checks"] if c["check"] == "duplicate_submission")["outcome"] == "UNCERTAIN"
    assert next(c for c in second["decision"]["checks"] if c["check"] == "duplicate_detection")["passed"]


def test_verified_vendor_conflict_and_blocked_vendor(client):
    path=web.ROOT / "samples" / "vendor_master.csv"
    path.write_text("vendor,gstin,status\nAcme Supplies,,APPROVED\nOther Approved,,APPROVED\nBlocked Vendor,,BLOCKED\n")
    conflict=submit(client,pdf(invoice_text(number="CONFLICT-1",vendor="Other Approved")))
    assert conflict["status"] == "REJECTED"
    assert any(c["check"] == "vendor_match" and c["outcome"] == "CONFIRMED_VIOLATION" for c in conflict["decision"]["checks"])
    blocked=submit(client,pdf(invoice_text(number="BLOCKED-1",vendor="Blocked Vendor")))
    assert blocked["status"] == "REJECTED"
    assert any(c["check"] == "vendor_authorisation" for c in blocked["decision"]["checks"])


def test_ocr_overage_is_uncertain_not_rejected(client,monkeypatch):
    image=Image.new("RGB",(600,400),"white"); out=io.BytesIO(); image.save(out,format="PNG")
    monkeypatch.setattr(processor,"_ocr_image",lambda _:invoice_text(number="OCR-OVER",total="1200.00",subtotal="1020.00"))
    row=submit(client,out.getvalue(),"bill.png")
    assert row["status"] == "PENDING_REVIEW"
    assert next(c for c in row["decision"]["checks"] if c["check"] == "po_remaining")["outcome"] == "UNCERTAIN"


def test_clear_history_keeps_duplicate_and_po_ledger(client):
    first=submit(client,pdf(invoice_text(number="KEEP-1")))
    with client.session_transaction() as sess: csrf=sess["csrf"]
    response=client.post("/api/history/clear",headers={"X-CSRF-Token":csrf})
    assert response.status_code == 200 and response.json["archived"] == 1
    assert client.get("/api/runs").json == []
    assert client.get("/api/runs/"+first["run_id"]).json["status"] == "APPROVED"
    assert submit(client,pdf(invoice_text(number="KEEP-1")))["status"] == "REJECTED"


def test_missing_financial_data_is_blocked(client):
    row=submit(client,pdf(invoice_text(number="NO-TAX",tax="")))
    assert row["status"] == "PENDING_REVIEW"
    finance=row["decision"]["financial_validation"]
    assert finance["status"] == "BLOCKED_MISSING_DATA"
    assert any(c["financial_outcome"] == "BLOCKED" for c in finance["checks"])

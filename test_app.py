import io
import json
import os
import time
from urllib.error import HTTPError
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
    # TEST_DATABASE_URL (e.g. postgresql+psycopg://localhost/asa_test) runs the
    # same suite against PostgreSQL; each test starts from an empty schema.
    url = os.getenv("TEST_DATABASE_URL")
    engine = create_engine(url or "sqlite:///" + str(tmp_path / "test.db").replace("\\", "/"), future=True)
    if url: storage.meta.drop_all(engine)
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

def test_bulk_upload_search_and_csv_export(client):
    with client.session_transaction() as sess: csrf = sess['csrf']
    response = client.post('/api/runs/bulk', data={'invoice_files': [
        (io.BytesIO(pdf(invoice_text(number='BATCH-1'))), 'batch-one.pdf'),
        (io.BytesIO(pdf(invoice_text(number='BATCH-2', total='200.00', subtotal='20.00'))), 'batch-two.pdf')]},
        headers={'X-CSRF-Token': csrf})
    assert response.status_code == 202
    assert len(response.json['runs']) == 2
    for entry in response.json['runs']:
        for _ in range(100):
            run = client.get('/api/runs/' + entry['run_id']).json
            if run['status'] != 'RUNNING': break
            time.sleep(.05)
        assert run['status'] != 'RUNNING'
    found = client.get('/api/runs?q=BATCH-1').json
    assert len(found) == 1 and found[0]['invoice_number'] == 'BATCH-1'
    exported = client.get('/api/runs/export.csv')
    assert exported.status_code == 200
    assert b'BATCH-1' in exported.data and b'BATCH-2' in exported.data

    workbook = client.get('/api/runs/export.xlsx')
    assert workbook.status_code == 200
    assert workbook.data[:2] == b'PK'


def test_bulk_review_preserves_automated_decision(client):
    pending = submit(client, pdf(invoice_text(number='NEEDS-REVIEW', po='', subtotal='', tax='', total='')))
    assert pending['status'] == 'PENDING_REVIEW'
    with client.session_transaction() as sess: csrf = sess['csrf']
    response = client.post('/api/runs/bulk-review', json={
        'run_ids': [pending['run_id']], 'reviewer': 'AP reviewer',
        'outcome': 'VERIFIED', 'note': 'Source document inspected'}, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 200
    reloaded = client.get('/api/runs/' + pending['run_id']).json
    assert reloaded['status'] == 'PENDING_REVIEW'
    assert reloaded['reviews'][0]['reviewer'] == 'AP reviewer'


def test_ocr_cache_persists_but_decisions_are_recomputed(client, monkeypatch):
    calls = []
    def fake_ocr(data):
        calls.append(data)
        return invoice_text(number="CACHED-1")
    monkeypatch.setattr(processor, "_ocr_image", fake_ocr)
    raw = b"\x89PNG\r\n\x1a\nunique-cache-test"
    first = submit(client, raw, "first.png")
    assert first["status"] == "APPROVED" and first["ocr_cache_hit"] is False
    storage.engine.dispose()
    second = submit(client, raw, "again.png")
    assert second["status"] == "REJECTED" and second["ocr_cache_hit"] is True
    assert len(calls) == 1
    assert "cached" in next(s["detail"] for s in second["stages"] if s["stage"] == "PDF text / OCR" and s["status"] == "COMPLETED")


def test_ai_http_error_detail_is_reported_without_key(monkeypatch):
    from io import BytesIO
    body = b'{"error":{"message":"model unavailable for token-secret"}}'
    def fail(*_args, **_kwargs):
        raise HTTPError("https://example.test", 404, "Not Found", None, BytesIO(body))
    monkeypatch.setattr(ai_extraction.request, "urlopen", fail)
    result = ai_extraction._call_model("Invoice No: X", {"endpoint":"https://example.test",
                                               "model":"test-model", "api_key":"token-secret"})
    assert result["status"] == "UNAVAILABLE"
    assert "HTTP 404" in result["error"] and "[redacted]" in result["error"]
    assert "token-secret" not in result["error"]


def test_login_session_has_persistent_cookie(client, monkeypatch):
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "password")
    response = client.post("/login", data={"username":"demo", "password":"password"})
    assert response.status_code == 302
    assert "Expires=" in response.headers["Set-Cookie"]
    assert client.get("/api/runs").status_code == 200


@pytest.mark.parametrize("change,expected", [
    ({"number":""}, "APPROVED"), ({"total":""}, "PENDING_REVIEW"),
    ({"date":"31/02/2026"}, "APPROVED"), ({"vendor":"Wrong Vendor"}, "PENDING_REVIEW"),
    ({"po":""}, "APPROVED"), ({"po":"UNKNOWN-44"}, "APPROVED"),
    ({"total":"1001.00"}, "PENDING_REVIEW"), ({"subtotal":"800.00"}, "PENDING_REVIEW"),
    ({"heading":"DELIVERY CHALLAN"}, "PENDING_REVIEW"),
])
def test_decisions(client, change, expected):
    row = submit(client, pdf(invoice_text(**change)))
    assert row["status"] == expected
    assert row["reasons"]
    assert row["stages"]
    if change.get("heading") == "DELIVERY CHALLAN": assert row["document_type"] == "DELIVERY_CHALLAN"


def test_non_invoice_has_separate_routing_status(client):
    row = submit(client, pdf(invoice_text(heading="DELIVERY CHALLAN", number="CH-1")))
    assert row["status"] == "PENDING_REVIEW"
    assert row["document_routing"] == {
        "status": "NON_INVOICE",
        "reason": "Delivery Challan routed outside invoice approval",
    }
    stages = [stage for stage in row["stages"] if stage["stage"] == "AI OCR reconciliation"]
    assert stages[-1]["status"] == "SKIPPED"


def test_scanned_pdf_image_and_ocr_failure(client, monkeypatch):
    image = Image.new("RGB", (600,400), "white"); out=io.BytesIO(); image.save(out, format="PNG")
    scan = fitz.open(); p=scan.new_page(); p.insert_image(p.rect, stream=out.getvalue())
    monkeypatch.setattr(processor, "_ocr_image", lambda data: invoice_text(number="SCAN-1"))
    scanned = submit(client, scan.tobytes())
    assert scanned["method"] == "pdf+ocr" and scanned["status"] == "APPROVED"
    image_run = submit(client, out.getvalue(), "bill.png")
    assert image_run["method"] == "ocr"
    monkeypatch.setattr(processor, "_ocr_image", lambda data: (_ for _ in ()).throw(RuntimeError("OCR unavailable")))
    # A different document must still exercise OCR failure; an identical image
    # correctly reuses the prior successful OCR result.
    broken_image = Image.new("RGB", (600,400), "gray")
    broken_bytes = io.BytesIO(); broken_image.save(broken_bytes, format="PNG")
    failure = submit(client, broken_bytes.getvalue(), "broken.png")
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
    assert submit(client,pdf(excess))["status"] == "PENDING_REVIEW"


def test_manual_review_preserves_automated_decision(client):
    row=submit(client,pdf(invoice_text(po="", subtotal="800.00")))
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
    pending = submit(client, pdf(invoice_text(number="AI-NEEDS-REVIEW", po="", subtotal="800.00")))
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


def test_ai_connection_test_returns_safe_provider_status(client, monkeypatch):
    with client.session_transaction() as sess: csrf = sess["csrf"]
    monkeypatch.setattr(web, "_call_model", lambda *_: {"status":"UNAVAILABLE", "model":"test-model",
                                                           "error":"HTTP 404: model unavailable"})
    response = client.post("/api/ai/connection-test", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 503
    assert response.json == {"status":"UNAVAILABLE", "model":"test-model", "error":"HTTP 404: model unavailable"}
    monkeypatch.setattr(web, "_call_model", lambda *_: {"status":"COMPLETED", "model":"test-model", "fields":{}})
    response = client.post("/api/ai/connection-test", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and response.json["status"] == "COMPLETED"


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
    raw=pdf(invoice_text(number="PENDING-1",po="",subtotal="800.00"))
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


def test_issue_acknowledgement_is_audited_without_approving(client):
    row = submit(client, pdf(invoice_text(po="", subtotal="800.00")))
    rid = row["run_id"]
    status = row["status"]
    issue = next(c["check"] for c in row["decision"]["checks"] if c["outcome"] != "VERIFIED_PASS")
    with client.session_transaction() as sess: csrf = sess["csrf"]
    headers = {"X-CSRF-Token": csrf}
    url = f"/api/runs/{rid}/issues/{issue}"
    assert client.post(url, json={"reviewer": "Tester", "acknowledged": True}, headers=headers).status_code == 200
    saved = client.get(f"/api/runs/{rid}").json
    assert saved["status"] == status
    assert saved["reviews"][-1]["outcome"] == "ISSUE_ACKNOWLEDGED"
    assert client.post(url, json={"reviewer": "Tester", "acknowledged": False}, headers=headers).status_code == 200
    assert client.get(f"/api/runs/{rid}").json["reviews"][-1]["outcome"] == "ISSUE_REOPENED"
    assert client.post(f"/api/runs/{rid}/issues/not-real", json={"reviewer": "Tester", "acknowledged": True}, headers=headers).status_code == 400


def test_failed_ocr_uses_original_document_ai(client, monkeypatch):
    monkeypatch.setattr(web, "read_document_cached", lambda *args: (_ for _ in ()).throw(RuntimeError("OCR unavailable")))
    seen = []
    def vision(raw, suffix, config):
        seen.append((raw, suffix))
        return {"status": "COMPLETED", "text": invoice_text(), "pages": 1, "model": "vision-test"}
    monkeypatch.setattr(web, "read_document_with_ai", vision)
    raw = pdf(invoice_text())
    row = submit(client, raw)
    assert seen == [(raw, ".pdf")]
    assert row["method"] == "ai_vision"
    assert row["status"] == "APPROVED"
    assert row["ai_extraction"]["source"] == "original_page_images"


def test_failed_ocr_and_ai_never_auto_approves(client, monkeypatch):
    monkeypatch.setattr(web, "read_document_cached", lambda *args: (_ for _ in ()).throw(RuntimeError("OCR unavailable")))
    monkeypatch.setattr(web, "read_document_with_ai", lambda *args: {"status": "UNAVAILABLE", "error": "Provider timeout"})
    assert submit(client, pdf(invoice_text()))["status"] == "FAILED"


@pytest.mark.parametrize("list_response", [False, True])
def test_ai_vision_sends_page_images_and_reads_json(monkeypatch, list_response):
    captured = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args):
            return json.dumps({"choices": [{"message": {"content": json.dumps([{"text": invoice_text(), "bill_kind": "tax_invoice", "already_paid": False, "fields": {"total": {"value": 1000, "evidence": "Grand Total: 1000.00"}, "tax": {"value": 999, "evidence": "Tax: 180.00"}}}] if list_response else {"text": invoice_text(), "bill_kind": "tax_invoice", "already_paid": False, "fields": {"total": {"value": 1000, "evidence": "Grand Total: 1000.00"}, "tax": {"value": 999, "evidence": "Tax: 180.00"}}})}}]}).encode()
    def open_request(req, timeout):
        captured.append(json.loads(req.data))
        return Response()
    monkeypatch.setattr(ai_extraction.request, "urlopen", open_request)
    result = ai_extraction.read_document_with_ai(pdf(invoice_text()), ".pdf", {"endpoint": "https://example.com/v1/chat/completions", "model": "vision", "api_key": "test-only"})
    assert result["status"] == "COMPLETED"
    assert result["text"] == invoice_text()
    content = captured[0]["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert result["already_paid"] is False
    assert result["fields"]["total"] == 1000
    assert result["fields"]["tax"] is None


def test_ocr_and_ai_start_in_parallel_and_ai_can_win(monkeypatch):
    import threading
    ai_started = threading.Event()
    def slow_ocr(*args):
        assert ai_started.wait(1), "AI must start before OCR finishes"
        time.sleep(.1)
        return "unreadable", "ocr", 1, False
    def ai_read(*args):
        ai_started.set()
        return {"status": "COMPLETED", "text": invoice_text(), "pages": 1, "model": "test"}
    monkeypatch.setattr(web, "AI_RUNTIME_CONFIG", {"endpoint": "test", "model": "test", "api_key": "test"})
    monkeypatch.setattr(web, "read_document_cached", slow_ocr)
    monkeypatch.setattr(web, "read_document_with_ai", ai_read)
    text, method, pages, cached, vision = web.read_parallel(b"image", ".jpg", use_ocr=True)
    assert method == "ai_vision" and vision["status"] == "COMPLETED"


def test_isolated_ocr_releases_process_and_caches_result(client, monkeypatch):
    import subprocess
    monkeypatch.setenv("OCR_ISOLATED", "true")
    calls = []
    def child(args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps([invoice_text(), "ocr", 1]).encode(), stderr=b"")
    monkeypatch.setattr(subprocess, "run", child)
    first = web.read_document_cached(b"unique-image-process-test", ".jpg")
    second = web.read_document_cached(b"unique-image-process-test", ".jpg")
    assert first[0] == invoice_text() and first[-1] is False and second[-1] is True
    assert len(calls) == 1 and calls[0]["timeout"] == 25


def test_isolated_ocr_timeout_is_reported(client, monkeypatch):
    import subprocess
    monkeypatch.setenv("OCR_ISOLATED", "true")
    def timeout(*args, **kwargs): raise subprocess.TimeoutExpired("ocr", 25)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="deadline"):
        web.read_document_cached(b"unique-timeout-test", ".jpg")


def test_ai_default_does_not_start_ocr(monkeypatch):
    monkeypatch.setattr(web, "AI_RUNTIME_CONFIG", {"endpoint": "test", "model": "test", "api_key": "test"})
    def no_ocr(*args): raise AssertionError("OCR must be opt-in")
    monkeypatch.setattr(web, "read_document_cached", no_ocr)
    monkeypatch.setattr(web, "read_document_with_ai", lambda *a: {"status": "COMPLETED", "text": invoice_text(), "pages": 1})
    assert web.read_parallel(b"image", ".jpg")[1] == "ai_vision"


def test_verify_vendor_and_retry_audits_and_preserves_dataset_isolation(client, monkeypatch):
    row = submit(client, pdf(invoice_text(number="VERIFY-1", vendor="New Supplier", po="", subtotal="800")))
    queued = []
    monkeypatch.setattr(web, "submit_run", lambda *args: queued.append(args))
    with client.session_transaction() as sess: csrf = sess["csrf"]
    response = client.post(f"/api/runs/{row['run_id']}/retry", json={"reviewer":"Anant", "verify_vendor":True}, headers={"X-CSRF-Token":csrf})
    assert response.status_code == 202
    rows, _ = web.master_data.load("vendor_master", web.ROOT / "samples")
    assert next(v for v in rows if v["vendor"] == "New Supplier")["status"] == "APPROVED"
    assert queued[0][3] == "upload"
    assert client.get(f"/api/runs/{row['run_id']}").json["status"] == row["status"]
    assert {r["outcome"] for r in client.get(f"/api/runs/{row['run_id']}").json["reviews"]} >= {"VENDOR_VERIFIED", "RETRIED"}


def test_retry_cannot_unblock_supplier(client):
    (web.ROOT / "samples" / "vendor_master.csv").write_text("vendor,status\nAcme Supplies,BLOCKED\n")
    row = submit(client, pdf(invoice_text(number="VERIFY-BLOCK")))
    with client.session_transaction() as sess: csrf = sess["csrf"]
    response = client.post(f"/api/runs/{row['run_id']}/retry", json={"reviewer":"Anant", "verify_vendor":True}, headers={"X-CSRF-Token":csrf})
    assert response.status_code == 409


def test_structured_vision_clears_heuristic_amounts_and_derives_lines(monkeypatch):
    payload = {"text":"Seller: Acme Supplies\nPart A 500.00\nPart B 320.00\nCGST 90.00 SGST 90.00\nGrand Total 1000.00", "fields":{"vendor":{"value":"Acme Supplies","evidence":"Seller: Acme Supplies"}, "tax":{"value":180,"evidence":"CGST 90.00 SGST 90.00"},"total":{"value":1000,"evidence":"Grand Total 1000.00"}}, "line_items":[{"description":"A","amount":500},{"description":"B","amount":320}], "document_type":"TAX_INVOICE"}
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,*args): return json.dumps({"choices":[{"message":{"content":json.dumps(payload)}}]}).encode()
    def provider(req, timeout):
        body = json.loads(req.data)
        assert body["response_format"]["type"] == "json_schema"
        assert "line_items" in body["response_format"]["json_schema"]["schema"]["required"]
        return Response()
    monkeypatch.setattr(ai_extraction.request,"urlopen",provider)
    result = ai_extraction.read_document_with_ai(pdf(invoice_text()),".pdf",{"endpoint":"https://example.com","model":"test","api_key":"test"})
    assert result["status"] == "COMPLETED"
    assert result["fields"]["subtotal"] == 820
    assert result["fields"]["tax"] == 180
    assert result["fields"]["invoice_number"] is None  # no heuristic substitution
    assert result["details"]["tax_breakdown"] == {}


def test_structured_vision_never_keeps_parser_guesses(client, monkeypatch):
    monkeypatch.setattr(web,"read_document_with_ai",lambda *a:{"status":"COMPLETED","structured":True,"text":invoice_text(),"pages":1,"model":"test","fields":{k:None for k in ai_extraction.VISION_FIELDS},"details":{"identity_ambiguous":False,"tax_breakdown":{}},"line_items":[]})
    monkeypatch.setattr(web,"read_document_cached",lambda *a: (_ for _ in ()).throw(RuntimeError()))
    row=submit(client,pdf(invoice_text()))
    assert row["status"] == "PENDING_REVIEW"
    assert row["invoice"]["total"] is None

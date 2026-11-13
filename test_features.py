"""Tests for schema v2, performance paths and the AP features added on top."""
import gzip
import io
import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text

import app as web
import asa_invoice_processor as processor
import storage
from test_app import client, invoice_text, pdf, submit  # noqa: F401  (pytest fixture)


def csrf_of(client):
    client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf"]


def write_pos(client, body):
    (web.ROOT / "samples" / "pos.csv").write_text(body, encoding="utf-8")


# --- Schema v2 migration ---------------------------------------------------

def test_v1_database_migrates_additively_and_backfills(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "v1.db"), future=True)
    with engine.begin() as conn:  # The exact v1 schema shipped before this change.
        conn.exec_driver_sql("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO schema_migrations VALUES (1)")
        conn.exec_driver_sql("CREATE TABLE documents (id VARCHAR(36) PRIMARY KEY, filename VARCHAR(180) NOT NULL, "
                             "mime VARCHAR(80) NOT NULL, content BLOB NOT NULL, created_at DATETIME NOT NULL)")
        conn.exec_driver_sql("CREATE TABLE processing_runs (id VARCHAR(36) PRIMARY KEY, document_id VARCHAR(36), "
                             "created_at DATETIME NOT NULL, source VARCHAR(180) NOT NULL, event_type VARCHAR(40) NOT NULL, "
                             "status VARCHAR(30) NOT NULL, payload TEXT NOT NULL)")
        payload = {"vendor": "Acme Supplies", "invoice_number": "OLD-1", "po_number": "PO-1", "total": 400.0,
                   "due_date": "2026-12-01", "archived": True, "timings": {"total_seconds": 1.5},
                   "document_routing": {"status": "INVOICE_AP_WORKFLOW"}}
        conn.execute(text("INSERT INTO processing_runs VALUES ('r1', NULL, '2026-11-01 10:00:00', 'old.pdf', "
                          "'upload', 'APPROVED', :p)"), {"p": json.dumps(payload)})
    monkeypatch.setattr(storage, "engine", engine)
    storage.initialize()
    storage.initialize()  # idempotent
    columns = {c["name"] for c in inspect(engine).get_columns("processing_runs")}
    assert set(storage.SUMMARY_COLUMNS) <= columns
    assert {i["name"] for i in inspect(engine).get_indexes("processing_runs")} >= {"ix_runs_vendor_key", "ix_runs_po_key"}
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT max(version) FROM schema_migrations").scalar() == storage.SCHEMA_VERSION
        row = conn.exec_driver_sql("SELECT vendor_key, invoice_key, po_key, total, due_date, archived, "
                                   "processing_seconds, routing FROM processing_runs").one()
    assert tuple(row) == ("acmesupplies", "old1", "po1", 400.0, "2026-12-01", 1, 1.5, "INVOICE_AP_WORKFLOW")
    # The archived legacy approval still blocks a duplicate and consumes PO balance.
    history = storage.decision_history({"vendor": "ACME supplies", "po_number": "po-1"})
    assert [h["run_id"] for h in history] == ["r1"]
    assert storage.list_runs() == [] and len(storage.list_runs(include_archived=True)) == 1
    engine.dispose()


def test_decision_history_only_loads_related_rows(client):
    submit(client, pdf(invoice_text(number="REL-1")))
    write_pos(client, "po_number,vendor,po_amount,invoiced_to_date\nPO-1,Acme Supplies,1000,0\nPO-9,Other Co,500,0\n")
    submit(client, pdf(invoice_text(number="UNREL-1", vendor="Other Co", po="PO-9", subtotal="500.00", tax="0.00", total="500.00")))
    related = storage.decision_history({"vendor": "Acme Supplies", "po_number": "PO-1"})
    assert [r["invoice_number"] for r in related] == ["rel1"]
    assert storage.decision_history({"vendor": None, "po_number": None}) == []


# --- New AP controls -------------------------------------------------------

def test_possible_duplicate_with_different_number_routes_to_review(client):
    write_pos(client, "po_number,vendor,po_amount,invoiced_to_date\nPO-1,Acme Supplies,5000,0\n")
    assert submit(client, pdf(invoice_text(number="ORIG-1")))["status"] == "APPROVED"
    lookalike = submit(client, pdf(invoice_text(number="ORIG-1-R")))
    assert lookalike["status"] == "APPROVED"
    check = next(c for c in lookalike["decision"]["checks"] if c["check"] == "possible_duplicate")
    assert check["outcome"] == "ADVISORY"
    # A different invoice date is a legitimate repeat order, not a lookalike.
    assert submit(client, pdf(invoice_text(number="ORIG-2", date="2026-11-11")))["status"] == "APPROVED"


def test_exact_duplicate_still_rejected_not_double_flagged(client):
    raw = pdf(invoice_text(number="EXACT-1"))
    assert submit(client, raw)["status"] == "APPROVED"
    again = submit(client, raw)
    assert again["status"] == "REJECTED"
    assert not any(c["check"] == "possible_duplicate" for c in again["decision"]["checks"])


def test_future_dated_invoice_is_reported_without_hold(tmp_path):
    po = tmp_path / "pos.csv"; po.write_text("po_number,vendor,po_amount,invoiced_to_date\nPO-1,Acme Supplies,1000,0\n")
    inv = {"invoice_number": "F-1", "po_number": "PO-1", "vendor": "Acme Supplies", "date": "2026-12-01",
           "total": 1000, "subtotal": 1000, "tax": 0, "details": {}, "document_type": "TAX_INVOICE"}
    future = processor.decide(inv, po, history_rows=[], today=date(2026, 11, 13))
    assert future.status == "APPROVED"
    assert any(c["check"] == "invoice_date_future" for c in future.checks)
    # One day of grace absorbs time-zone differences.
    assert processor.decide({**inv, "date": "14/11/2026"}, po, history_rows=[], today=date(2026, 11, 13)).status == "APPROVED"


def test_parse_date_formats():
    assert processor.parse_date("2026-11-10") == date(2026, 11, 10)
    assert processor.parse_date("10/11/2026") == date(2026, 11, 10)
    assert processor.parse_date("10-Nov-2026") == date(2026, 11, 10)
    assert processor.parse_date("31/02/2026") is None and processor.parse_date(None) is None


# --- List API, compression, events ----------------------------------------

def test_runs_list_is_slim_paginated_and_shows_latest_review(client):
    rows = [submit(client, pdf(invoice_text(number=f"PAGE-{i}", po="", subtotal="800.00"))) for i in range(3)]
    csrf = csrf_of(client)
    client.post(f"/api/runs/{rows[0]['run_id']}/reviews", json={"reviewer": "R1", "outcome": "VERIFIED", "note": ""},
                headers={"X-CSRF-Token": csrf})
    listed = client.get("/api/runs")
    assert listed.headers["X-Total-Count"] == "3"
    data = listed.json
    assert all(k not in data[0] for k in storage.HEAVY_KEYS)
    by_id = {r["run_id"]: r for r in data}
    assert by_id[rows[0]["run_id"]]["reviews"][-1]["outcome"] == "VERIFIED"  # was always "Unchecked" before
    assert by_id[rows[1]["run_id"]]["reviews"] == []
    page = client.get("/api/runs?limit=2&offset=2").json
    assert len(client.get("/api/runs?limit=2").json) == 2 and len(page) == 1
    assert page[0]["run_id"] not in {r["run_id"] for r in client.get("/api/runs?limit=2").json}
    assert client.get("/api/runs?decision=PENDING_REVIEW").headers["X-Total-Count"] == "3"
    assert client.get("/api/runs?decision=APPROVED").json == []
    assert client.get("/api/runs?decision=bogus").status_code == 400
    assert client.get("/api/runs?date=nope").status_code == 400
    today = datetime.now(timezone.utc).date().isoformat()
    assert len(client.get("/api/runs?date=" + today).json) == 3
    assert client.get("/api/runs?date=2001-01-01").json == []
    assert [r["invoice_number"] for r in client.get("/api/runs?q=page-1").json] == ["PAGE-1"]
    assert len(client.get("/api/runs?vendor=acme").json) == 3


def test_full_run_still_has_complete_evidence(client):
    row = submit(client, pdf(invoice_text(number="FULL-1")))
    full = client.get("/api/runs/" + row["run_id"]).json
    assert full["decision"]["checks"] and full["invoice"]["invoice_number"] == "FULL-1"


def test_gzip_for_large_json_only_when_accepted(client):
    for i in range(4): submit(client, pdf(invoice_text(number=f"GZ-{i}")))
    plain = client.get("/api/runs")
    assert "Content-Encoding" not in plain.headers
    zipped = client.get("/api/runs", headers={"Accept-Encoding": "gzip, deflate"})
    assert zipped.headers["Content-Encoding"] == "gzip" and "Accept-Encoding" in zipped.headers["Vary"]
    assert json.loads(gzip.decompress(zipped.data)) == plain.json
    assert int(zipped.headers["Content-Length"]) == len(zipped.data) < len(plain.data)


def test_event_stream_replays_stages_and_finishes(client):
    row = submit(client, pdf(invoice_text(number="SSE-1")))
    body = client.get(f"/api/runs/{row['run_id']}/events", headers={"Accept-Encoding": "gzip"}).get_data(as_text=True)
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {") and '"stage"' in line]
    assert len(events) == len(row["stages"]) and "id" not in events[0]
    assert 'event: finished\ndata: {"status": "APPROVED"}' in body
    assert client.get("/api/runs/missing/events").status_code == 404


# --- Master data -----------------------------------------------------------

def test_master_data_validation_storage_and_use_in_decisions(client):
    csrf = csrf_of(client)
    default = client.get("/api/master/po_master").json
    assert default["source"] == "file" and default["rows"][0]["po_number"] == "PO-1"
    assert client.put("/api/master/po_master", json={"csv": "po_number,vendor\n", "updated_by": "AP"}).status_code == 403
    bad = client.put("/api/master/po_master", headers={"X-CSRF-Token": csrf}, json={
        "csv": "po_number,vendor,po_amount,invoiced_to_date\nPO-7,Acme,abc,0\nPO-7,Acme,10,20\n", "updated_by": "AP"})
    assert bad.status_code == 400
    assert any("po_amount" in e for e in bad.json["errors"]) and any("duplicate PO" in e for e in bad.json["errors"])
    assert client.put("/api/master/po_master", headers={"X-CSRF-Token": csrf},
                      json={"csv": "po_number,vendor,po_amount,invoiced_to_date\nPO-7,Acme,1,0\n"}).status_code == 400
    ok = client.put("/api/master/po_master", headers={"X-CSRF-Token": csrf}, json={
        "csv": "﻿PO_Number,Vendor,PO_Amount,Invoiced_To_Date\nPO-77,Acme Supplies,\"2,000\",500\n", "updated_by": "AP lead"})
    assert ok.status_code == 200 and ok.json["saved"] == 1
    # PO-1 was only in the file; decisions now use the database master.
    assert submit(client, pdf(invoice_text(number="M-1")))["status"] == "APPROVED"
    approved = submit(client, pdf(invoice_text(number="M-2", po="PO-77")))
    assert approved["status"] == "APPROVED"
    po = client.get("/api/master/po_master").json
    assert po["source"] == "database" and po["updated_by"] == "AP lead"
    assert po["rows"][0]["approved_value"] == 1000.0
    assert po["rows"][0]["remaining"] == 500.0 and po["rows"][0]["utilisation"] == 0.75
    exported = client.get("/api/master/po_master.csv")
    assert exported.status_code == 200 and b"PO-77,Acme Supplies,2000.0,500.0" in exported.data
    assert client.get("/api/master/unknown").status_code == 404


def test_vendor_master_gstin_validation_and_blocking(client):
    csrf = csrf_of(client)
    bad = client.put("/api/master/vendor_master", headers={"X-CSRF-Token": csrf},
                     json={"csv": "vendor,gstin,status\nAcme Supplies,29ABCDE1234F1Z0,APPROVED\n", "updated_by": "AP"})
    assert bad.status_code == 400 and "GSTIN" in bad.json["errors"][0]
    ok = client.put("/api/master/vendor_master", headers={"X-CSRF-Token": csrf},
                    json={"csv": "vendor,gstin,status\nAcme Supplies,,BLOCKED\n", "updated_by": "AP"})
    assert ok.status_code == 200
    blocked = submit(client, pdf(invoice_text(number="VB-1")))
    assert blocked["status"] == "REJECTED"
    assert any(c["check"] == "vendor_authorisation" for c in blocked["decision"]["checks"])


def test_correction_recheck_uses_database_master(client):
    csrf = csrf_of(client)
    client.put("/api/master/po_master", headers={"X-CSRF-Token": csrf}, json={
        "csv": "po_number,vendor,po_amount,invoiced_to_date\nPO-55,Acme Supplies,1000,0\n", "updated_by": "AP"})
    row = submit(client, pdf(invoice_text(number="FIX-1", po="", subtotal="800.00")))
    response = client.post(f"/api/runs/{row['run_id']}/reviews", headers={"X-CSRF-Token": csrf}, json={
        "reviewer": "R", "outcome": "NEEDS_CORRECTION", "note": "PO on page 2", "corrections": {"po_number": "PO-55"}})
    assert response.json["revised_review_result"]["po_matching"]["status"] == "MATCHED"


# --- Analytics, audit, security -------------------------------------------

def test_analytics_summary_metrics_and_aging(client, monkeypatch):
    approved = submit(client, pdf(invoice_text(number="AG-1", subtotal="400.00", tax="0.00", total="400.00")))
    submit(client, pdf(invoice_text(number="AG-2", po="", subtotal="800.00")))
    submit(client, pdf(invoice_text(heading="DELIVERY CHALLAN", number="CH-9")))
    with storage.engine.begin() as conn:  # Give the approved invoice a past due date.
        conn.execute(storage.runs.update().where(storage.runs.c.id == approved["run_id"]).values(due_date="01/01/2020"))
    s = client.get("/api/analytics/summary").json
    m = s["invoice_metrics"]
    assert (m["invoices"], m["approved"], m["pending"], m["non_invoices"], m["decided"]) == (2, 1, 1, 1, 2)
    assert m["value"] == 1400.0
    assert s["touchless_rate"] == 0.5 and s["exception_rate"] == 0.5
    aging = {b["bucket"]: b for b in s["aging"]}
    assert aging["Overdue"] == {"bucket": "Overdue", "count": 1, "value": 400.0}
    assert s["top_vendors"][0]["vendor"] == "Acme Supplies" and s["top_vendors"][0]["approved_value"] == 400.0
    assert s["processing_seconds"]["median"] is not None


def test_audit_export_contains_trace(client):
    row = submit(client, pdf(invoice_text(number="AUD-1")))
    response = client.get(f"/api/runs/{row['run_id']}/audit.json")
    assert "attachment" in response.headers["Content-Disposition"]
    body = json.loads(response.data)
    assert body["run"]["status"] == "APPROVED" and len(body["run"]["stages"]) == len(row["stages"])
    assert client.get("/api/runs/nope/audit.json").status_code == 404


def test_login_throttling(client, monkeypatch):
    monkeypatch.setenv("DEMO_USERNAME", "demo"); monkeypatch.setenv("DEMO_PASSWORD", "pw")
    web.login_failures.clear()
    for _ in range(web.LOGIN_MAX_FAILURES):
        assert client.post("/login", data={"username": "demo", "password": "wrong"}).status_code == 401
    assert client.post("/login", data={"username": "demo", "password": "pw"}).status_code == 429
    web.login_failures.clear()
    assert client.post("/login", data={"username": "demo", "password": "pw"}).status_code == 302


def test_security_headers(client):
    response = client.get("/healthz")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "same-origin"


def test_workspace_shell_and_assets(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="view"' in html and "/static/app.js" in html and "/static/app.css" in html
    assert 'href="#/review"' in html and "window.APP" in html
    js = client.get("/static/app.js").get_data(as_text=True)
    for route in ("overviewPage", "uploadPage", "reviewPage", "invoicePage", "vendorsPage", "dataPage", "howPage"):
        assert route in js
    assert client.get("/static/app.css").status_code == 200


def test_bulk_upload_is_queued_through_bounded_pool(client):
    csrf = csrf_of(client)
    # Distinct amounts: identical vendor/amount/date/PO would (correctly) trip possible_duplicate.
    files = [(io.BytesIO(pdf(invoice_text(number=f"Q-{i}", subtotal=f"{10+i}.00", tax="0.00", total=f"{10+i}.00"))), f"q{i}.pdf")
             for i in range(6)]
    response = client.post("/api/runs/bulk", data={"invoice_files": files}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 202
    import time
    for _ in range(200):
        statuses = [client.get("/api/runs/" + r["run_id"]).json["status"] for r in response.json["runs"]]
        if "RUNNING" not in statuses: break
        time.sleep(.05)
    assert statuses == ["APPROVED"] * 6
    assert web.executor._max_workers == web.PROCESSING_WORKERS


def test_blank_po_label_does_not_borrow_next_label():
    inv = processor.extract_invoice("TAX INVOICE\nInvoice Number: X-1\nPO Number:\nSubtotal: 400.00\nGrand Total: 400.00")
    assert inv["po_number"] is None
    # The split-line layout this fallback exists for still works.
    assert processor.extract_invoice("TAX INVOICE\nPO No\nPO-1001\nTotal: 5")["po_number"] == "PO-1001"


def test_font_stylesheet_url_is_not_html_escaped(client):
    # An escaped URL becomes relative and re-requests the whole page as CSS.
    html = client.get("/").get_data(as_text=True)
    assert "&#39;https://fonts" not in html and 'href="https://fonts.googleapis.com/css2?family=Geist' in html

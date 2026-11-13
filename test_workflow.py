"""Exception workflow: routing, reviewer resolution, and the APIs behind the workspace."""
import json

from sqlalchemy import create_engine, inspect

import app as web
import storage
import workflow
from test_app import client, invoice_text, pdf, submit  # noqa: F401  (pytest fixture)


def csrf_of(client):
    client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf"]


def resolve(client, rid, action, note="", corrections=None, reviewer="Priya"):
    return client.post(f"/api/runs/{rid}/resolve", headers={"X-CSRF-Token": csrf_of(client)},
                       json={"action": action, "reviewer": reviewer, "note": note, "corrections": corrections or {}})


def test_route_priority_and_labels():
    checks = lambda *names: [{"check": n, "outcome": "UNCERTAIN"} for n in names]
    assert workflow.route("PENDING_REVIEW", checks("po_match", "possible_duplicate"))["team"] == "Treasury & Fraud"
    assert workflow.route("PENDING_REVIEW", checks("po_match", "vendor_master"))["team"] == "Vendor Management"
    r = workflow.route("PENDING_REVIEW", checks("required_fields", "po_match"))
    assert r["team"] == "Procurement" and r["sla_hours"] == 48 and r["reason"] == "Purchase order found"
    assert workflow.route("PENDING_REVIEW", checks("invoice_math"))["team"] == "AP Specialist"
    assert workflow.route("APPROVED", []) is None
    assert set(workflow.CHECK_LABELS) >= {"duplicate_detection", "po_remaining", "possible_duplicate"}


def test_pending_invoice_is_routed_and_queued(client):
    row = submit(client, pdf(invoice_text(number="Q-1", po="")))
    assert row["review_route"]["team"] == "Procurement"
    queue = client.get("/api/runs?open=1")
    assert [r["run_id"] for r in queue.json] == [row["run_id"]] and queue.headers["X-Total-Count"] == "1"
    assert client.get("/api/runs?open=1&team=Procurement").json[0]["route_team"] == "Procurement"
    assert client.get("/api/runs?open=1&team=Treasury %26 Fraud").json == []
    # Filed non-invoices and approved invoices never enter the queue.
    submit(client, pdf(invoice_text(heading="DELIVERY CHALLAN", number="CH-1")))
    submit(client, pdf(invoice_text(number="OK-1")))
    assert len(client.get("/api/runs?open=1").json) == 1


def test_reviewer_approval_closes_queue_and_counts_in_ledger(client):
    row = submit(client, pdf(invoice_text(number="R-1", po="")))
    # Approve with the PO the reviewer confirmed; the ledger follows the correction.
    response = resolve(client, row["run_id"], "APPROVED", "Buyer confirmed PO-1", {"po_number": "PO-1"})
    assert response.status_code == 200, response.json
    saved = client.get(f"/api/runs/{row['run_id']}").json
    assert saved["status"] == "PENDING_REVIEW"            # automated decision preserved
    assert saved["resolution"] == "APPROVED"
    assert saved["resolution_detail"]["reviewer"] == "Priya"
    assert saved["reviews"][-1]["outcome"] == "APPROVED_BY_REVIEWER"
    assert client.get("/api/runs?open=1").json == []
    assert [r["run_id"] for r in client.get("/api/runs?decision=APPROVED").json] == [row["run_id"]]
    assert client.get("/api/runs?decision=PENDING_REVIEW").json == []
    # Reviewer-approved invoice blocks a duplicate and consumed PO-1's full balance.
    assert submit(client, pdf(invoice_text(number="R-1")))["status"] == "REJECTED"
    over = submit(client, pdf(invoice_text(number="R-2", subtotal="1.00", tax="0.00", total="1.00")))
    assert over["status"] == "REJECTED" and any("remaining PO=0.00" in r for r in over["reasons"])
    assert client.get("/api/master/po_master").json["rows"][0]["approved_value"] == 1000.0


def test_approval_blocked_when_ledger_rule_would_break(client):
    pending = submit(client, pdf(invoice_text(number="LATE-1", po="")))
    assert submit(client, pdf(invoice_text(number="FIRST-1")))["status"] == "APPROVED"  # consumes PO-1
    blocked = resolve(client, pending["run_id"], "APPROVED", "PO-1", {"po_number": "PO-1"})
    assert blocked.status_code == 409 and "remaining PO" in blocked.json["error"]
    assert client.get(f"/api/runs/{pending['run_id']}").json["resolution"] is None
    # Rejecting is always possible, but needs a reason for the vendor.
    assert resolve(client, pending["run_id"], "REJECTED").status_code == 400
    assert resolve(client, pending["run_id"], "REJECTED", "PO already fully billed").status_code == 200
    again = resolve(client, pending["run_id"], "APPROVED", "retry")
    assert again.status_code == 409 and "Already rejected" in again.json["error"]


def test_resolve_validation(client):
    approved = submit(client, pdf(invoice_text(number="AUTO-1")))
    assert resolve(client, approved["run_id"], "APPROVED").status_code == 409
    challan = submit(client, pdf(invoice_text(heading="DELIVERY CHALLAN", number="CH-2")))
    assert resolve(client, challan["run_id"], "APPROVED").status_code == 409
    pending = submit(client, pdf(invoice_text(number="V-1", po="")))
    assert resolve(client, pending["run_id"], "MAYBE").status_code == 400
    assert resolve(client, pending["run_id"], "APPROVED", reviewer="").status_code == 400
    assert resolve(client, pending["run_id"], "APPROVED", corrections={"bank": "x"}).status_code == 400
    assert resolve(client, pending["run_id"], "APPROVED", corrections={"total": "abc"}).status_code == 400
    assert resolve(client, "missing", "APPROVED").status_code == 404


def test_analytics_includes_workflow_metrics(client):
    pending = submit(client, pdf(invoice_text(number="AN-1", po="")))
    submit(client, pdf(invoice_text(number="AN-2", subtotal="400.00", tax="0.00", total="400.00")))
    rejected = submit(client, pdf(invoice_text(number="AN-2", subtotal="400.00", tax="0.00", total="400.00")))
    assert rejected["status"] == "REJECTED"
    s = client.get("/api/analytics/summary").json
    assert s["money_stopped"] == 400.0
    assert s["time_saved_minutes"] == web.MANUAL_MINUTES_PER_INVOICE
    assert {q["team"]: q["open"] for q in s["queue"]}["Procurement"] == 1
    assert any(r["check"] == "duplicate_detection" and r["rejected"] == 1 for r in s["stop_reasons"])
    assert any(r["check"] == "po_match" and r["review"] == 1 for r in s["stop_reasons"])
    assert s["daily"] and sum(d["APPROVED"] + d["PENDING_REVIEW"] + d["REJECTED"] for d in s["daily"]) == 3
    assert [x["stage"] for x in s["step_seconds"]][0] == "Document received"
    resolve(client, pending["run_id"], "REJECTED", "No PO exists")
    s = client.get("/api/analytics/summary").json
    assert s["invoice_metrics"]["rejected_by_reviewer"] == 1 and s["reviewers"][0]["reviewer"] == "Priya"
    assert s["reviewers"][0]["rejected"] == 1 and s["reviewers"][0]["median_seconds"] >= 0
    assert s["money_stopped"] == 1400.0


def test_vendor_profiles(client):
    (web.ROOT / "samples" / "vendor_master.csv").write_text("vendor,gstin,status\nAcme Supplies,,APPROVED\nIdle Co,,BLOCKED\n")
    submit(client, pdf(invoice_text(number="VP-1", subtotal="300.00", tax="0.00", total="300.00")))
    submit(client, pdf(invoice_text(number="VP-2", vendor="Stranger Ltd", po="")))
    vendors = {v["vendor"]: v for v in client.get("/api/vendors").json["vendors"]}
    acme = vendors["Acme Supplies"]
    assert acme["invoices"] == 1 and acme["approved_value"] == 300.0 and acme["open_pos"] == 1 and acme["po_remaining"] == 700.0
    assert "Blocked in vendor master" in vendors["Idle Co"]["attention"]
    assert vendors["Stranger Ltd"]["on_file"] is False and vendors["Stranger Ltd"]["waiting"] == 1


def test_sample_inbox_lists_and_runs(client, monkeypatch):
    demo = web.ROOT / "samples" / "demo"; demo.mkdir(parents=True)
    (demo / "01_clean_invoice.pdf").write_bytes(pdf(invoice_text(number="DEMO-9")))
    (demo / "notes.txt").write_text("ignored")
    items = client.get("/api/demo").json
    assert [i["file"] for i in items] == ["01_clean_invoice.pdf"] and items[0]["tag"] == "Happy path"
    assert client.get("/api/demo/01_clean_invoice.pdf").data.startswith(b"%PDF")
    assert client.get("/api/demo/../app.py").status_code == 404
    rid = client.post("/api/demo/01_clean_invoice.pdf", headers={"X-CSRF-Token": csrf_of(client)}).json["run_id"]
    import time
    for _ in range(100):
        run = client.get("/api/runs/" + rid).json
        if run["status"] != "RUNNING": break
        time.sleep(.05)
    assert run["status"] == "APPROVED" and run["invoice_number"] == "DEMO-9"


def test_meta_vocabulary(client):
    m = client.get("/api/meta").json
    assert m["stages"] == web.STAGE_NAMES and m["checks"]["po_match"] == "Purchase order found"
    assert {t["team"] for t in m["teams"]} == set(workflow.TEAMS)


def test_logout_accepts_form_csrf_token(client):
    csrf = csrf_of(client)
    assert client.post("/logout").status_code == 403
    assert client.post("/logout", data={"csrf": csrf}).status_code == 302


def test_v2_database_upgrades_to_v3_keeping_archive_flag(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "v2.db"), future=True)
    monkeypatch.setattr(storage, "engine", engine)
    storage.initialize()
    with engine.begin() as conn:  # Reproduce a v2 database: drop the v3 columns.
        for col in ("route_team", "flags", "resolution"):
            conn.exec_driver_sql("DROP INDEX IF EXISTS ix_runs_route_team")
            conn.exec_driver_sql(f"ALTER TABLE processing_runs DROP COLUMN {col}")
        conn.exec_driver_sql("DELETE FROM schema_migrations WHERE version = 3")
        payload = {"vendor": "Acme", "invoice_number": "P-1", "document_routing": {"status": "INVOICE_AP_WORKFLOW"},
                   "decision": {"checks": [{"check": "po_match", "outcome": "UNCERTAIN"}]}}
        conn.exec_driver_sql("INSERT INTO processing_runs (id, created_at, source, event_type, status, payload, archived) "
                             "VALUES ('old', '2026-11-01 10:00:00', 'a.pdf', 'upload', 'PENDING_REVIEW', ?, 1)", (json.dumps(payload),))
    storage.initialize()
    assert {"route_team", "flags", "resolution"} <= {c["name"] for c in inspect(engine).get_columns("processing_runs")}
    with engine.connect() as conn:
        assert tuple(conn.exec_driver_sql("SELECT route_team, flags, archived, resolution FROM processing_runs").one()) == \
            ("Procurement", "po_match", 1, None)
        assert conn.exec_driver_sql("SELECT max(version) FROM schema_migrations").scalar() == 3
    engine.dispose()


def test_search_treats_underscore_and_percent_literally(client):
    submit(client, pdf(invoice_text(number="INV_2026_01")))
    submit(client, pdf(invoice_text(number="INVX2026X02", subtotal="10.00", tax="0.00", total="10.00")))
    assert [r["invoice_number"] for r in client.get("/api/runs?q=INV_2026").json] == ["INV_2026_01"]
    assert client.get("/api/runs?q=bill.pdf").json  # source filename search
    assert client.get("/api/runs?q=100%25").json == []


def test_blocked_approval_names_the_blocking_rule(client):
    pending = submit(client, pdf(invoice_text(number="LOOK-2", po="")))
    assert submit(client, pdf(invoice_text(number="LOOK-1")))["status"] == "APPROVED"
    blocked = resolve(client, pending["run_id"], "APPROVED", "PO-1", {"po_number": "PO-1"})
    assert blocked.status_code == 409
    assert "remaining PO" in blocked.json["error"] and "Possible duplicate" not in blocked.json["error"]

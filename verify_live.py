"""Authenticated live smoke test. Reads credentials only from environment."""
import os
import re
import sys
import time
from pathlib import Path
import requests

base = os.getenv("LIVE_URL", "https://zampai-invoice-intelligence.onrender.com").rstrip("/")
session = requests.Session()
health = session.get(base + "/healthz", timeout=90)
assert health.status_code == 200 and health.json()["status"] == "ok", (health.status_code, health.text[:200])
login = session.post(base + "/login", data={"username":os.environ["DEMO_USERNAME"],
                                              "password":os.environ["DEMO_PASSWORD"]}, timeout=90)
assert login.status_code == 200 and "Your bills, under control" in login.text, (login.status_code, login.url)
match = re.search(r"const csrf=\"([^\"]+)\"", login.text)
assert match, "CSRF token missing"
csrf = match.group(1)
file = Path(__file__).parent / "samples" / "demo" / "04_missing_po.pdf"
with file.open("rb") as stream:
    response = session.post(base + "/api/runs", files={"invoice_file":(file.name,stream,"application/pdf")},
                            headers={"X-CSRF-Token":csrf}, timeout=90)
assert response.status_code == 202, (response.status_code,response.text[:200])
rid = response.json()["run_id"]
for _ in range(100):
    row_response = session.get(base + "/api/runs/" + rid, timeout=90)
    assert row_response.status_code == 200
    row = row_response.json()
    if row["status"] != "RUNNING": break
    time.sleep(1)
else: raise AssertionError("Run remained RUNNING")
assert row["status"] == "PENDING_REVIEW", (row["status"], row.get("error"))
assert row["invoice_number"] == "DEMO-003" and row["document_type"] == "TAX_INVOICE"
assert row["stages"] and row["decision"]["po_matching"]["status"] == "NOT_IDENTIFIED"
history = session.get(base + "/api/runs", timeout=90).json()
assert any(r["run_id"] == rid for r in history)
document = session.get(base + "/api/runs/" + rid + "/document", timeout=90)
assert document.status_code == 200 and document.content == file.read_bytes()
private = requests.get(base + "/api/runs/" + rid + "/document", timeout=90)
assert private.url.endswith("/login") and private.content != file.read_bytes()
sample_response = session.post(base + "/api/samples/6", headers={"X-CSRF-Token":csrf}, timeout=90)
assert sample_response.status_code == 202, (sample_response.status_code,sample_response.text[:200])
sample_id = sample_response.json()["run_id"]
for _ in range(150):
    sample = session.get(base + "/api/runs/" + sample_id, timeout=90).json()
    if sample["status"] != "RUNNING": break
    time.sleep(1)
else: raise AssertionError("Sample run remained RUNNING")
assert sample["status"] == "PENDING_REVIEW", (sample["status"],sample.get("error"))
assert sample["document_type"] == "DELIVERY_CHALLAN" and sample["method"] == "ocr"
assert sample["event_type"] == "dataset_evaluation" and sample.get("comparisons")
print("PASS health, login, PDF upload, decision, stages, history and stored document")
print("run", rid, "status", row["status"], "method", row["method"])
print("PASS original dataset OCR, delivery challan classification, benchmark isolation and document access control")
print("sample run", sample_id, "status", sample["status"])

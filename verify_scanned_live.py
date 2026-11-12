"""Live OCR approval and persistence smoke test; credentials only in environment."""
import os
import re
import time
from pathlib import Path
import requests

base = os.getenv("LIVE_URL", "https://zampai-invoice-intelligence.onrender.com").rstrip("/")
s = requests.Session()
r = s.post(base + "/login", data={"username":os.environ["DEMO_USERNAME"],
                                    "password":os.environ["DEMO_PASSWORD"]}, timeout=90)
assert r.status_code == 200 and "Your bills, under control" in r.text
csrf = re.search(r"const csrf=\"([^\"]+)\"", r.text).group(1)
old = s.get(base + "/api/runs/d51d3aa8-1ce2-4846-beea-273e71f62f4e", timeout=90)
assert old.status_code == 200 and old.json()["status"] == "PENDING_REVIEW", "History lost on redeploy"
file = Path(__file__).parent / "samples" / "demo" / "05_scanned_invoice.png"
with file.open("rb") as stream:
    r = s.post(base + "/api/runs", files={"invoice_file":(file.name,stream,"image/png")},
               headers={"X-CSRF-Token":csrf}, timeout=90)
assert r.status_code == 202, (r.status_code,r.text[:100])
rid = r.json()["run_id"]
for _ in range(180):
    try:
        r = s.get(base + "/api/runs/" + rid, timeout=90)
    except requests.RequestException:
        time.sleep(1)
        continue
    if r.status_code in (502, 503, 504):
        time.sleep(1)
        continue
    assert r.status_code == 200, (r.status_code, r.text[:100])
    row = r.json()
    if row["status"] != "RUNNING": break
    time.sleep(1)
else: raise AssertionError("Live OCR remained RUNNING")
assert row["status"] == "APPROVED", (row["status"],row.get("reasons"),row.get("error"))
assert row["method"] == "ocr" and row["invoice_number"] == "DEMO-SCAN-1"
assert row["decision"]["po_matching"]["status"] == "MATCHED"
assert row["decision"]["financial_validation"]["status"] == "PASSED"
print("PASS prior run survived redeploy, scanned image OCR approved with matching PO and financial checks")
print("run",rid)

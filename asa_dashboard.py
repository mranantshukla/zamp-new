"""Local AP dashboard. Run: python asa_dashboard.py --port 8766"""
from __future__ import annotations
import argparse, base64, html, json, tempfile, uuid, urllib.request, threading, re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from asa_invoice_processor import decide, extract_invoice, read_document

ROOT = Path(__file__).parent; SAMPLES = ROOT / "samples"
RUNS = ROOT / "run_history.json"; VERIFIED = ROOT / "manual_verification.json"
DATASET_CACHE = ROOT / "dataset_cache.json"
DATASET_IMAGES = ROOT / "dataset_images"
RUN_LOCK = threading.Lock()

def load_json(path, default):
    try: return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception: return default
def load_runs(): return load_json(RUNS, [])
def load_verified(): return load_json(VERIFIED, {})
def save_run(row):
    rows = load_runs(); rows.insert(0, row); RUNS.write_text(json.dumps(rows, indent=2), encoding="utf-8")

def reconcile_legacy_dataset_runs():
    """Backfill decisions for earlier evaluation-only runs from source images."""
    with RUN_LOCK:
        rows = load_runs(); changed = False
        for row in rows:
            if row.get("event_type") != "dataset_evaluation" or row.get("status") != "EVALUATED": continue
            match = re.fullmatch(r"Indian GST invoice (\d{2})", row.get("source") or "")
            if not match: continue
            image_path = DATASET_IMAGES / f"invoice_{int(match.group(1)):02d}.jpg"
            if not image_path.is_file(): continue
            try:
                text, method, _ = read_document(image_path.read_bytes(), ".jpg")
                pred = extract_invoice(text)
                decision = decide(pred, SAMPLES / "pos.csv", RUNS, method=method)
                row.update(status=decision.status, invoice_number=pred.get("invoice_number"),
                           vendor=pred.get("vendor"), gstin=pred.get("gstin"),
                           po_number=pred.get("po_number"), total=pred.get("total"),
                           reasons=decision.reasons)
            except Exception as exc:
                row.update(status="PENDING_REVIEW", reasons=[f"Reprocessing failed: {exc}"])
            changed = True
        if changed: RUNS.write_text(json.dumps(rows, indent=2), encoding="utf-8")

def dataset_rows():
    cached = load_json(DATASET_CACHE, [])
    if cached: return cached
    try:
        url = "https://datasets-server.huggingface.co/rows?dataset=AjitRawat/invoice&config=default&split=train&offset=0&length=100"
        with urllib.request.urlopen(url, timeout=60) as r:
            rows = json.loads(r.read().decode()).get("rows", [])
        if rows: DATASET_CACHE.write_text(json.dumps(rows), encoding="utf-8")
        return rows
    except Exception: return cached

def dataset_image_bytes(index, row):
    """Read a locally cached sample first; fall back to the dataset URL once."""
    DATASET_IMAGES.mkdir(exist_ok=True)
    local = DATASET_IMAGES / f"invoice_{index + 1:02d}.jpg"
    if local.exists() and local.stat().st_size > 1000:
        return local.read_bytes(), None
    try:
        raw = urllib.request.urlopen(row["image"]["src"], timeout=30).read()
        local.write_bytes(raw)
        return raw, None
    except Exception as e:
        return b"", str(e)

def ocr_bytes(data, suffix=".jpg"):
    try: return read_document(data, suffix)[0], None
    except Exception as e: return "", str(e)

LABEL_ALIASES = {
    "invoice_number": ("invoice_number", "invoice_no"), "po_number": ("po_number",),
    "date": ("invoice_date",), "total": ("grand_total", "amount", "total_price"),
    "subtotal": ("taxable_value", "total_value"), "pan": ("pan",),
    "order_number": ("order_no",), "customer_number": ("customer_no", "customer_id"),
    "cgst": ("cgst",), "sgst": ("sgst",), "igst": ("igst",),
}

def evaluate_dataset(pred, header):
    """Compare predictions after OCR. No label enters extraction or decisioning."""
    details = pred.get("details") or {}
    tax = details.get("tax_breakdown") or {}
    values = {**pred, **{k: details.get(k) for k in ("pan", "order_number", "customer_number")}}
    values.update({k: (tax.get(k.upper()) or {}).get("amount") for k in ("cgst", "sgst", "igst")})
    rows = []
    for field, aliases in LABEL_ALIASES.items():
        label = next((header[k] for k in aliases if header.get(k) not in (None, "")), None)
        value = values.get(field)
        if label is None: outcome = "Label unavailable"
        elif value is None: outcome = "Extraction missing"
        else:
            def norm(v):
                try: return f"{float(str(v).replace(',', '').replace('₹','').strip()):.2f}"
                except ValueError: return "".join(str(v).casefold().split()).strip(" .:-")
            outcome = "Exact match" if norm(value) == norm(label) else "Mismatch"
        rows.append((field, value, label, outcome))
    # The dataset does not identify whether gstin_no belongs to buyer or seller.
    rows.append(("gstin (role unspecified)", None, header.get("gstin_no"), "Ambiguous label" if header.get("gstin_no") else "Label unavailable"))
    covered = {key for aliases in LABEL_ALIASES.values() for key in aliases} | {"gstin_no"}
    rows.extend((key, None, value, "Label not mapped to a reliable extraction field") for key, value in header.items() if key not in covered)
    comparable = [r for r in rows if r[3] in ("Exact match", "Mismatch", "Extraction missing")]
    accuracy = sum(r[3] == "Exact match" for r in comparable) / len(comparable) if comparable else None
    return rows, accuracy

def decision_stages(decision):
    checks = {item["check"]: item for item in decision.checks}
    finance = [item for item in decision.checks if item["check"] in ("po_remaining", "invoice_math", "gst_components", "gst_tax_type", "gst_split_rate")]
    return {
        "PO matching": "Matched" if checks.get("po_match", {}).get("passed") else "Needs review",
        "Financial validation": "Flagged" if any(not item["passed"] for item in finance) else "Passed" if finance else "Skipped (insufficient data)",
        "Decision": decision.status,
    }

def parse_form(handler):
    body = handler.rfile.read(int(handler.headers.get("Content-Length", 0)))
    ctype = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype: return parse_qs(body.decode("utf-8")), None
    boundary = ctype.split("boundary=", 1)[1].encode(); fields={}; upload=None
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part: continue
        headers, content = part.split(b"\r\n\r\n", 1); content=content.rstrip(b"\r\n-")
        hs=headers.decode("utf-8", "ignore"); name=None; filename=None
        if 'name="' in hs: name=hs.split('name="',1)[1].split('"',1)[0]
        if 'filename="' in hs: filename=hs.split('filename="',1)[1].split('"',1)[0]
        if name and filename and content: upload=(filename, content)
        elif name: fields[name]=[content.decode("utf-8", "ignore")]
    return fields, upload

def data_preview(filename, data):
    ext=Path(filename).suffix.lower(); mime={".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".pdf":"application/pdf"}.get(ext)
    if not mime: return "<div class='empty-preview'>Preview available after OCR for this file type.</div>"
    src="data:"+mime+";base64,"+base64.b64encode(data).decode()
    if mime.startswith("image/"): return f"<img class='bill-image' src='{src}' alt='Uploaded bill preview'>"
    return f"<iframe class='bill-pdf' src='{src}' title='Uploaded bill preview'></iframe>"

def rich_details_html(invoice):
    details = invoice.get("details") or {}
    fields = [
        ("Seller", details.get("seller") or invoice.get("vendor")),
        ("Buyer", details.get("buyer")),
        ("Buyer GSTIN", details.get("buyer_gstin")),
        ("Invoice date", details.get("invoice_date") or invoice.get("date")),
        ("Due date", details.get("due_date")),
        ("Order number", details.get("order_number")),
        ("Customer number", details.get("customer_number")),
        ("Payment terms", details.get("payment_terms")),
        ("Place of supply", details.get("place_of_supply")),
        ("Tax type", details.get("tax_type")),
        ("PAN", details.get("pan")),
        ("Phone", details.get("phone")),
        ("Email", details.get("email")),
        ("Bank", details.get("bank_name")),
        ("Account last 4", details.get("account_last4")),
        ("IFSC", details.get("ifsc")),
        ("Reverse charge", details.get("reverse_charge")),
        ("Billing address", details.get("billing_address")),
        ("Shipping address", details.get("shipping_address")),
    ]
    field_rows = "".join(f"<tr><td>{html.escape(label)}</td><td>{html.escape(str(value))}</td></tr>" for label, value in fields if value not in (None, "", "-"))
    tax_rows = "".join(f"<tr><td>{html.escape(name)}</td><td>{html.escape(str(item.get('rate') or '-'))}%</td><td>{html.escape(str(item.get('amount') or '-'))}</td></tr>" for name, item in (details.get("tax_breakdown") or {}).items())
    line_rows = "".join(f"<tr><td>{index}</td><td>{html.escape(str(item.get('description') or '-'))}</td></tr>" for index, item in enumerate(invoice.get("line_items") or [], 1))
    return f"<div class='two-col rich-grid'><div><h3>Additional invoice dimensions</h3><table><tr><th>Field</th><th>Value</th></tr>{field_rows or '<tr><td colspan=2 class=muted>No additional fields detected</td></tr>'}</table></div><div><h3>Tax breakdown</h3><table><tr><th>Tax</th><th>Rate</th><th>Amount</th></tr>{tax_rows or '<tr><td colspan=3 class=muted>No tax components detected</td></tr>'}</table><h3>Line-item text</h3><table><tr><th>#</th><th>Description</th></tr>{line_rows or '<tr><td colspan=2 class=muted>No line items detected</td></tr>'}</table></div></div>"

def page(result=None, preview="", activity=None, dataset_preview=""):
    verified=load_verified(); runs=load_runs()
    for row in runs:
        if not row.get("run_id"):
            identity = json.dumps({k: row.get(k) for k in ("time", "scenario", "source", "invoice_number")}, sort_keys=True)
            row["run_id"] = "legacy-" + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
    def esc(value): return html.escape(str(value)) if value not in (None, "") else "-"
    def history_row(r):
        rid = r["run_id"]
        kind = "Dataset sample" if r.get("event_type") == "dataset_evaluation" else "Upload" if r.get("method") else "Legacy run"
        accuracy = f"{r['field_accuracy']:.1%}" if isinstance(r.get("field_accuracy"), (float, int)) else "-"
        reason = "; ".join((r.get("reasons") or [r.get("error") or "-"])[:2])
        cells = [r.get("time"), kind, r.get("source") or r.get("scenario"), r.get("invoice_number"),
                 r.get("vendor"), r.get("gstin"), r.get("po_number"), r.get("total"), r.get("method"), accuracy]
        columns = "".join(f"<td>{esc(value)}</td>" for value in cells)
        status = esc(r.get("status"))
        status_cell = f"<td class='{html.escape(str(r.get('status') or '').lower())}' title='{esc(reason)}'>{status}</td>"
        review = verified.get(rid)
        if isinstance(review, dict):
            summary = f"{esc(review.get('outcome'))} by {esc(review.get('reviewer'))} at {esc(review.get('time'))}"
            if review.get("note"): summary += f" — {esc(review['note'])}"
        elif review:
            summary = f"Legacy check at {esc(review)} (reviewer and outcome not recorded)"
        else:
            summary = "Awaiting manual review"
        review_form = (f"<td><details class='review-detail'><summary>{summary}</summary>"
                       f"<form method='post' class='review-form'><input type='hidden' name='review_run' value='{esc(rid)}'>"
                       "<label>Reviewer name<input name='reviewer' required maxlength='80'></label>"
                       "<label>Outcome<select name='review_outcome'><option value='VERIFIED'>Fields verified</option>"
                       "<option value='NEEDS_CORRECTION'>Needs correction</option></select></label>"
                       "<label>Review note<textarea name='review_note' maxlength='500' placeholder='Evidence or correction needed'></textarea></label>"
                       "<label><input type='checkbox' name='fields_confirmed' value='1'> I compared the extracted fields with the bill</label>"
                       "<button type='submit'>Save review</button></form></details></td>")
        return f"<tr>{columns}{status_cell}<td>{esc(reason)}</td>{review_form}</tr>"
    history = "".join(history_row(r) for r in runs)
    activity_html="".join(f"<div class='activity-row'><span class='dot'></span><span>{html.escape(s)}</span><span class='activity-status'>{html.escape(v)}</span></div>" for s,v in (activity or {}).items())
    result_html=""
    if result:
        inv=result["invoice"]; checks="".join(f"<tr><td>{html.escape(c['check'])}</td><td class='{ 'pass' if c['passed'] else 'fail' }'>{'PASS' if c['passed'] else 'FLAG'}</td><td>{html.escape(c['detail'])}</td></tr>" for c in result["checks"])
        result_html=f"<section class='card'><div class='section-heading'><div><span class='eyebrow'>AUTOMATED REVIEW</span><h2>Extracted details</h2></div><span class='decision {result['status'].lower()}'>{result['status'].replace('_',' ')}</span></div><table><tr><th>Invoice</th><th>Vendor</th><th>GSTIN</th><th>PO</th><th>Date</th><th>Subtotal</th><th>Discount</th><th>Tax</th><th>Total</th></tr><tr><td>{html.escape(str(inv.get('invoice_number') or '-'))}</td><td>{html.escape(str(inv.get('vendor') or '-'))}</td><td>{html.escape(str(inv.get('gstin') or '-'))}</td><td>{html.escape(str(inv.get('po_number') or '-'))}</td><td>{html.escape(str(inv.get('date') or '-'))}</td><td>{inv.get('subtotal') or '-'}</td><td>{inv.get('discount') or 0}</td><td>{inv.get('tax') or 0}</td><td>{inv.get('total') or '-'}</td></tr></table>{rich_details_html(inv)}<h3>Validation evidence</h3><table><tr><th>Rule</th><th>Result</th><th>Evidence</th></tr>{checks}</table><p class='next-action'><b>Next action:</b> {html.escape(result['next_action'])}</p></section>"
    ds=dataset_rows(); options=''.join(f"<option value='{i}'>Indian GST invoice {i+1:02d}</option>" for i in range(len(ds))) or '<option value="">Dataset unavailable</option>'
    markup = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>AP Invoice Intelligence</title><style>
*{{box-sizing:border-box}}body{{margin:0;font:14px Segoe UI,Arial;background:#f5f7fb;color:#132238}}main{{max-width:1240px;margin:0 auto;padding:28px 30px}}.topbar{{display:flex;justify-content:space-between;align-items:center;margin-bottom:26px}}h1{{font-size:25px;margin:0}}h2{{margin:4px 0 16px;font-size:19px}}h3{{font-size:14px;margin:20px 0 9px}}.muted{{color:#718096}}.status{{color:#10845a;background:#e6f8ef;border-radius:20px;padding:7px 12px;font-weight:600}}.grid{{display:grid;grid-template-columns:390px 1fr;gap:20px}}.card{{background:#fff;border:1px solid #e4eaf2;border-radius:16px;padding:22px;box-shadow:0 5px 18px #243b5310;margin-bottom:20px}}.eyebrow{{font-size:11px;font-weight:700;letter-spacing:1px;color:#5d78a3}}label{{display:block;margin:15px 0 7px;font-weight:600}}select,input[type=file]{{width:100%;padding:11px;border:1px solid #cbd5e1;border-radius:8px;background:#fff}}button{{border:0;border-radius:8px;padding:11px 16px;background:#1668df;color:#fff;font-weight:700;cursor:pointer;margin-top:14px}}button.secondary{{background:#eef4ff;color:#1668df}}hr{{border:0;border-top:1px solid #e6ebf2;margin:22px 0}}.activity{{background:#0e1724;border-radius:12px;padding:18px;color:#dce8f7;min-height:260px}}.activity-row{{display:flex;gap:10px;padding:10px 0;border-bottom:1px solid #263447;align-items:center}}.dot{{height:9px;width:9px;border-radius:50%;background:#28c98b}}.activity-status{{margin-left:auto;color:#91a5c0;font-size:12px}}.bill-image,.bill-pdf{{width:100%;height:380px;object-fit:contain;border:1px solid #d9e1eb;border-radius:10px;background:#f8fafc}}.bill-pdf{{border:0}}.empty-preview{{height:380px;display:grid;place-items:center;background:#f8fafc;border:1px dashed #cbd5e1;border-radius:10px;color:#718096}}table{{width:100%;border-collapse:collapse;display:block;overflow-x:auto}}th,td{{padding:10px 9px;border-bottom:1px solid #e9eef5;text-align:left;white-space:nowrap}}th{{font-size:12px;text-transform:uppercase;color:#687b96;background:#f8fafc}}.approved,.pass{{color:#10845a;background:#e6f8ef}}.rejected,.fail{{color:#bd3b46;background:#fff0f1}}.pending_review{{color:#9b6a00;background:#fff6dc}}.decision{{padding:9px 12px;border-radius:8px;font-weight:800}}.section-heading{{display:flex;justify-content:space-between;align-items:center}}.next-action{{background:#f5f8fc;padding:12px;border-radius:8px}}.toggle{{margin:0}}.toggle-button{{margin:0;padding:6px 10px;background:#eef4ff;color:#1668df;font-size:12px}}.dataset-note{{font-size:12px;color:#718096;line-height:1.5}}.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}@media(max-width:850px){{.grid,.two-col{{grid-template-columns:1fr}}}}
</style></head><body><main><div class='topbar'><div><h1>AP Invoice Intelligence</h1><div class='muted'>Payment-operations control center</div></div><span class='status'>● System online</span></div><div class='grid'><section class='card'><span class='eyebrow'>INPUT</span><h2>Upload Bill</h2><form method='post' enctype='multipart/form-data'><label>Upload from computer</label><input type='file' name='invoice_file' accept='.pdf,.png,.jpg,.jpeg' required><button>Fetch details</button></form><hr><span class='eyebrow'>OR USE A SAMPLE</span><form method='post'><label>Select from dataset</label><select name='dataset_index'>{options}</select><button class='secondary'>Fetch dataset bill</button></form><p class='dataset-note'>PDFs and images only. OCR reads the document; text paste is intentionally not supported.</p></section><section class='card'><span class='eyebrow'>RUN MONITOR</span><h2>What is happening?</h2><div class='activity'>{activity_html or '<div class="muted">Fetch a bill to see its preview, OCR, field extraction, PO matching, and validation activity.</div>'}</div></section></div>{(f"<section class='card'><div class='section-heading'><div><span class='eyebrow'>DOCUMENT PREVIEW</span><h2>Bill being processed</h2></div><span class='muted'>Original input</span></div>{preview}</section>" if preview else '')}{dataset_preview}{result_html}<section class='card'><div class='section-heading'><div><span class='eyebrow'>AUDIT TRAIL</span><h2>Processing history</h2></div><span class='muted'>Manual verification is reversible</span></div><table><tr><th>Time</th><th>Source</th><th>Status</th><th>Invoice</th><th>Verified</th></tr>{history or '<tr><td colspan=5 class="muted">No runs yet</td></tr>'}</table></section></main></body></html>"""

    # A separate stylesheet keeps the visual design readable without changing
    # the server's existing upload, sample, and verification routes.
    styles = (ROOT / "dashboard.css").read_text(encoding="utf-8")
    markup = markup.replace("</head>", f"<style>{styles}</style></head>", 1)
    approved = sum(r.get("status") == "APPROVED" for r in runs)
    rejected = sum(r.get("status") == "REJECTED" for r in runs)
    pending = sum(r.get("status") == "PENDING_REVIEW" for r in runs)
    failed = sum(r.get("status") == "FAILED" for r in runs)
    review = rejected + pending + failed
    completed = approved + rejected + pending
    approval_rate = f"{approved / completed:.1%}" if completed else "—"
    def queue_row(r):
        return f"<tr><td>{esc(r.get('invoice_number'))}</td><td>{esc(r.get('vendor'))}</td><td>{esc(r.get('total'))}</td><td><span class='decision {str(r.get('status') or '').lower()}'>{esc(r.get('status')).replace('_',' ')}</span></td><td><a href='#history'>Open in register →</a></td></tr>"
    queue = "".join(queue_row(r) for r in runs if r.get("status") == "PENDING_REVIEW")[:5000]
    queue = queue or "<tr><td colspan='5' class='muted'>No invoices currently require review.</td></tr>"
    max_decision = max(approved, rejected, pending, failed, 1)
    distribution = "".join(
        f"<div class='bar-row'><span>{label}</span><div class='bar-track'><i style='width:{count / max_decision * 100:.0f}%'></i></div><b>{count}</b></div>"
        for label, count in (("Approved", approved), ("Pending review", pending), ("Rejected", rejected), ("Failed", failed))
    )
    shell = (
        "<body><div class='app-shell'><aside class='sidebar'>"
        "<div class='brand'><span class='brand-mark'>Z</span><div><div class='brand-name'>zamp<span style='color:#ffec72'>.</span></div><div class='brand-sub'>AP case study</div></div></div>"
        "<nav aria-label='Workspace navigation'><div class='nav-label'>Workspace</div><div class='nav-links'>"
        "<a class='active' href='#overview'><span class='nav-icon'>◈</span><span class='nav-text'>Overview</span></a>"
        "<a href='#new-bill'><span class='nav-icon'>⊕</span><span class='nav-text'>New bill</span></a>"
        "<a href='#run-monitor'><span class='nav-icon'>▤</span><span class='nav-text'>Run monitor</span></a>"
        "<a href='#review-queue'><span class='nav-icon'>!</span><span class='nav-text'>Review queue</span></a>"
        "<a href='#history'><span class='nav-icon'>◷</span><span class='nav-text'>Audit trail</span></a>"
        "</div></nav><div class='sidebar-foot'><b>Invoice operations</b><br>Review every bill before payment approval.</div>"
        "</aside><main class='content' id='overview'>"
        "<div class='topbar'><div class='breadcrumb'>Workspace / Accounts payable / Overview</div>"
        "<div class='top-actions'><span class='status'>● Local demo online</span><span class='avatar' aria-hidden='true'>AP</span></div></div>"
        "<section class='hero'><div class='hero-copy'><span class='hero-kicker'>Invoice command center</span>"
        "<h1>Your bills, under control.</h1><p>Upload, inspect and verify invoices in one workspace.</p></div>"
        "<div class='hero-art' aria-hidden='true'>✦</div></section>"
        "<div class='metrics'>"
        f"<div class='metric'><div class='metric-label'>Invoices processed</div><div class='metric-value'>{len(runs)}</div><div class='metric-note'>Recorded in history</div></div>"
        f"<div class='metric'><div class='metric-label'>Auto-approval rate</div><div class='metric-value'>{approval_rate}</div><div class='metric-note'>Completed decisions</div></div>"
        f"<div class='metric'><div class='metric-label'>Pending reviews</div><div class='metric-value'>{pending}</div><div class='metric-note'>Human attention required</div></div>"
        f"<div class='metric'><div class='metric-label'>Rejected</div><div class='metric-value'>{rejected}</div><div class='metric-note'>Confirmed violations</div></div>"
        f"<div class='metric'><div class='metric-label'>Failures</div><div class='metric-value'>{failed}</div><div class='metric-note'>Processing did not finish</div></div>"
        "<div class='metric'><div class='metric-label'>Avg processing time</div><div class='metric-value'>—</div><div class='metric-note'>Timing not recorded</div></div>"
        "</div><div class='workspace-grid'>"
    )
    markup = markup.replace(
        "<body><main><div class='topbar'><div><h1>AP Invoice Intelligence</h1><div class='muted'>Payment-operations control center</div></div><span class='status'>● System online</span></div><div class='grid'>",
        shell, 1,
    )
    analytics = ("<div class='two-col analytics-grid'>"
                 "<section class='card'><div class='section-heading'><div><span class='eyebrow'>DECISION MIX</span><h2>Processing outcomes</h2></div><span class='muted'>Live history</span></div>"
                 f"<div class='bar-chart'>{distribution}</div></section>"
                 "<section class='card' id='review-queue'><div class='section-heading'><div><span class='eyebrow'>WORK QUEUE</span><h2>Review queue</h2></div><span class='decision pending_review'>"
                 f"{pending} open</span></div><p class='section-sub'>Invoices with uncertain extraction or evidence that needs a human decision.</p><div class='table-wrap'><table><tr><th>Invoice</th><th>Vendor</th><th>Amount</th><th>Status</th><th>Action</th></tr>{queue}</table></div></section></div>")
    markup = markup.replace("</main>", analytics + "</main>", 1)
    markup = markup.replace("<section class='card'><span class='eyebrow'>INPUT</span><h2>Upload Bill</h2>",
                            "<section class='card' id='new-bill'><span class='eyebrow'>CREATE A NEW RUN</span><h2>Add a bill</h2><p class='section-sub'>Choose a PDF or image, or try a real GST invoice sample.</p>", 1)
    markup = markup.replace("<form method='post' enctype='multipart/form-data'><label>Upload from computer</label><input type='file' name='invoice_file' accept='.pdf,.png,.jpg,.jpeg' required><button>Fetch details</button></form>",
                            "<form method='post' enctype='multipart/form-data'><div class='upload-zone'><div class='upload-icon'>↑</div><label class='form-label' for='invoice_file'>Upload from computer</label><input id='invoice_file' type='file' name='invoice_file' accept='.pdf,.png,.jpg,.jpeg' required></div><button class='primary'>Fetch bill details →</button></form>", 1)
    markup = markup.replace("<hr><span class='eyebrow'>OR USE A SAMPLE</span>", "<div class='separator'>OR SELECT A SAMPLE</div>", 1)
    markup = markup.replace("<label>Select from dataset</label><select name='dataset_index'>", "<label class='sample-label' for='dataset_index'>Indian GST dataset</label><select id='dataset_index' name='dataset_index'>", 1)
    markup = markup.replace("<section class='card'><span class='eyebrow'>RUN MONITOR</span><h2>What is happening?</h2>",
                            "<section class='card' id='run-monitor'><span class='eyebrow'>LIVE ACTIVITY</span><h2>Run monitor</h2><p class='section-sub'>Follow the bill from capture through validation.</p>", 1)
    markup = markup.replace("<div class=\"muted\">Fetch a bill to see its preview, OCR, field extraction, PO matching, and validation activity.</div>",
                            "<div class='run-empty'><span>▧</span>Pick a bill to start processing.<br>Its checks will appear here.</div>")
    markup = markup.replace("<section class='card'><div class='section-heading'><div><span class='eyebrow'>AUDIT TRAIL</span>",
                            "<section class='card' id='history'><div class='section-heading'><div><span class='eyebrow'>AUDIT TRAIL</span>", 1)
    markup = markup.replace("</main></body></html>", "</main></div></body></html>", 1)
    markup = markup.replace("Manual verification is reversible", "Document check only; payment approval is separate")
    markup = markup.replace("<th>Time</th><th>Source</th><th>Status</th><th>Invoice</th><th>Verified</th>",
                            "<th>Time</th><th>Type</th><th>Source</th><th>Invoice</th><th>Vendor</th><th>Seller GSTIN</th><th>PO</th><th>Total</th><th>Method</th><th>Field accuracy</th><th>Decision</th><th>Reason</th><th>Manual check</th>")
    markup = markup.replace('colspan=5 class="muted"', 'colspan=13 class="muted"')
    markup = markup.replace("☑ Verified", "☑ Checked").replace("☐ Not verified", "☐ Unchecked")
    return markup

class Handler(BaseHTTPRequestHandler):
    def do_GET(self): self.respond(page())
    def do_POST(self):
        data,upload=parse_form(self)
        if "review_run" in data:
            rid=data["review_run"][0]
            reviewer=data.get("reviewer",[""])[0].strip()
            outcome=data.get("review_outcome",[""])[0]
            note=data.get("review_note",[""])[0].strip()
            confirmed=data.get("fields_confirmed",[""])[0] == "1"
            known={r.get("run_id") or "legacy-" + uuid.uuid5(uuid.NAMESPACE_URL,json.dumps({k:r.get(k) for k in ("time","scenario","source","invoice_number")},sort_keys=True)).hex for r in load_runs()}
            if rid not in known or not reviewer or len(reviewer)>80 or len(note)>500 or outcome not in ("VERIFIED","NEEDS_CORRECTION") or not confirmed or (outcome=="NEEDS_CORRECTION" and not note):
                self.respond(page(activity={"Manual review":"Could not save: enter a reviewer, confirm field comparison, and add a note for corrections"})); return
            with RUN_LOCK:
                values=load_verified()
                values[rid]={"time":datetime.now().isoformat(timespec="seconds"),"reviewer":reviewer,"outcome":outcome,"note":note,"fields_confirmed":True}
                VERIFIED.write_text(json.dumps(values,indent=2),encoding="utf-8")
            self.respond(page(activity={"Manual review":f"Saved {outcome}"})); return
        if "dataset_index" in data:
            ds=dataset_rows(); idx=int(data["dataset_index"][0]); dp=""; result=None
            if 0 <= idx < len(ds):
                row=ds[idx]["row"]; raw,error=dataset_image_bytes(idx,row)
                try:
                    text,method,_=read_document(raw,".jpg") if raw else ("", "failed", 0)
                    if not raw: raise RuntimeError(error or "Image unavailable")
                    pred=extract_invoice(text)
                    activity={"Document received":"Completed","RapidOCR":"Completed","Field extraction":"Completed","Dataset evaluation":"Completed"}
                except Exception as e:
                    text=""; pred={}; error=str(e)
                    activity={"Document received":"Completed" if raw else "Failed","RapidOCR":f"Failed: {error}","Field extraction":"Skipped","Dataset evaluation":"Skipped"}
                label=json.loads(row["ground_truth"]).get("gt_parse",{}).get("header",{})
                comparisons,accuracy=evaluate_dataset(pred,label)
                comparison="".join(f"<tr><td>{html.escape(k)}</td><td>{html.escape(str(v) if v is not None else '-')}</td><td>{html.escape(str(l) if l is not None else '-')}</td><td>{html.escape(outcome)}</td></tr>" for k,v,l,outcome in comparisons)
                score=f"{sum(r[3]=='Exact match' for r in comparisons)}/{sum(r[3] in ('Exact match','Mismatch','Extraction missing') for r in comparisons)} ({accuracy:.1%})" if accuracy is not None else "No comparable labels"
                image="data:image/jpeg;base64," + base64.b64encode(raw).decode() if raw else ""
                image_markup=f"<img class='bill-image' src='{image}' alt='Selected dataset bill'>" if image else f"<div class='empty-preview'>Sample image unavailable: {html.escape(error or 'unknown error')}</div>"
                dp=f"<section class='card'><div class='two-col'><div><span class='eyebrow'>SOURCE IMAGE</span><h2>Selected dataset bill</h2>{image_markup}</div><div><span class='eyebrow'>OCR VS LABEL</span><h2>Fetched details</h2><p>Field-level exact-match accuracy: {score}</p><table><tr><th>Field</th><th>OCR</th><th>Label</th><th>Outcome</th></tr>{comparison}</table><details><summary>View OCR text</summary><pre>{html.escape(text or error or 'No OCR text')}</pre></details></div></div>{rich_details_html(pred) if pred else ''}</section>"
                source=f"Indian GST invoice {idx + 1:02d}"
                with RUN_LOCK:
                    decision=decide(pred,SAMPLES/"pos.csv",RUNS,method="ocr") if pred else None
                    if decision:
                        result={"status":decision.status,"invoice":decision.invoice,"checks":decision.checks,"next_action":decision.next_action}
                        activity.update(decision_stages(decision))
                    else:
                        activity["Decision"]="PENDING_REVIEW (extraction failed)"
                    save_run({"run_id":str(uuid.uuid4()),"time":datetime.now().isoformat(timespec="seconds"),
                              "event_type":"dataset_evaluation","source":source,
                              "status":decision.status if decision else "PENDING_REVIEW",
                              "invoice_number":pred.get("invoice_number"),"vendor":pred.get("vendor"),
                              "gstin":pred.get("gstin"),"po_number":pred.get("po_number"),"total":pred.get("total"),
                              "method":"ocr" if pred else "failed","field_accuracy":accuracy,
                              "reasons":decision.reasons if decision else [error or "Extraction failed"],
                              **({"error":error} if not pred else {})})
            self.respond(page(result=result,dataset_preview=dp,activity=activity if 0 <= idx < len(ds) else None)); return
        if not upload: self.respond(page()); return
        filename,raw=upload; suffix=Path(filename).suffix.lower(); preview=data_preview(filename,raw)
        activity={"Document received":"Completed","Bill preview loaded":"Completed"}
        try:
            text,method,_=read_document(raw,suffix)
            activity["OCR"]="Completed" if "ocr" in method else "Skipped (embedded PDF text)"
            activity["Text extraction"]="Completed"
            inv=extract_invoice(text); activity["Field extraction"]="Completed"
            with RUN_LOCK:
                d=decide(inv,SAMPLES/"pos.csv",RUNS,method=method)
                rid=str(uuid.uuid4()); result={"run_id":rid,"status":d.status,"invoice":d.invoice,"checks":d.checks,"next_action":d.next_action}
                save_run({"run_id":rid,"time":datetime.now().isoformat(timespec="seconds"),"source":filename,"status":d.status,"invoice_number":inv.get("invoice_number"),"vendor":inv.get("vendor"),"gstin":inv.get("gstin"),"po_number":inv.get("po_number"),"total":inv.get("total"),"method":method,"reasons":d.reasons})
            activity.update(decision_stages(d))
            self.respond(page(result,preview,activity))
        except Exception as e:
            activity.update({"OCR / text extraction":f"Failed: {e}","Field extraction":"Skipped","Decision":"PENDING_REVIEW"})
            rid=str(uuid.uuid4()); save_run({"run_id":rid,"time":datetime.now().isoformat(timespec="seconds"),"source":filename,"status":"PENDING_REVIEW","invoice_number":None,"error":str(e)})
            self.respond(page(preview=preview,activity=activity))
    def respond(self,body):
        data=body.encode(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)

if __name__=="__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--port",type=int,default=8766); args=ap.parse_args()
    from app import app
    print(f"Open http://127.0.0.1:{args.port}")
    app.run(host="127.0.0.1", port=args.port, threaded=True)

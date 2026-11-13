"""Production web entry point; the legacy dashboard CLI remains available."""
from __future__ import annotations

import hmac
import hashlib
import json
import logging
import os
import threading
import time
import csv
import gzip
import io
import zipfile
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeout
from xml.sax.saxutils import escape as xml_escape
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, render_template, render_template_string, request, session, url_for
from werkzeug.utils import secure_filename

import storage
import master_data
import workflow
import asa_invoice_processor as processor
from asa_invoice_processor import classify_document, extract_invoice, read_document
from asa_dashboard import dataset_rows, dataset_image_bytes, evaluate_dataset
from ai_review import review_invoice
from ai_extraction import _call_model, reconcile_ocr, read_document_with_ai

ROOT = Path(__file__).parent
app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET", "local-development-secret-change-before-deployment")
app.config.update(MAX_CONTENT_LENGTH=8 * 1024 * 1024,
                  PERMANENT_SESSION_LIFETIME=timedelta(days=7),
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("asa")
if os.getenv("RENDER"):
    missing = [key for key in ("APP_SECRET", "DEMO_USERNAME", "DEMO_PASSWORD") if not os.getenv(key)]
    if missing:
        raise RuntimeError("Missing required deployment variables: " + ", ".join(missing))
if os.getenv("RENDER"):
    # Render terminates TLS at one proxy hop; trust exactly that hop so login
    # throttling keys on the client address rather than the proxy's.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
storage.initialize()
storage.recover_interrupted()

# OCR inference is already serialized by a process-wide lock, so extra threads
# only add memory pressure. A bounded pool queues batch uploads instead of
# starting up to 25 concurrent threads that each hold document bytes.
PROCESSING_WORKERS = max(1, min(8, int(os.getenv("PROCESSING_WORKERS", "2"))))
executor = ThreadPoolExecutor(max_workers=PROCESSING_WORKERS, thread_name_prefix="asa-run")

def submit_run(*args):
    executor.submit(process_run, *args)

LOGIN_WINDOW_SECONDS, LOGIN_MAX_FAILURES = 300, 5
login_failures = defaultdict(deque)
login_lock = threading.Lock()

def _login_blocked(key):
    cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
    with login_lock:
        attempts = login_failures[key]
        while attempts and attempts[0] < cutoff: attempts.popleft()
        if not attempts: login_failures.pop(key, None)
        return len(attempts) >= LOGIN_MAX_FAILURES

# The secret is deliberately runtime-only: it is never sent back to the browser,
# stored in run history, or committed to source control.  Environment variables
# are the persistent deployment configuration; the dashboard form is useful for
# a local demo session.
AI_RUNTIME_CONFIG = {
    "endpoint": os.getenv("AI_REVIEW_API_URL", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions").strip(),
    "model": os.getenv("AI_REVIEW_MODEL", "gemini-3.1-flash-lite").strip(),
    "api_key": os.getenv("AI_REVIEW_API_KEY") or os.getenv("GEMINI_API_KEY", ""),
}

STAGE_NAMES = ["Document received", "PDF text / OCR", "Document classification", "Field extraction",
               "AI OCR reconciliation", "Extraction validation", "Vendor verification", "PO matching", "Financial validation",
               "Duplicate detection", "AP decision", "AI evidence review", "History persistence"]

def logged_in():
    return not os.getenv("DEMO_USERNAME") or session.get("authenticated") is True

@app.before_request
def guard():
    if request.path in ("/healthz", "/login") or request.path.startswith("/static/"): return
    if not logged_in(): return redirect(url_for("login"))
    if request.method in ("POST", "PUT", "DELETE"):
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf")
        if not token or not hmac.compare_digest(token, session.get("csrf") or ""):
            abort(403)

COMPRESSIBLE = ("application/json", "text/html", "text/csv", "text/css", "application/javascript", "text/javascript")

@app.after_request
def finalize_response(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Compress large text responses: /api/runs is the dashboard's heaviest call.
    if (response.direct_passthrough or response.is_streamed or response.status_code < 200
            or response.status_code >= 300 or "Content-Encoding" in response.headers
            or response.mimetype not in COMPRESSIBLE
            or "gzip" not in request.headers.get("Accept-Encoding", "").lower()):
        return response
    data = response.get_data()
    if len(data) < 1024: return response
    response.set_data(gzip.compress(data, compresslevel=5))
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(response.get_data()))
    response.vary.add("Accept-Encoding")
    return response

@app.route("/healthz")
def healthz():
    try:
        with storage.engine.connect() as conn: conn.exec_driver_sql("SELECT 1")
        return jsonify(status="ok")
    except Exception: return jsonify(status="unavailable"), 503

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        key = request.remote_addr or "unknown"
        if _login_blocked(key):
            return render_template_string(LOGIN_HTML, error="Too many failed attempts. Try again in a few minutes.", demo_username=os.getenv("DEMO_USERNAME", "")), 429
        user = os.getenv("DEMO_USERNAME", "")
        password = os.getenv("DEMO_PASSWORD", "")
        if user and password and hmac.compare_digest(request.form.get("username", ""), user) and hmac.compare_digest(request.form.get("password", ""), password):
            session.clear(); session["authenticated"] = True
            session.permanent = True
            session["csrf"] = os.urandom(24).hex()
            with login_lock: login_failures.pop(key, None)
            return redirect(url_for("index"))
        with login_lock: login_failures[key].append(time.monotonic())
        return render_template_string(LOGIN_HTML, error="Invalid credentials", demo_username=user), 401
    return render_template_string(LOGIN_HTML, error="", demo_username=os.getenv("DEMO_USERNAME", ""))

@app.post("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

def validate_file(filename, raw):
    name = secure_filename(filename or "document")[:180]
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf" and raw.startswith(b"%PDF-"): return name, "application/pdf"
    if suffix == ".png" and raw.startswith(b"\x89PNG\r\n\x1a\n"): return name, "image/png"
    if suffix in (".jpg", ".jpeg") and raw.startswith(b"\xff\xd8\xff"): return name, "image/jpeg"
    raise ValueError("Upload a valid PDF, PNG or JPEG invoice")

def stage(rid, name, status, detail=""):
    storage.add_stage(rid, name, status, detail)

def document_routing(kind: str) -> dict:
    """Keep document handling separate from AP payment eligibility."""
    if kind == "TAX_INVOICE":
        return {"status": "INVOICE_AP_WORKFLOW", "reason": "Tax invoice routed to AP validation"}
    if kind in {"PURCHASE_ORDER", "DELIVERY_CHALLAN", "CREDIT_NOTE", "DEBIT_NOTE", "PROFORMA_INVOICE"}:
        return {"status": "NON_INVOICE", "reason": f"{kind.replace('_', ' ').title()} routed outside invoice approval"}
    return {"status": "UNREADABLE_OR_UNKNOWN", "reason": "Document type cannot be routed to automatic invoice approval"}

def read_document_cached(raw, suffix):
    version = f"ocr-v2:{suffix.lower()}:{processor.OCR_PDF_DPI}:{processor.OCR_ENHANCED_PASS}:1"
    key = hashlib.sha256(version.encode() + b"\0" + raw).hexdigest()
    cached = storage.cached_ocr(key)
    if cached is not None:
        return (*cached, True)
    result = read_document(raw, suffix)
    storage.save_ocr(key, result)
    return (*result, False)

ocr_reader_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-reader")
ai_reader_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-reader")


def read_parallel(raw, suffix):
    """Race independent readers for scans; a usable text PDF avoids model cost."""
    if not all(AI_RUNTIME_CONFIG.get(k) for k in ("endpoint", "model", "api_key")):
        return (*read_document_cached(raw, suffix), None)
    if suffix.lower() == ".pdf":
        import fitz
        with fitz.open(stream=raw, filetype="pdf") as doc:
            text_layer = "\n".join(page.get_text() for page in doc)
        if len(text_layer.strip()) >= 80:
            return (*read_document_cached(raw, suffix), None)
    readers = {ocr_reader_pool.submit(read_document_cached, raw, suffix): "ocr",
               ai_reader_pool.submit(read_document_with_ai, raw, suffix, dict(AI_RUNTIME_CONFIG)): "ai"}
    best_ocr, best_ai, errors = None, None, []
    try:
        for future in as_completed(readers, timeout=65):
            try:
                result = future.result()
                if readers[future] == "ai":
                    best_ai = result
                    if result.get("status") != "COMPLETED": continue
                    text, method, pages, cached = result["text"], "ai_vision", result["pages"], False
                else:
                    best_ocr = result
                    text, method, pages, cached = result
                fields = extract_invoice(text)
                usable = fields.get("total") is not None and fields.get("vendor") and fields.get("subtotal") is not None
                if usable:
                    return text, method, pages, cached, result if readers[future] == "ai" else None
            except Exception as exc:
                errors.append(type(exc).__name__)
        if best_ai and best_ai.get("status") == "COMPLETED":
            return best_ai["text"], "ai_vision", best_ai["pages"], False, best_ai
        if best_ocr: return (*best_ocr, best_ai)
        raise RuntimeError("Both document readers failed: " + ", ".join(errors))
    except FutureTimeout:
        if best_ocr: return (*best_ocr, best_ai)
        if best_ai and best_ai.get("status") == "COMPLETED": return best_ai["text"], "ai_vision", best_ai["pages"], False, best_ai
        raise RuntimeError("Document reading deadline exceeded")
    finally:
        for future in readers: future.cancel()


def process_run(rid, raw, suffix, event_type="upload", label=None):
    started = time.monotonic()
    current = "Document received"
    try:
        for name in STAGE_NAMES: stage(rid, name, "PENDING")
        stage(rid, current, "RUNNING")
        stage(rid, current, "COMPLETED", f"{len(raw)} bytes received")
        current = "PDF text / OCR"; stage(rid, current, "RUNNING")
        ocr_started = time.monotonic()
        ocr_error = None
        try:
            text, method, pages, cache_hit, vision = read_parallel(raw, suffix)
        except Exception as exc:
            ocr_error = type(exc).__name__
            text, method, pages, cache_hit = "", "ocr_failed", 0, False
        preliminary = extract_invoice(text)
        vision = None if ocr_error else vision
        if vision is None and (ocr_error or preliminary.get("subtotal") is None or not preliminary.get("total") or sum(bool(preliminary.get(k)) for k in ("vendor", "invoice_number", "date", "total")) < 3):
            vision = read_document_with_ai(raw, suffix, AI_RUNTIME_CONFIG)
            if vision["status"] == "COMPLETED":
                text, method, pages = vision["text"], "ai_vision", vision["pages"]
            elif ocr_error:
                raise RuntimeError("OCR failed and AI fallback is " + vision["status"].lower())
        ocr_seconds = time.monotonic() - ocr_started
        stage(rid, current, "COMPLETED", f"{method}; {pages} page(s); {ocr_seconds:.2f}s" + ("; cached" if cache_hit else ""))
        current = "Document classification"; stage(rid, current, "RUNNING")
        kind = classify_document(text)
        routing = document_routing(kind)
        stage(rid, current, "COMPLETED", f"{kind}; {routing['reason']}")
        current = "Field extraction"; stage(rid, current, "RUNNING")
        invoice = extract_invoice(text)
        if vision and vision["status"] == "COMPLETED":
            invoice["details"].update({k: vision[k] for k in ("bill_kind", "already_paid") if k in vision})
            for metadata in invoice.get("field_metadata", {}).values():
                metadata.update(method="ai_vision", reliability="CONTEXTUAL", validation_status="AI_TRANSCRIBED")
            if kind == "UNKNOWN" and vision.get("bill_kind") and invoice.get("total") is not None:
                kind = invoice["document_type"] = "TAX_INVOICE"
                routing = document_routing(kind)
        stage(rid, current, "COMPLETED", f"{sum(bool(invoice.get(k)) for k in ('invoice_number','vendor','po_number','date','total'))}/5 key fields detected")
        current = "AI OCR reconciliation"; stage(rid, current, "RUNNING")
        # Optional: reconcile OCR text before decisioning. Dataset labels are
        # intentionally unavailable here and unsupported proposals are dropped.
        ai_started = time.monotonic()
        if vision and vision["status"] == "COMPLETED":
            ai_extraction = {"status": "COMPLETED", "source": "original_page_images", "model": vision["model"],
                             "accepted_fields": [k for k in ("vendor", "invoice_number", "date", "total") if invoice.get(k)]}
        elif vision and vision.get("status") == "UNAVAILABLE":
            ai_extraction = {**vision, "accepted_fields": []}
        elif routing["status"] != "INVOICE_AP_WORKFLOW":
            ai_extraction = {"status": "SKIPPED", "accepted_fields": [], "reason": routing["reason"]}
        else:
            invoice, ai_extraction = reconcile_ocr(invoice, text, AI_RUNTIME_CONFIG)
        ai_extraction_seconds = time.monotonic() - ai_started
        detected = sum(bool(invoice.get(k)) for k in ("invoice_number", "vendor", "po_number", "date", "total"))
        if ai_extraction["status"] == "COMPLETED":
            stage(rid, current, "COMPLETED", f"{len(ai_extraction['accepted_fields'])} OCR-backed field(s) accepted; {detected}/5 key fields detected; {ai_extraction_seconds:.2f}s")
        elif ai_extraction["status"] in ("NOT_CONFIGURED", "SKIPPED"):
            stage(rid, current, "SKIPPED", ai_extraction.get("reason", "No AI extraction model configured; deterministic OCR retained"))
        else:
            stage(rid, current, "BLOCKED", "AI extraction unavailable: " + ai_extraction.get("error", "Unknown provider error"))
        current = "Extraction validation"; stage(rid, current, "RUNNING")
        missing = [k for k in ("invoice_number", "vendor", "date", "total") if not invoice.get(k)]
        stage(rid, current, "BLOCKED" if missing else "COMPLETED", "Missing: " + ", ".join(missing) if missing else "Key fields present")
        # Each following stage records the real decision subsystem's result.
        current = "Vendor verification"; stage(rid, current, "RUNNING")
        current = "PO matching"; stage(rid, current, "RUNNING")
        current = "Financial validation"; stage(rid, current, "RUNNING")
        current = "Duplicate detection"; stage(rid, current, "RUNNING")
        current = "AP decision"; stage(rid, current, "RUNNING")
        current = "AI evidence review"; stage(rid, current, "RUNNING")
        current = "History persistence"; stage(rid, current, "RUNNING")
        # Save the decision, advisory review, benchmark and final stages in one
        # transaction. A terminal status is never visible with a partial run.
        def finalize(decision):
            ai_review = review_invoice(invoice, decision)
            checks = {c["check"]: c for c in decision.checks}
            # Unreadable/unknown documents still need a person; only filed
            # non-invoices (challans, POs, credit notes) leave the queue.
            review_route = workflow.route(decision.status, decision.checks) if routing["status"] != "NON_INVOICE" else None
            extra = {"ai_extraction": ai_extraction, "ai_review": ai_review, "review_route": review_route,
                     "document_routing": routing,
                     "timings": {"ocr_seconds": round(ocr_seconds, 2),
                                 "ai_extraction_seconds": round(ai_extraction_seconds, 2),
                                 "total_seconds": round(time.monotonic()-started, 2)},
                     "ocr_cache_hit": cache_hit}
            if event_type == "dataset_evaluation" and label is not None:
                comparisons, accuracy = evaluate_dataset(invoice, label)
                extra.update(field_accuracy=accuracy, comparisons=comparisons)
            final_stages = [
                ("Vendor verification", "COMPLETED" if checks.get("vendor_master", {}).get("passed") else "BLOCKED",
                 checks.get("vendor_master", {}).get("detail", "Vendor unverified")),
                ("PO matching", "COMPLETED" if decision.po_matching["status"] in ("MATCHED", "INFERRED") else "BLOCKED",
                 decision.po_matching["explanation"]),
                ("Financial validation", "COMPLETED" if decision.financial_validation["status"] == "PASSED" else "BLOCKED",
                 decision.financial_validation["status"]),
                ("Duplicate detection", "BLOCKED" if not checks["duplicate_detection"]["passed"] else "COMPLETED",
                 checks["duplicate_detection"]["detail"]),
                ("AP decision", "COMPLETED", decision.status),
                ("AI evidence review", "COMPLETED", ai_review["recommendation"]),
                ("History persistence", "COMPLETED", f"Run completed in {time.monotonic()-started:.2f}s"),
            ]
            return extra, final_stages
        storage.locked_decision(rid, invoice, ROOT / "samples" / "pos.csv", method, finalize=finalize,
                                masters=master_data.decision_masters(ROOT / "samples"))
    except Exception as exc:
        log.exception("run %s failed at %s", rid, current)
        stage(rid, current, "FAILED", str(exc)[:240])
        try:
            seen = {s["stage"] for s in storage.get_run(rid)["stages"] if s["status"] in ("COMPLETED","BLOCKED","FAILED","SKIPPED")}
            for name in STAGE_NAMES:
                if name not in seen: stage(rid, name, "SKIPPED", "Earlier stage failed")
        except Exception: log.exception("could not close pending stages for %s", rid)
        storage.finish_run(rid, {"error":str(exc)[:240], "reasons":["Processing failed; document requires review"]}, "FAILED")

@app.post("/api/runs")
def create_upload():
    file = request.files.get("invoice_file")
    if not file: return jsonify(error="Choose a file"), 400
    raw = file.read(8*1024*1024 + 1)
    if len(raw) > 8*1024*1024: return jsonify(error="File exceeds 8 MB"), 413
    try: name, mime = validate_file(file.filename, raw)
    except ValueError as exc: return jsonify(error=str(exc)), 400
    rid = storage.create_run(name, "upload", raw, mime)
    submit_run(rid, raw, Path(name).suffix)
    return jsonify(run_id=rid), 202

@app.post('/api/runs/bulk')
def create_bulk_upload():
    files = request.files.getlist('invoice_files')
    if not files or len(files) > 25:
        return jsonify(error='Select 1 to 25 documents'), 400
    prepared = []
    for file in files:
        raw = file.read(8*1024*1024 + 1)
        if len(raw) > 8*1024*1024: return jsonify(error=f'{file.filename}: exceeds 8 MB'), 413
        try: name, mime = validate_file(file.filename, raw)
        except ValueError as exc: return jsonify(error=f'{file.filename}: {exc}'), 400
        prepared.append((name, mime, raw))
    created = []
    for name, mime, raw in prepared:
        rid = storage.create_run(name, 'upload', raw, mime)
        created.append({'run_id': rid, 'source': name})
        submit_run(rid, raw, Path(name).suffix)
    return jsonify(runs=created), 202

@app.post("/api/samples/<int:index>")
def create_sample(index):
    rows = dataset_rows()
    if index < 0 or index >= len(rows): abort(404)
    raw, error = dataset_image_bytes(index, rows[index]["row"])
    if not raw: return jsonify(error=error or "Sample image unavailable"), 503
    rid = storage.create_run(f"Indian GST invoice {index+1:02d}", "dataset_evaluation", raw, "image/jpeg")
    # Ground truth remains outside process_run's extraction and decision inputs.
    label = json.loads(rows[index]["row"]["ground_truth"]).get("gt_parse", {}).get("header", {})
    submit_run(rid, raw, ".jpg", "dataset_evaluation", label)
    return jsonify(run_id=rid), 202

RUN_STATUSES = {"RUNNING", "APPROVED", "REJECTED", "PENDING_REVIEW", "FAILED"}
SEARCH_FIELDS = ("source", "vendor", "invoice_number", "po_number", "gstin", "status", "run_id")

def _int_arg(name, default, low, high):
    try: return max(low, min(high, int(request.args.get(name, default))))
    except ValueError: return default

@app.get("/api/runs")
def runs_api():
    """Slim, newest-first run summaries. Status/date filters and pagination run
    in SQL; open /api/runs/<id> for full evidence. ``X-Total-Count`` reports the
    status/date-filtered total."""
    decision = request.args.get("decision", "").upper()
    if decision and decision not in RUN_STATUSES: return jsonify(error="Unknown decision filter"), 400
    vendor = request.args.get("vendor", "").strip().casefold()[:120]
    query = request.args.get('q', '').strip().casefold()[:120]
    day = None
    if request.args.get("date"):
        try: day = date.fromisoformat(request.args["date"])
        except ValueError: return jsonify(error="date must be YYYY-MM-DD"), 400
    limit, offset = _int_arg("limit", 300, 1, 1000), _int_arg("offset", 0, 0, 10**7)
    open_only, team = request.args.get("open") == "1", request.args.get("team") or None
    rows = storage.list_runs(limit=limit, offset=offset, status=decision or None, day=day,
                             search=query or vendor or None, slim=True, open_only=open_only, team=team)
    rows = [r for r in rows if (not vendor or vendor in str(r.get("vendor") or "").casefold()) and
            (not query or any(query in str(r.get(k) or '').casefold() for k in SEARCH_FIELDS))]
    response = jsonify(rows)
    response.headers["X-Total-Count"] = str(storage.count_runs(status=decision or None, day=day,
                                                               open_only=open_only, team=team))
    return response

@app.get('/api/runs/export.csv')
def export_runs():
    fields = ['time','run_id','source','document_type','vendor','gstin','invoice_number','po_number','total','status','method','reasons']
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=fields); writer.writeheader()
    for row in storage.list_runs(limit=10000):
        writer.writerow({key: '; '.join(row.get(key) or []) if key == 'reasons' else row.get(key) for key in fields})
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition':'attachment; filename="asa_invoice_records.csv"'})

@app.get('/api/runs/export.xlsx')
def export_runs_xlsx():
    """Excel workbook built in memory; no external office dependency required."""
    fields = ['time','run_id','source','document_type','vendor','gstin','invoice_number','po_number','due_date','total','status','method','reasons']
    rows = storage.list_runs(limit=10000)
    def cell(value):
        text = '; '.join(value or []) if isinstance(value, list) else str(value or '')
        return '<c t="inlineStr"><is><t>{}</t></is></c>'.format(xml_escape(text))
    sheet_rows = ['<row r="1">' + ''.join(cell(name.replace('_', ' ').title()) for name in fields) + '</row>']
    for index, row in enumerate(rows, 2):
        sheet_rows.append('<row r="{}">{}</row>'.format(index, ''.join(cell(row.get(name)) for name in fields)))
    workbook = io.BytesIO()
    with zipfile.ZipFile(workbook, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Invoice records" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml', '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{}</sheetData></worksheet>'.format(''.join(sheet_rows)))
    return Response(workbook.getvalue(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition':'attachment; filename="asa_invoice_records.xlsx"'})

@app.post("/api/history/clear")
def clear_history():
    count = storage.archive_history()
    log.info("archived %s completed runs from visible history", count)
    return jsonify(archived=count, note="Audit, duplicate and PO balance records retained")

@app.get("/api/ai/config")
def ai_config_get():
    """Expose non-secret AI configuration state for the settings panel."""
    return jsonify(endpoint=AI_RUNTIME_CONFIG["endpoint"], model=AI_RUNTIME_CONFIG["model"],
                   api_key_configured=bool(AI_RUNTIME_CONFIG["api_key"]),
                   configured=all(AI_RUNTIME_CONFIG.values()))

@app.post("/api/ai/config")
def ai_config_set():
    """Set optional advisory-model configuration for this running process only."""
    data = request.get_json(silent=True) or {}
    endpoint = str(data.get("endpoint") or "").strip()
    model = str(data.get("model") or "").strip()
    api_key = str(data.get("api_key") or "")
    if endpoint and not endpoint.startswith(("https://", "http://")):
        return jsonify(error="AI endpoint must start with http:// or https://"), 400
    if len(endpoint) > 500 or len(model) > 200 or len(api_key) > 1000:
        return jsonify(error="AI configuration value is too long"), 400
    AI_RUNTIME_CONFIG["endpoint"] = endpoint
    AI_RUNTIME_CONFIG["model"] = model
    # Empty key means “leave the current runtime key unchanged”; this prevents
    # a page refresh from accidentally deleting an environment-provided key.
    if api_key:
        AI_RUNTIME_CONFIG["api_key"] = api_key
    return jsonify(saved=True, endpoint=endpoint, model=model,
                   api_key_configured=bool(AI_RUNTIME_CONFIG["api_key"]),
                   note="The key is retained only by this running server process.")

@app.post("/api/ai/connection-test")
def ai_connection_test():
    """Verify the configured provider without processing a customer document."""
    result = _call_model("Invoice Number: AI-CONNECTION-CHECK", AI_RUNTIME_CONFIG)
    if result["status"] == "COMPLETED":
        return jsonify(status="COMPLETED", model=result.get("model"),
                       note="AI provider accepted the request."), 200
    return jsonify(status=result["status"], model=result.get("model"),
                   error=result.get("error") or "AI provider is not configured."), 503

@app.get("/api/runs/<rid>")
def run_api(rid):
    row = storage.get_run(rid)
    return jsonify(row) if row else (jsonify(error="Not found"), 404)

@app.get("/api/runs/<rid>/events")
def events(rid):
    if not storage.run_status(rid): abort(404)
    def generate():
        # Two indexed queries per poll (new stage rows + status) instead of
        # reloading the whole run, its payload, stages and reviews every second.
        last_id = 0
        for _ in range(900):
            status = storage.run_status(rid)
            for item in storage.stages_after(rid, last_id):
                last_id = item.pop("id")
                yield "data: " + json.dumps(item, default=str) + "\n\n"
            if status != "RUNNING":
                yield "event: finished\ndata: " + json.dumps({"status": status}) + "\n\n"
                break
            time.sleep(1.0)
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})

@app.get("/api/runs/<rid>/document")
def document_api(rid):
    doc = storage.document_for_run(rid)
    if not doc: abort(404)
    return Response(doc.content, mimetype=doc.mime,
                    headers={"Content-Disposition":"inline; filename=\"" + secure_filename(doc.filename) + "\"",
                             "Cache-Control":"private, no-store", "X-Content-Type-Options":"nosniff"})

CORRECTABLE_FIELDS = {"invoice_number", "po_number", "vendor", "gstin", "date", "total", "subtotal", "discount", "tax"}

@app.post("/api/runs/<rid>/issues/<issue>")
def acknowledge_issue_api(rid, issue):
    data = request.get_json(silent=True) or {}
    reviewer = str(data.get("reviewer") or "").strip()[:80]
    acknowledged = data.get("acknowledged")
    if not reviewer or not isinstance(acknowledged, bool):
        return jsonify(error="Reviewer and acknowledgement state are required"), 400
    row = storage.get_run(rid)
    if not row: abort(404)
    checks = (row.get("decision") or {}).get("checks") or []
    if not any(c.get("check") == issue and c.get("outcome") != "VERIFIED_PASS" for c in checks):
        return jsonify(error="Unknown issue"), 400
    storage.record_review(rid, reviewer, "ISSUE_ACKNOWLEDGED" if acknowledged else "ISSUE_REOPENED", issue, {})
    return jsonify(saved=True, automated_decision=row["status"])

@app.post("/api/runs/<rid>/reviews")
def review_api(rid):
    data = request.get_json(silent=True) or {}
    reviewer = str(data.get("reviewer") or "").strip()[:80]
    outcome = data.get("outcome")
    note = str(data.get("note") or "").strip()[:1000]
    corrections = data.get("corrections") or {}
    if not reviewer or outcome not in ("VERIFIED", "NEEDS_CORRECTION") or not isinstance(corrections, dict) or (outcome == "NEEDS_CORRECTION" and not note):
        return jsonify(error="Reviewer, valid outcome and correction note are required"), 400
    if set(corrections) - CORRECTABLE_FIELDS: return jsonify(error="Unknown correction field"), 400
    row = storage.get_run(rid)
    if not row: abort(404)
    revised = None
    if corrections:
        from asa_invoice_processor import decide
        amended = dict(row.get("invoice") or {})
        amended.update(corrections)
        amended["field_metadata"] = dict(amended.get("field_metadata") or {})
        for field, value in corrections.items():
            amended["field_metadata"][field] = {"value":value, "evidence":note, "method":"manual_review",
                                                 "reliability":"MANUALLY_VERIFIED", "validation_status":"CORRECTED",
                                                 "manual_correction":True}
        # Same ledger scope as the live decision: every related completed run,
        # archived or not, excluding this run itself.
        history = storage.decision_history(amended, exclude=rid)
        masters = master_data.decision_masters(ROOT / "samples")
        revised = decide(amended, ROOT / "samples" / "pos.csv", method="ocr", history_rows=history,
                         pos=masters.get("po_master"), vendor_master=masters.get("vendor_master")).__dict__
    storage.record_review(rid, reviewer, outcome, note, corrections, revised)
    return jsonify(saved=True, automated_decision=row["status"], revised_review_result=revised)

@app.post('/api/runs/bulk-review')
def bulk_review_api():
    data = request.get_json(silent=True) or {}
    run_ids = data.get('run_ids') or []
    reviewer = str(data.get('reviewer') or '').strip()[:80]
    outcome = data.get('outcome')
    note = str(data.get('note') or '').strip()[:1000]
    if not isinstance(run_ids, list) or not run_ids or len(run_ids) > 100 or not reviewer or outcome not in ('VERIFIED', 'NEEDS_CORRECTION') or (outcome == 'NEEDS_CORRECTION' and not note):
        return jsonify(error='Select up to 100 runs, enter a reviewer, and provide a correction note when needed'), 400
    saved = 0
    for rid in set(str(item) for item in run_ids):
        if storage.get_run(rid):
            storage.record_review(rid, reviewer, outcome, note, {})
            saved += 1
    return jsonify(saved=saved, note='Review notes saved. Automated payment decisions remain unchanged.')

@app.get("/api/runs/<rid>/audit.json")
def audit_export(rid):
    """Self-contained audit record: decision evidence, stage trace and reviews."""
    row = storage.get_run(rid)
    if not row: abort(404)
    body = json.dumps({"exported_at": datetime.now(timezone.utc).isoformat(), "run": row}, default=str, indent=2)
    return Response(body, mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="asa_audit_{secure_filename(rid)}.json"'})

@app.get("/api/master/<kind>")
def master_get(kind):
    if kind not in master_data.KINDS: abort(404)
    try: rows, info = master_data.load(kind, ROOT / "samples")
    except master_data.MasterDataError as exc: return jsonify(error=str(exc), errors=exc.errors), 500
    if kind == "po_master" and rows:
        # PO utilisation: opening balance less approved invoices in the ledger.
        approved = storage.approved_by_po()
        for row in rows:
            value, count = approved.get(storage._key(row["po_number"]), (0.0, 0))
            row["approved_value"], row["approved_invoices"] = round(value, 2), count
            row["remaining"] = round(row["po_amount"] - row["invoiced_to_date"] - value, 2)
            row["utilisation"] = round((row["invoiced_to_date"] + value) / row["po_amount"], 4) if row["po_amount"] else None
    return jsonify(kind=kind, rows=rows or [], **info)

@app.get("/api/master/<kind>.csv")
def master_csv(kind):
    if kind not in master_data.KINDS: abort(404)
    rows, _ = master_data.load(kind, ROOT / "samples")
    return Response(master_data.to_csv(kind, rows or []), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'})

@app.put("/api/master/<kind>")
def master_put(kind):
    """Replace a master list from CSV. Affects future decisions only; recorded
    decisions are immutable."""
    if kind not in master_data.KINDS: abort(404)
    data = request.get_json(silent=True) or {}
    content, updated_by = data.get("csv"), str(data.get("updated_by") or "").strip()[:80]
    if not isinstance(content, str) or not content.strip(): return jsonify(error="CSV content is required"), 400
    if not updated_by: return jsonify(error="Enter who is making this change"), 400
    try: rows = master_data.save(kind, content, updated_by)
    except master_data.MasterDataError as exc: return jsonify(error=str(exc), errors=exc.errors), 400
    log.info("%s replaced by %s: %s rows", kind, updated_by, len(rows))
    return jsonify(saved=len(rows), note="Applies to invoices processed from now on; existing decisions are unchanged.")

AGING_BUCKETS = ("Overdue", "Due in 0-7 days", "Due in 8-30 days", "Due in 31+ days", "No due date")
# Assumed manual handling time per invoice for the "time saved" estimate. It is
# an operator-supplied assumption, shown as such in the UI, not a measurement.
MANUAL_MINUTES_PER_INVOICE = float(os.getenv("MANUAL_MINUTES_PER_INVOICE", "8"))

def _effective(row):
    return row.get("resolution") or row["status"]

@app.get("/api/analytics/summary")
def analytics_summary():
    """AP operating metrics from indexed columns (no payload parsing)."""
    today = date.today()
    rows = [r for r in storage.analytics_rows(include_archived=request.args.get("archived") == "1")
            if r["event_type"] != "dataset_evaluation"]
    # Everything except filed non-invoices: unreadable/unknown documents are
    # candidate invoices that a person must handle, so they count and queue.
    invoices = [r for r in rows if r["routing"] != "NON_INVOICE"]
    by_status = defaultdict(lambda: {"count": 0, "value": 0.0})
    aging = {name: {"count": 0, "value": 0.0} for name in AGING_BUCKETS}
    vendors = defaultdict(lambda: {"vendor": None, "invoices": 0, "approved_value": 0.0, "exceptions": 0})
    stop_reasons = defaultdict(lambda: {"review": 0, "rejected": 0})
    queue = defaultdict(lambda: {"open": 0, "overdue": 0})
    daily = defaultdict(lambda: {"APPROVED": 0, "PENDING_REVIEW": 0, "REJECTED": 0, "FAILED": 0})
    seconds = []
    now_utc = datetime.now(timezone.utc)
    for r in rows:
        if r["processing_seconds"] is not None: seconds.append(r["processing_seconds"])
    for r in invoices:
        status = _effective(r)
        bucket = by_status[status]; bucket["count"] += 1; bucket["value"] += r["total"] or 0
        created = r["created_at"] if r["created_at"].tzinfo else r["created_at"].replace(tzinfo=timezone.utc)
        if status in daily[created.date().isoformat()]: daily[created.date().isoformat()][status] += 1
        if r["vendor_key"]:
            v = vendors[r["vendor_key"]]; v["vendor"] = v["vendor"] or r["vendor"]; v["invoices"] += 1
            if status == "APPROVED": v["approved_value"] += r["total"] or 0
            if status in ("PENDING_REVIEW", "REJECTED"): v["exceptions"] += 1
        if r["status"] in ("PENDING_REVIEW", "REJECTED"):
            for flag in filter(None, (r["flags"] or "").split(",")):
                stop_reasons[flag]["rejected" if r["status"] == "REJECTED" else "review"] += 1
        if r["status"] == "PENDING_REVIEW" and not r["resolution"]:
            team = r["route_team"] or "AP Specialist"
            queue[team]["open"] += 1
            if (now_utc - created).total_seconds() / 3600 > workflow.sla_hours(team): queue[team]["overdue"] += 1
        if status == "APPROVED":
            due = processor.parse_date(r["due_date"])
            name = ("No due date" if not due else "Overdue" if due < today else "Due in 0-7 days"
                    if (due - today).days <= 7 else "Due in 8-30 days" if (due - today).days <= 30 else "Due in 31+ days")
            aging[name]["count"] += 1; aging[name]["value"] += r["total"] or 0
    count = lambda status: by_status[status]["count"]
    decided = sum(count(s) for s in ("APPROVED", "REJECTED", "PENDING_REVIEW"))
    touchless = sum(1 for r in invoices if r["status"] == "APPROVED")
    invoice_metrics = {"invoices": len(invoices), "value": round(sum(r["total"] or 0 for r in invoices), 2),
                       "approved": count("APPROVED"), "pending": count("PENDING_REVIEW"),
                       "rejected": count("REJECTED"), "decided": decided, "touchless": touchless,
                       "approved_by_reviewer": sum(1 for r in invoices if r["resolution"] == "APPROVED"),
                       "rejected_by_reviewer": sum(1 for r in invoices if r["resolution"] == "REJECTED"),
                       "non_invoices": sum(1 for r in rows if r["routing"] == "NON_INVOICE")}
    # Time per step: gap between consecutive stage completions, so long OCR is
    # attributed to OCR rather than to every stage that was "running" alongside.
    step_totals, step_counts = defaultdict(float), defaultdict(int)
    for events in storage.recent_stage_events().values():
        finals = {}
        for e in events:
            if e["status"] in ("COMPLETED", "BLOCKED", "SKIPPED", "FAILED"): finals[e["stage"]] = e["at"]
        started = next((e["at"] for e in events if e["status"] == "RUNNING"), None)
        previous = started
        for name in STAGE_NAMES:
            if name in finals and previous is not None:
                step_totals[name] += max(0.0, (finals[name] - previous).total_seconds()); step_counts[name] += 1
                previous = finals[name]
    reviewers = defaultdict(lambda: {"approved": 0, "rejected": 0, "seconds": []})
    for r in storage.reviewer_stats():
        entry = reviewers[r["reviewer"]]
        entry["approved" if r["outcome"] == "APPROVED_BY_REVIEWER" else "rejected"] += 1
        created = r["created_at"] if r["created_at"].tzinfo else r["created_at"].replace(tzinfo=timezone.utc)
        at = r["at"] if r["at"].tzinfo else r["at"].replace(tzinfo=timezone.utc)
        entry["seconds"].append((at - created).total_seconds())
    seconds.sort()
    top = sorted(vendors.values(), key=lambda v: (-v["approved_value"], -v["invoices"]))[:10]
    return jsonify(
        as_of=today.isoformat(), documents=len(rows), decided=decided, invoice_metrics=invoice_metrics,
        touchless_rate=round(touchless / decided, 4) if decided else None,
        exception_rate=round((count("REJECTED") + count("PENDING_REVIEW")) / decided, 4) if decided else None,
        money_stopped=round(by_status["REJECTED"]["value"], 2),
        time_saved_minutes=round(touchless * MANUAL_MINUTES_PER_INVOICE, 1),
        manual_minutes_per_invoice=MANUAL_MINUTES_PER_INVOICE,
        by_status={k: {"count": v["count"], "value": round(v["value"], 2)} for k, v in by_status.items()},
        aging=[{"bucket": k, "count": v["count"], "value": round(v["value"], 2)} for k, v in aging.items()],
        top_vendors=[{**v, "approved_value": round(v["approved_value"], 2)} for v in top],
        daily=[{"date": d, **v} for d, v in sorted(daily.items())[-30:]],
        stop_reasons=sorted(({"check": k, "label": workflow.CHECK_LABELS.get(k, k), **v} for k, v in stop_reasons.items()),
                            key=lambda x: -(x["review"] + x["rejected"]))[:8],
        queue=[{"team": t, **queue[t], "sla_hours": workflow.sla_hours(t)} for t in workflow.TEAMS],
        step_seconds=[{"stage": n, "seconds": round(step_totals[n] / step_counts[n], 3)} for n in STAGE_NAMES if step_counts[n]],
        reviewers=[{"reviewer": k, "approved": v["approved"], "rejected": v["rejected"],
                    "median_seconds": sorted(v["seconds"])[len(v["seconds"]) // 2] if v["seconds"] else None}
                   for k, v in sorted(reviewers.items())],
        processing_seconds={"median": seconds[len(seconds) // 2] if seconds else None,
                            "p95": seconds[min(len(seconds) - 1, int(len(seconds) * 0.95))] if seconds else None})

@app.post("/api/runs/<rid>/resolve")
def resolve_api(rid):
    """A reviewer's final approve/reject for an invoice waiting for review.
    The automated decision is preserved alongside it for audit."""
    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "").upper()
    reviewer = str(data.get("reviewer") or "").strip()[:80]
    note = str(data.get("note") or "").strip()[:1000]
    corrections = data.get("corrections") or {}
    if action not in ("APPROVED", "REJECTED") or not reviewer:
        return jsonify(error="Choose approve or reject and enter your name"), 400
    if action == "REJECTED" and not note:
        return jsonify(error="Say why the invoice is rejected; the note goes to the vendor email"), 400
    if not isinstance(corrections, dict) or set(corrections) - CORRECTABLE_FIELDS:
        return jsonify(error="Unknown correction field"), 400
    for field in ("total", "subtotal", "discount", "tax"):
        if field in corrections:
            try: corrections[field] = float(corrections[field])
            except (TypeError, ValueError): return jsonify(error=f"{field} must be a number"), 400
    masters = master_data.decision_masters(ROOT / "samples")
    def recheck(invoice, history):
        # The reviewer has checked the document, so OCR uncertainty no longer
        # softens ledger rules: an over-PO or duplicate approval is a confirmed
        # violation and blocks the approval. ("ocr" must not be in the method.)
        from asa_invoice_processor import decide
        return decide(invoice, ROOT / "samples" / "pos.csv", method="reviewer_verified", history_rows=history,
                      pos=masters.get("po_master"), vendor_master=masters.get("vendor_master"))
    try:
        result = storage.resolve_run(rid, action, reviewer, note, corrections, recheck)
    except LookupError: abort(404)
    except storage.ResolutionConflict as exc: return jsonify(error=str(exc)), 409
    log.info("run %s %s by reviewer %s", rid, action, reviewer)
    return jsonify(saved=True, resolution=result)

@app.get("/api/vendors")
def vendors_api():
    """Vendor profiles: master record, activity in this workspace, open PO balance."""
    rows = [r for r in storage.analytics_rows(include_archived=True)
            if r["event_type"] != "dataset_evaluation" and r["routing"] != "NON_INVOICE"]
    vendor_rows, info = master_data.load("vendor_master", ROOT / "samples")
    po_rows, _ = master_data.load("po_master", ROOT / "samples")
    approved_po = storage.approved_by_po()
    profiles = {}
    def profile(name):
        key = storage._key(name)
        if key not in profiles:
            profiles[key] = {"vendor": name, "status": None, "gstin": None, "on_file": False, "invoices": 0,
                             "approved": 0, "approved_value": 0.0, "waiting": 0, "rejected": 0, "last_invoice": None,
                             "open_pos": 0, "po_remaining": 0.0, "attention": []}
        return profiles[key]
    for v in vendor_rows or []:
        p = profile(v["vendor"]); p.update(on_file=True, status=v.get("status") or "APPROVED", gstin=v.get("gstin") or None)
    for po in po_rows or []:
        p = profile(po["vendor"])
        if vendor_rows is None: p["on_file"] = True; p["status"] = p["status"] or "APPROVED"
        if str(po.get("status") or "OPEN").upper() in ("", "OPEN", "APPROVED"):
            used, _count = approved_po.get(storage._key(po["po_number"]), (0.0, 0))
            p["open_pos"] += 1; p["po_remaining"] += po["po_amount"] - po["invoiced_to_date"] - used
    for r in rows:
        if not r["vendor"]: continue
        p = profile(r["vendor"]); status = _effective(r); p["invoices"] += 1
        if status == "APPROVED": p["approved"] += 1; p["approved_value"] += r["total"] or 0
        elif status == "PENDING_REVIEW": p["waiting"] += 1
        elif status == "REJECTED": p["rejected"] += 1
        stamp = r["created_at"].isoformat()
        if not p["last_invoice"] or stamp > p["last_invoice"]: p["last_invoice"] = stamp
    for p in profiles.values():
        if not p["on_file"]: p["attention"].append("Not in vendor master")
        if str(p["status"] or "").upper() in ("BLOCKED", "PROHIBITED", "UNAUTHORISED"): p["attention"].append("Blocked in vendor master")
        if p["rejected"]: p["attention"].append(f"{p['rejected']} rejected invoice(s)")
        if p["po_remaining"] < -0.005: p["attention"].append("Billed over open PO value")
        p["approved_value"] = round(p["approved_value"], 2); p["po_remaining"] = round(p["po_remaining"], 2)
    ordered = sorted(profiles.values(), key=lambda p: (not p["attention"], -p["invoices"], p["vendor"].casefold()))
    return jsonify(vendors=ordered, source=info.get("source"))

# Sample inbox: bundled fictional documents with the behaviour each demonstrates.
DEMO_DOCS = {
    "01_clean_invoice.pdf": ("Happy path", "Clean invoice, exact PO match", "Acme Supplies",
                             "Approved. Vendor, PO-1001 and arithmetic all check out, so nobody has to touch it."),
    "02_duplicate_invoice.pdf": ("Edge case", "Same invoice sent twice", "Acme Supplies",
                                 "Rejected. Run after the clean invoice: the same vendor and invoice number were already approved."),
    "03_po_overage.pdf": ("Edge case", "Bill exceeds the PO balance", "Acme Supplies",
                          "Rejected. After the clean invoice consumed PO-1001, this 1,180.00 bill has no balance left."),
    "04_missing_po.pdf": ("Needs a person", "No purchase order quoted", "Acme Supplies",
                          "Sent to Procurement. Nothing to match against, so a buyer has to confirm the order."),
    "05_scanned_invoice.png": ("Scan", "Image-only invoice read by OCR", "Acme Supplies",
                               "Approved. Text is read from the image, every key field sits beside its label, and PO-1004 has balance."),
    "06_delivery_challan.pdf": ("Not an invoice", "Delivery challan", "Acme Supplies",
                                "Filed, not paid. It records goods delivered, so it stays out of approval numbers."),
}

def _demo_files():
    folder = ROOT / "samples" / "demo"
    return sorted(f for f in folder.iterdir() if f.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg")) if folder.is_dir() else []

@app.get("/api/demo")
def demo_list():
    items = []
    for f in _demo_files():
        tag, title, vendor, expect = DEMO_DOCS.get(f.name, ("Sample", f.stem.replace("_", " ").title(), "", ""))
        items.append({"file": f.name, "tag": tag, "title": title, "vendor": vendor, "expect": expect,
                      "kind": "image" if f.suffix.lower() != ".pdf" else "pdf"})
    return jsonify(items)

@app.get("/api/demo/<name>")
def demo_file(name):
    match = next((f for f in _demo_files() if f.name == name), None)
    if not match: abort(404)
    mime = "application/pdf" if match.suffix.lower() == ".pdf" else "image/png" if match.suffix.lower() == ".png" else "image/jpeg"
    return Response(match.read_bytes(), mimetype=mime, headers={"Cache-Control": "private, max-age=300"})

@app.post("/api/demo/<name>")
def demo_run(name):
    match = next((f for f in _demo_files() if f.name == name), None)
    if not match: abort(404)
    raw = match.read_bytes()
    try: filename, mime = validate_file(match.name, raw)
    except ValueError as exc: return jsonify(error=str(exc)), 400
    rid = storage.create_run(filename, "upload", raw, mime)
    submit_run(rid, raw, Path(filename).suffix)
    return jsonify(run_id=rid), 202

@app.get("/api/meta")
def meta_api():
    """Static vocabulary for the UI: plain-language check names, teams, stages."""
    benchmark = {}
    try:
        data = json.loads((ROOT / "dataset_benchmark.json").read_text(encoding="utf-8"))
        outcomes = data.get("field_outcomes") or {}
        comparable = sum(outcomes.get(k, 0) for k in ("Exact match", "Mismatch", "Extraction missing"))
        benchmark = {"samples": data.get("samples"), "exact": outcomes.get("Exact match", 0), "comparable": comparable,
                     "accuracy": data.get("field_exact_match_accuracy")}
    except (OSError, ValueError): pass
    return jsonify(checks=workflow.CHECK_LABELS, stages=STAGE_NAMES,
                   teams=[{"team": t, "sla_hours": workflow.sla_hours(t), "handles": v["handles"]} for t, v in workflow.TEAMS.items()],
                   benchmark=benchmark, manual_minutes_per_invoice=MANUAL_MINUTES_PER_INVOICE,
                   po_tolerance_pct=processor.PolicyConfig.from_environment().po_tolerance_pct)

@app.get("/")
def index():
    if "csrf" not in session: session["csrf"] = os.urandom(24).hex()
    return render_template("index.html", csrf=session["csrf"],
                           samples=[(i, f"Indian GST invoice {i+1:02d}") for i, _ in enumerate(dataset_rows())],
                           auth_enabled=bool(os.getenv("DEMO_USERNAME")))

LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · zamp infinity</title><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%230d0d0d'/%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='0'%3E%3Cstop offset='0' stop-color='%23ff98d9'/%3E%3Cstop offset='.5' stop-color='%23f98963'/%3E%3Cstop offset='1' stop-color='%23ac90e1'/%3E%3C/linearGradient%3E%3C/defs%3E%3Cg transform='translate%283 10%29 scale%28.54%29'%3E%3Cpath d='M24 12C28 6 34 4.5 39 7.5C44 10.5 43.5 17.5 38 19.5C32.5 21.5 28 17 24 12C20 7 15.5 2.5 10 4.5C4.5 6.5 4 13.5 9 16.5C14 19.5 20 18 24 12Z' fill='none' stroke='url%28%23g%29' stroke-width='4.4' stroke-linecap='round'/%3E%3C/g%3E%3C/svg%3E"><link rel="stylesheet" href="/static/app.css"></head>
<body class="login-page"><main class="login-card"><a class="brand" href="/"><svg class="brand-mark" viewBox="0 0 48 24" aria-hidden="true"><defs><linearGradient id="infgrad" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#ff98d9"/><stop offset=".5" stop-color="#f98963"/><stop offset="1" stop-color="#ac90e1"/></linearGradient></defs><path d="M24 12C28 6 34 4.5 39 7.5C44 10.5 43.5 17.5 38 19.5C32.5 21.5 28 17 24 12C20 7 15.5 2.5 10 4.5C4.5 6.5 4 13.5 9 16.5C14 19.5 20 18 24 12Z" fill="none" stroke="url(#infgrad)" stroke-width="3.6" stroke-linecap="round" stroke-linejoin="round"/></svg><span class="brand-name">zamp <em>infinity</em></span><span class="brand-tag">The potential is always infinite.</span></a>
<h1>Welcome back</h1><p class="muted">Sign in to pick up today's invoices and exceptions.</p>
<form method="post" class="stack"><label>Username<input name="username" autocomplete="username" required autofocus></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required></label>
<button class="btn btn-primary">Sign in</button></form>
{% if demo_username %}<button class="btn btn-ghost" type="button" id="demo-shortcut">Demo mode</button>
<p class="muted">Use the demo account, then enter its password.</p>{% endif %}
{% if error %}<p class="form-error" role="alert">{{ error }}</p>{% endif %}</main>
{% if demo_username %}<script>document.getElementById('demo-shortcut').addEventListener('click', function () {
  document.querySelector('input[name="username"]').value = {{ demo_username|tojson }};
  document.querySelector('input[name="password"]').focus();
});</script>{% endif %}</body></html>"""

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8766")), threaded=True)

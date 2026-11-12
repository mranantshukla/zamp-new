"""Production web entry point; the legacy dashboard CLI remains available."""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.utils import secure_filename

import storage
from asa_invoice_processor import classify_document, extract_invoice, read_document
from asa_dashboard import dataset_rows, dataset_image_bytes, evaluate_dataset
from ai_review import review_invoice

ROOT = Path(__file__).parent
app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET", "local-development-secret-change-before-deployment")
app.config.update(MAX_CONTENT_LENGTH=8 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("asa")
if os.getenv("RENDER") and (not os.getenv("DEMO_USERNAME") or not os.getenv("DEMO_PASSWORD") or not os.getenv("APP_SECRET")):
    raise RuntimeError("APP_SECRET, DEMO_USERNAME and DEMO_PASSWORD are required in deployment")
storage.initialize()
storage.recover_interrupted()

STAGE_NAMES = ["Document received", "PDF text / OCR", "Document classification", "Field extraction",
               "Extraction validation", "Vendor verification", "PO matching", "Financial validation",
               "Duplicate detection", "AP decision", "AI evidence review", "History persistence"]

def logged_in():
    return not os.getenv("DEMO_USERNAME") or session.get("authenticated") is True

@app.before_request
def guard():
    if request.path in ("/healthz", "/login") or request.path.startswith("/static/"): return
    if not logged_in(): return redirect(url_for("login"))
    if request.method in ("POST", "PUT", "DELETE") and request.headers.get("X-CSRF-Token") != session.get("csrf"):
        abort(403)

@app.route("/healthz")
def healthz():
    try:
        with storage.engine.connect() as conn: conn.exec_driver_sql("SELECT 1")
        return jsonify(status="ok")
    except Exception: return jsonify(status="unavailable"), 503

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = os.getenv("DEMO_USERNAME", "")
        password = os.getenv("DEMO_PASSWORD", "")
        if user and password and hmac.compare_digest(request.form.get("username", ""), user) and hmac.compare_digest(request.form.get("password", ""), password):
            session.clear(); session["authenticated"] = True
            session["csrf"] = os.urandom(24).hex()
            return redirect(url_for("index"))
        return render_template_string(LOGIN_HTML, error="Invalid credentials"), 401
    return render_template_string(LOGIN_HTML, error="")

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

def process_run(rid, raw, suffix, event_type="upload", label=None):
    started = time.monotonic()
    current = "Document received"
    try:
        for name in STAGE_NAMES: stage(rid, name, "PENDING")
        stage(rid, current, "RUNNING")
        stage(rid, current, "COMPLETED", f"{len(raw)} bytes received")
        current = "PDF text / OCR"; stage(rid, current, "RUNNING")
        text, method, pages = read_document(raw, suffix)
        stage(rid, current, "COMPLETED", f"{method}; {pages} page(s)")
        current = "Document classification"; stage(rid, current, "RUNNING")
        kind = classify_document(text)
        stage(rid, current, "COMPLETED", kind)
        current = "Field extraction"; stage(rid, current, "RUNNING")
        invoice = extract_invoice(text)
        stage(rid, current, "COMPLETED", f"{sum(bool(invoice.get(k)) for k in ('invoice_number','vendor','po_number','date','total'))}/5 key fields detected")
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
        # Benchmark labels enter only after extraction and the AP decision.
        decision = storage.locked_decision(rid, invoice, ROOT / "samples" / "pos.csv", method)
        ai_review = review_invoice(invoice, decision)
        stage(rid, "AI evidence review", "COMPLETED", ai_review["recommendation"])
        storage.finish_run(rid, {**{k:v for k,v in storage.get_run(rid).items()
                                    if k not in ("run_id", "time", "created_at", "document_id", "source", "event_type", "status", "stages", "reviews")},
                                 "ai_review": ai_review}, decision.status)
        checks = {c["check"]: c for c in decision.checks}
        stage(rid, "Vendor verification", "COMPLETED" if checks.get("vendor_master", {}).get("passed") else "BLOCKED",
              checks.get("vendor_master", {}).get("detail", "Vendor unverified"))
        stage(rid, "PO matching", "COMPLETED" if decision.po_matching["status"] == "MATCHED" else "BLOCKED",
              decision.po_matching["explanation"])
        stage(rid, "Financial validation", "COMPLETED" if decision.financial_validation["status"] == "PASSED" else "BLOCKED",
              decision.financial_validation["status"])
        stage(rid, "Duplicate detection", "BLOCKED" if not checks["duplicate_detection"]["passed"] else "COMPLETED",
              checks["duplicate_detection"]["detail"])
        stage(rid, "AP decision", "COMPLETED", decision.status)
        stage(rid, "History persistence", "COMPLETED", f"Saved in {time.monotonic()-started:.2f}s")
        if event_type == "dataset_evaluation" and label is not None:
            comparisons, accuracy = evaluate_dataset(invoice, label)
            run = storage.get_run(rid)
            payload = {k:v for k,v in run.items() if k not in ("run_id","time","created_at","document_id","source","event_type","status","stages","reviews")}
            payload.update(field_accuracy=accuracy, comparisons=comparisons)
            storage.finish_run(rid, payload, decision.status)
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
    threading.Thread(target=process_run, args=(rid, raw, Path(name).suffix), daemon=True).start()
    return jsonify(run_id=rid), 202

@app.post("/api/samples/<int:index>")
def create_sample(index):
    rows = dataset_rows()
    if index < 0 or index >= len(rows): abort(404)
    raw, error = dataset_image_bytes(index, rows[index]["row"])
    if not raw: return jsonify(error=error or "Sample image unavailable"), 503
    rid = storage.create_run(f"Indian GST invoice {index+1:02d}", "dataset_evaluation", raw, "image/jpeg")
    # Ground truth remains outside process_run's extraction and decision inputs.
    label = json.loads(rows[index]["row"]["ground_truth"]).get("gt_parse", {}).get("header", {})
    threading.Thread(target=process_run, args=(rid, raw, ".jpg", "dataset_evaluation", label), daemon=True).start()
    return jsonify(run_id=rid), 202

@app.get("/api/runs")
def runs_api():
    rows = storage.list_runs()
    decision = request.args.get("decision", "").upper()
    vendor = request.args.get("vendor", "").casefold()
    date = request.args.get("date", "")
    rows = [r for r in rows if (not decision or r["status"] == decision) and
            (not vendor or vendor in str(r.get("vendor") or "").casefold()) and
            (not date or r["time"].startswith(date))]
    return jsonify(rows)

@app.post("/api/history/clear")
def clear_history():
    count = storage.archive_history()
    log.info("archived %s completed runs from visible history", count)
    return jsonify(archived=count, note="Audit, duplicate and PO balance records retained")

@app.get("/api/runs/<rid>")
def run_api(rid):
    row = storage.get_run(rid)
    return jsonify(row) if row else (jsonify(error="Not found"), 404)

@app.get("/api/runs/<rid>/events")
def events(rid):
    if not storage.get_run(rid): abort(404)
    def generate():
        seen = 0
        for _ in range(300):
            row = storage.get_run(rid)
            events = row["stages"]
            for item in events[seen:]: yield "data: " + json.dumps(item, default=str) + "\n\n"
            seen = len(events)
            if row["status"] != "RUNNING":
                yield "event: finished\ndata: " + json.dumps({"status":row["status"]}) + "\n\n"
                break
            time.sleep(0.4)
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})

@app.get("/api/runs/<rid>/document")
def document_api(rid):
    doc = storage.document_for_run(rid)
    if not doc: abort(404)
    return Response(doc.content, mimetype=doc.mime,
                    headers={"Content-Disposition":"inline; filename=\"" + secure_filename(doc.filename) + "\"",
                             "Cache-Control":"private, no-store", "X-Content-Type-Options":"nosniff"})

@app.post("/api/runs/<rid>/reviews")
def review_api(rid):
    data = request.get_json(silent=True) or {}
    reviewer = str(data.get("reviewer") or "").strip()[:80]
    outcome = data.get("outcome")
    note = str(data.get("note") or "").strip()[:1000]
    corrections = data.get("corrections") or {}
    if not reviewer or outcome not in ("VERIFIED", "NEEDS_CORRECTION") or not isinstance(corrections, dict) or (outcome == "NEEDS_CORRECTION" and not note):
        return jsonify(error="Reviewer, valid outcome and correction note are required"), 400
    allowed = {"invoice_number", "po_number", "vendor", "gstin", "date", "total", "subtotal", "discount", "tax"}
    if set(corrections) - allowed: return jsonify(error="Unknown correction field"), 400
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
        history = [r for r in storage.list_runs(include_archived=True) if r["run_id"] != rid]
        revised = decide(amended, ROOT / "samples" / "pos.csv", method="ocr", history_rows=history).__dict__
    storage.record_review(rid, reviewer, outcome, note, corrections, revised)
    return jsonify(saved=True, automated_decision=row["status"], revised_review_result=revised)

@app.get("/")
def index():
    if "csrf" not in session: session["csrf"] = os.urandom(24).hex()
    samples = [(i, f"Indian GST invoice {i+1:02d}") for i, _ in enumerate(dataset_rows())]
    css_path = ROOT / "dashboard.css"
    if not css_path.exists(): css_path = Path(__file__).parent / "dashboard.css"
    css = css_path.read_text(encoding="utf-8")
    html = render_template_string(INDEX_HTML, csrf=session["csrf"], samples=samples, css=css)
    review_markup = "<section class='card' id='reviewqueue'><div class='section-heading'><div><span class='eyebrow'>WORK QUEUE</span><h2>Review queue</h2></div><span class='muted'>Human attention required</span></div><div id='reviewrows' class='timeline'><p class='muted'>No invoices currently require review.</p></div></section>"
    html = html.replace("<section class='card' id='history'>", review_markup + "<section class='card' id='history'>", 1)
    html = html.replace("<div id='reason'></div>", "<div id='reason'></div><section class='ai-review'><span class='eyebrow'>AI EVIDENCE REVIEW</span><div id='aiprofile' class='muted'>Waiting for automated evidence review.</div></section>", 1)
    sidebar = """<aside class='side'><div class='logo'>zamp<span>.</span></div><div class='side-caption'>DIGITAL WORKFORCE</div><nav class='app-nav' aria-label='Main navigation'><a href='#home'><span>⌂</span>Home</a><a href='#history'><span>▤</span>My invoices</a><a href='#upload'><span>⌁</span>Scan &amp; upload</a><a href='#reviewqueue'><span>!</span>Review centre</a><a href='#history'><span>◷</span>Reports</a></nav><div class='side-footer'>AP Intelligence<br><b>Operational workspace</b></div></aside>"""
    html = html.replace("<aside class='side'><div class='logo'>Z</div><a href='#upload'>◇</a><a href='#history'>◷</a></aside>", sidebar, 1)
    html = html.replace("<main class='content'>", "<main class='content' id='home'>", 1)
    hero = """<section class='hero'><div class='hero-copy'><span class='eyebrow'>ZAMP · AP INTELLIGENCE</span><h1>Let your digital<br>workforce run AP.</h1><p>Invoice capture, validation and exception handling — with people involved where judgement matters.</p><a class='hero-action' href='#upload'>Process an invoice <span>→</span></a></div><div class='hero-graphic' aria-hidden='true'><div class='orbit orbit-one'></div><div class='orbit orbit-two'></div><div class='node node-one'></div><div class='node node-two'></div><div class='node node-three'></div><div class='hero-label'>AUTONOMOUS<br>OPERATIONS</div></div></section>"""
    html = html.replace("<section class='hero'><span class='eyebrow'>ZAMP · ACCOUNTS PAYABLE</span><h1>Your bills, under control.</h1><p>Read documents, validate procurement evidence and route exceptions.</p></section>", hero, 1)
    ui_overrides = """
    body{background:#f5f3ee;color:#131313;font-family:Inter,Manrope,'DM Sans','Segoe UI',sans-serif}.shell{display:grid;grid-template-columns:236px minmax(0,1fr);min-height:100vh}
    .side{background:#101010;color:#f3f0eb;padding:30px 20px;display:flex;flex-direction:column;gap:24px;position:sticky;top:0;height:100vh}.side .logo{color:#fff;font-size:25px;font-weight:900;letter-spacing:-1.8px;padding:0 10px}.side .logo span{color:#ff956b}.side-caption{font-size:9px;letter-spacing:1.7px;color:#8a8782;padding:0 10px}.app-nav{display:grid;gap:5px;margin-top:20px}.side .app-nav a{display:flex;align-items:center;gap:11px;padding:12px;border-radius:10px;color:#aaa6a0;font-size:13px;font-weight:700;text-decoration:none;transition:color .2s,background .2s}.side .app-nav a span{width:16px;text-align:center;font-size:16px}.side .app-nav a:hover{background:#23211f;color:#fff}.side-footer{margin-top:auto;border-top:1px solid #302e2b;padding:18px 10px 0;color:#817d77;font-size:11px;line-height:1.7}.side-footer b{color:#dad4cc;font-weight:600}
    .content{min-width:0;padding:25px clamp(18px,3vw,44px) 70px;max-width:1500px;width:100%;margin:0 auto}
    .hero{background:#111;color:#fff;border-radius:26px;padding:clamp(32px,4vw,64px);min-height:390px;display:grid;grid-template-columns:minmax(0,1.1fr) minmax(250px,.9fr);align-items:center;gap:20px;position:relative;overflow:hidden;margin-bottom:24px}.hero-copy{position:relative;z-index:2}.hero h1{font-size:clamp(42px,5vw,76px);letter-spacing:-4px;line-height:.93;margin:17px 0 20px;font-weight:800}.hero p{max-width:470px;color:#c7c1ba;font-size:15px;line-height:1.6}.hero .eyebrow{color:#ffab83}.hero-action{display:inline-flex;align-items:center;gap:24px;margin-top:22px;background:#f1eee8;color:#111;border-radius:100px;padding:12px 18px;text-decoration:none;font-size:12px;font-weight:800}.hero-action span{font-size:18px}.hero-graphic{min-height:280px;position:relative;background:radial-gradient(circle at 50% 50%,#fb9c7f 0%,#da7693 23%,#9f699f 44%,#252033 70%);border-radius:50%;aspect-ratio:1;justify-self:center;width:min(100%,330px);box-shadow:0 0 0 1px #ffffff22 inset}.orbit{position:absolute;border:1px solid #ffffff55;border-radius:50%;inset:16%;animation:orbit 9s linear infinite}.orbit-two{inset:31%;border-style:dashed;animation-direction:reverse;animation-duration:12s}.node{position:absolute;width:10px;height:10px;background:#fff;border-radius:50%;box-shadow:0 0 20px #fff}.node-one{top:19%;left:24%}.node-two{right:13%;bottom:30%;background:#ffcfad}.node-three{bottom:12%;left:36%;width:7px;height:7px}.hero-label{position:absolute;bottom:18%;right:-8%;font-size:9px;letter-spacing:1.5px;line-height:1.6;color:#fff;text-align:right}@keyframes orbit{to{transform:rotate(360deg)}}
    .metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:24px}.metric{background:#fffdf9;border:1px solid #dedad3;border-radius:13px;padding:16px 18px;box-shadow:none;transition:transform .18s,border-color .18s}.metric:hover{transform:translateY(-2px);border-color:#aa9b91}.metric b{display:block;font-size:25px;margin-top:6px;color:#161616}.metric-label{font-size:10px;letter-spacing:.5px;text-transform:uppercase;color:#78736d;font-weight:800}
    .grid{display:grid;grid-template-columns:minmax(300px,.9fr) minmax(330px,1.1fr);gap:18px;margin:0}.card{background:#fffdf9;border:1px solid #dedad3;border-radius:17px;padding:23px;box-shadow:none;margin-bottom:18px}.card h2{font-size:20px;letter-spacing:-.7px}.button{border-radius:9px;background:#151515;color:#fff;padding:12px 15px;transition:transform .16s,background .16s}.button:hover{background:#33302d;transform:translateY(-1px)}.activity{background:#151515!important;border-radius:12px!important;padding:18px!important;color:#f2eee9!important}.tablewrap{overflow-x:auto}.tablewrap table{min-width:100%}.badge.APPROVED{background:#e9f8ef;color:#14714b}.badge.REJECTED,.badge.FAILED{background:#ffe9ed;color:#b32643}.badge.PENDING_REVIEW{background:#fff3d6;color:#8a620e}.ai-review{margin:18px 0;padding:15px;border:1px solid #e2ddd5;border-left:3px solid #ec936a;border-radius:10px;background:#fcf7f1}.ai-review p{margin:7px 0}.preview{transition:transform .2s}.is-scanning .preview{animation:scanPulse 1.2s ease-in-out infinite}@keyframes scanPulse{50%{box-shadow:0 0 0 4px #d9798b33}}@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
    @media(max-width:1000px){.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:740px){.shell{display:block}.side{height:auto;position:static;flex-direction:row;align-items:center;padding:10px 16px;gap:10px}.app-nav{display:flex;overflow:auto;margin:0}.side .app-nav a{white-space:nowrap;padding:8px}.side .app-nav a span{display:none}.content{padding:16px 14px 60px}.grid,.cols{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.card{padding:18px}}
    """
    ai_ui = """<script>(() => { const original = window.renderRun; if (!original) return;
    window.renderRun = function(run) { original(run); const target = document.getElementById('aiprofile');
      if (!target) return; const review = run.ai_review; if (!review) { target.textContent = 'No AI evidence review is available for this historical run.'; return; }
      const flags = (review.flagged_checks || []).map(flag => `${flag.check}: ${flag.outcome}`).join(' · ');
      target.innerHTML = `<b>${esc(review.recommendation)}</b><p>${esc(review.summary)}</p>${flags ? `<small>${esc(flags)}</small>` : ''}`;
    }; })();</script>"""
    html = html.replace("</head>", "<style>" + ui_overrides + "</style></head>", 1)
    return html.replace("</body>", ai_ui + "</body>", 1)

LOGIN_HTML = """<!doctype html><title>Sign in · ASA</title><main style='font:16px Arial;max-width:360px;margin:15vh auto'><h1>Invoice intelligence</h1><p>Sign in to view invoice records.</p><form method=post><input name=username placeholder=Username required><input name=password type=password placeholder=Password required><button>Sign in</button></form><p>{{error}}</p></main>"""
INDEX_HTML = """<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>ASA Invoice Intelligence</title><style>{{ css }}</style><style>
body{margin:0;background:#f6f6f1;color:#182018;font:15px Arial,sans-serif}.shell{display:grid;grid-template-columns:94px 1fr;min-height:100vh}.side{background:#171a15;color:#ffec72;padding:25px 15px}.logo{font-size:30px;font-weight:900}.side a{display:block;color:#fff;text-decoration:none;margin:42px 0}.content{padding:28px 3vw;max-width:1500px}.hero{background:#ffec72;border-radius:24px;padding:30px 36px;margin-bottom:22px}.hero h1{font-size:32px;margin:0}.hero p{margin-bottom:0}.metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.metric,.card{background:#fff;border:1px solid #e9e9dd;border-radius:20px;padding:22px}.metric b{display:block;font-size:25px;margin-top:10px}.grid{display:grid;grid-template-columns:minmax(300px,420px) 1fr;gap:22px;margin:22px 0}.card h2{margin:0 0 16px}.eyebrow{font-size:11px;letter-spacing:2px;color:#777c68;font-weight:800}.tabs{display:flex;gap:8px;margin:16px 0}.tabs button,.button{border:0;border-radius:12px;background:#ffec72;color:#1a1a13;padding:12px 16px;font-weight:800;cursor:pointer}input,select,textarea{box-sizing:border-box;width:100%;padding:12px;margin:7px 0 13px;border:1px solid #d9ddcb;border-radius:10px;background:white}.upload{border:2px dashed #cdd1bd;border-radius:15px;padding:18px}.preview{height:350px;width:100%;object-fit:contain;background:#fafaf5;border:1px solid #e7e8db;border-radius:12px}.timeline{display:grid;gap:6px;max-height:380px;overflow:auto}.stage{display:flex;justify-content:space-between;border-bottom:1px solid #eee;padding:9px}.stage small{color:#6b7562}.cols{display:grid;grid-template-columns:1fr 1fr;gap:20px}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px;border-bottom:1px solid #eceee7;white-space:nowrap}th{color:#6b7562;font-size:11px;text-transform:uppercase}.badge{display:inline-block;border-radius:20px;padding:6px 10px;background:#eee}.APPROVED{background:#dff4e9;color:#147148}.REJECTED,.FAILED{background:#ffe3e3;color:#a52727}.PENDING_REVIEW{background:#fff2c7;color:#84630d}.muted{color:#677169}.error{color:#b02727}.details{white-space:pre-wrap;max-height:350px;overflow:auto}.review{margin-top:18px}.hide{display:none}@media(max-width:850px){.shell{grid-template-columns:1fr}.side{display:none}.grid,.cols,.metrics{grid-template-columns:1fr}.content{padding:15px}}
</style></head><body><div class='shell'><aside class='side'><div class='logo'>Z</div><a href='#upload'>◇</a><a href='#history'>◷</a></aside><main class='content'><section class='hero'><span class='eyebrow'>ZAMP · ACCOUNTS PAYABLE</span><h1>Your bills, under control.</h1><p>Read documents, validate procurement evidence and route exceptions.</p></section><div class='metrics' id='metrics'></div><div class='grid'><section class='card' id='upload'><span class='eyebrow'>NEW RUN</span><h2>Add a bill</h2><div class='tabs'><button id='uploadtab'>Upload from computer</button><button id='sampletab'>Select from dataset</button></div><form id='uploadform' class='upload'><input type=file name=invoice_file accept='.pdf,.png,.jpg,.jpeg' required><button class='button'>Fetch bill details →</button></form><form id='sampleform' class='hide'><select id='sample'>{% for i,name in samples %}<option value='{{i}}'>{{name}}</option>{% endfor %}</select><button class='button'>Fetch dataset bill →</button></form><p class='muted'>PDF, PNG or JPEG · Up to 8 MB</p><p id='error' class='error'></p></section><section class='card'><span class='eyebrow'>LIVE ACTIVITY</span><h2>Run monitor</h2><div class='cols'><div><div id='timeline' class='timeline'><p class='muted'>Choose a bill to begin.</p></div></div><div><div id='previewempty' class='muted'>The source bill appears here.</div><iframe id='previewpdf' class='preview hide' title='Bill preview'></iframe><img id='previewimg' class='preview hide' alt='Bill preview'></div></div></section></div><section class='card hide' id='result'><span class='eyebrow'>DECISION EVIDENCE</span><h2 id='resulttitle'></h2><div id='reason'></div><div class='cols'><div><h3>Extracted fields</h3><div id='fields' class='tablewrap'></div><h3>PO matching</h3><div id='po'></div></div><div><h3>Financial validation</h3><div id='finance'></div><h3>Dataset evaluation</h3><div id='evaluation'></div></div></div><div class='review'><h3>Manual document review</h3><p class='muted'>Verification does not authorise payment. Corrections preserve the automated decision.</p><input id='reviewer' placeholder='Reviewer name'><select id='outcome'><option value='VERIFIED'>Verified against source</option><option value='NEEDS_CORRECTION'>Needs correction</option></select><textarea id='corrections' placeholder='Corrected fields as JSON, e.g. {"total": 1000}'></textarea><textarea id='note' placeholder='Review note'></textarea><button id='savereview' class='button'>Save review</button><p id='reviewmessage'></p><div id='reviews'></div></div></section><section class='card' id='history'><span class='eyebrow'>AUDIT TRAIL</span><h2>Processing history</h2><div class='cols'><select id='decisionfilter'><option value=''>All decisions</option><option>APPROVED</option><option>REJECTED</option><option>PENDING_REVIEW</option><option>FAILED</option></select><input id='vendorfilter' placeholder='Filter by vendor'></div><input id='datefilter' type=date><div class='tablewrap'><table><thead><tr><th>Time</th><th>Run ID</th><th>Type</th><th>Vendor</th><th>Seller GSTIN</th><th>Invoice</th><th>PO</th><th>Total</th><th>Method</th><th>Accuracy</th><th>Decision</th><th>Reason</th><th>Manual check</th></tr></thead><tbody id='historyrows'></tbody></table></div></section></main></div><script>
const csrf={{ csrf|tojson }}; let active=null, source=null; const $=id=>document.getElementById(id); const esc=s=>String(s??'-').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function table(rows){return '<table>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+esc(x)+'</td>').join('')+'</tr>').join('')+'</table>'}
function showTab(sample){$('uploadform').classList.toggle('hide',sample);$('sampleform').classList.toggle('hide',!sample)}$('uploadtab').onclick=()=>showTab(false);$('sampletab').onclick=()=>showTab(true);
async function start(url,body){$('error').textContent='';let r=await fetch(url,{method:'POST',headers:{'X-CSRF-Token':csrf},body});let j=await r.json();if(!r.ok){$('error').textContent=j.error||'Request failed';return}active=j.run_id;showRun(active);}
$('uploadform').onsubmit=e=>{e.preventDefault();start('/api/runs',new FormData(e.target))};$('sampleform').onsubmit=e=>{e.preventDefault();start('/api/samples/'+$('sample').value)};
async function refresh(){let q=new URLSearchParams({decision:$('decisionfilter').value,vendor:$('vendorfilter').value,date:$('datefilter').value});let rows=await (await fetch('/api/runs?'+q)).json();let completed=rows.filter(r=>['APPROVED','REJECTED','PENDING_REVIEW'].includes(r.status)).length;let approved=rows.filter(r=>r.status==='APPROVED').length;let pending=rows.filter(r=>r.status==='PENDING_REVIEW').length;let rejected=rows.filter(r=>r.status==='REJECTED').length;let value=rows.reduce((sum,r)=>sum+(Number(r.total)||0),0);let exceptionRate=completed?(((pending+rejected)/completed)*100).toFixed(1)+'%':'—';let currency=new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:0}).format(value);$('metrics').innerHTML=[['Invoices processed',rows.length],['Invoice value',currency],['Auto-approval rate',completed?((approved/completed)*100).toFixed(1)+'%':'—'],['Exception rate',exceptionRate],['Pending reviews',pending],['Rejected',rejected]].map(([k,v])=>`<div class='metric'><span class='metric-label'>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');$('historyrows').innerHTML=rows.map(r=>`<tr><td>${esc(r.time)}</td><td><button class='button' onclick='showRun(${JSON.stringify(r.run_id)})'>${esc(r.run_id.slice(0,8))}</button></td><td>${esc(r.document_type)}</td><td>${esc(r.vendor)}</td><td>${esc(r.gstin)}</td><td>${esc(r.invoice_number)}</td><td>${esc(r.po_number)}</td><td>${esc(r.total)}</td><td>${esc(r.method)}</td><td>${r.field_accuracy==null?'-':(r.field_accuracy*100).toFixed(1)+'%'}</td><td><span class='badge ${esc(r.status)}'>${esc(r.status)}</span></td><td>${esc((r.reasons||[])[0])}</td><td>${esc((r.reviews||[]).at(-1)?.outcome||'Unchecked')}</td></tr>`).join('')||'<tr><td colspan=13>No runs yet</td></tr>';if($('reviewrows'))$('reviewrows').innerHTML=rows.filter(r=>r.status==='PENDING_REVIEW').slice(0,8).map(r=>`<div class='stage'><b>${esc(r.invoice_number||'Invoice')}</b><span>${esc(r.vendor||'Unknown vendor')} · ${esc(r.total||'Amount unavailable')} <button class='button' onclick='showRun(${JSON.stringify(r.run_id)})'>Open</button></span></div>`).join('')||'<p class=muted>No invoices currently require review.</p>'}
['decisionfilter','vendorfilter','datefilter'].forEach(id=>$(id).onchange=refresh);
async function showRun(id){active=id;if(source)source.close();let r=await (await fetch('/api/runs/'+id)).json();let doc='/api/runs/'+id+'/document';$('previewempty').classList.add('hide');$('previewimg').classList.add('hide');$('previewpdf').classList.add('hide');if(r.document_id){let target=(r.source||'').toLowerCase().endsWith('.pdf')?$('previewpdf'):$('previewimg');target.src=doc;target.classList.remove('hide')}else $('previewempty').classList.remove('hide');renderRun(r);if(r.status==='RUNNING'){source=new EventSource('/api/runs/'+id+'/events');source.onmessage=async()=>renderRun(await (await fetch('/api/runs/'+id)).json());source.addEventListener('finished',async()=>{source.close();renderRun(await (await fetch('/api/runs/'+id)).json());refresh()})}}
function renderRun(r){let latest={};(r.stages||[]).forEach(s=>latest[s.stage]=s);$('timeline').innerHTML=Object.entries(latest).map(([name,s])=>`<div class='stage'><div><b>${esc(name)}</b><br><small>${esc(s.detail)}</small></div><span>${esc(s.status)}</span></div>`).join('')||'<p class=muted>Waiting for processing events…</p>';if(r.status==='RUNNING')return;$('result').classList.remove('hide');$('resulttitle').innerHTML=`<span class='badge ${esc(r.status)}'>${esc(r.status)}</span> · ${esc(r.document_type)}`;$('reason').textContent=(r.reasons||[r.error||'']).join(' · ');let inv=r.invoice||{};$('fields').innerHTML=table(Object.entries(inv).filter(([k,v])=>!['raw_text','details','line_items','field_metadata'].includes(k)).map(([k,v])=>[k,typeof v==='object'?JSON.stringify(v):v]));$('po').textContent=JSON.stringify(r.decision?.po_matching||{},null,2);$('finance').innerHTML=table((r.decision?.financial_validation?.checks||[]).map(c=>[c.check,c.outcome,c.detail]));$('evaluation').innerHTML=r.comparisons?table(r.comparisons):'<p class=muted>No reference labels for this upload.</p>';$('reviews').innerHTML=(r.reviews||[]).map(x=>`<p>${esc(x.outcome)} · ${esc(x.reviewer)} · ${esc(x.note)}</p>`).join('');}
$('savereview').onclick=async()=>{let corrections={};try{corrections=$('corrections').value.trim()?JSON.parse($('corrections').value):{}}catch(e){$('reviewmessage').textContent='Corrections must be valid JSON';return}let r=await fetch('/api/runs/'+active+'/reviews',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({reviewer:$('reviewer').value,outcome:$('outcome').value,note:$('note').value,corrections})});let j=await r.json();$('reviewmessage').textContent=j.error||'Review saved. Automated decision retained.';if(r.ok){showRun(active);refresh()}};
const clearButton=document.createElement('button');clearButton.className='button';clearButton.textContent='Clear history';clearButton.title='Archive visible runs; retain audit and duplicate records';$('history').insertBefore(clearButton,$('history').querySelector('.cols'));
clearButton.onclick=async()=>{if(!confirm('Clear visible history? Audit records, PO balances and duplicate protection will remain.'))return;let r=await fetch('/api/history/clear',{method:'POST',headers:{'X-CSRF-Token':csrf}});let j=await r.json();if(r.ok){$('history').querySelector('h2').textContent='Processing history · '+j.archived+' archived';refresh()}};
refresh();
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8766")), threaded=True)

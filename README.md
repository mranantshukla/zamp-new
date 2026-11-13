# zamp infinity

*The potential is always infinite.*

Accounts payable invoice intelligence (formerly ASA Invoice Intelligence).

An accounts payable case study that reads PDF or image bills, extracts invoice fields, checks vendors and purchase orders, reconciles amounts, detects duplicates, and records `APPROVED`, `REJECTED`, or `PENDING_REVIEW`. Document approval does not authorise payment.

## Architecture

- `asa_invoice_processor.py`: embedded PDF text, scanned-page rendering, RapidOCR, classification, extraction, GSTIN and financial checks.
- `decision_policy.py`: deterministic three-outcome policy and configurable PO tolerance/OCR evidence grade.
- `app.py`: Flask JSON API, live stage stream, document preview, review workflow and sample evaluation.
- `templates/index.html`, `static/app.js`, `static/app.css`: the workspace UI, a dependency-free single-page app (light theme by default, dark on toggle).
- `workflow.py`: plain-language check names and exception routing: which team reviews an invoice, and its deadline.
- `storage.py`: SQLAlchemy persistence for document bytes, runs, stages and reviews. SQLite is the default; set `DATABASE_URL` for PostgreSQL. Schema v2 adds indexed summary columns (vendor/invoice/PO keys, total, dates, routing, archived) so duplicate and PO-balance lookups touch only related rows; existing v1 databases are migrated additively on start-up.
- `master_data.py`: validated PO and vendor master lists stored in the database (falling back to `samples/pos.csv` and `samples/vendor_master.csv`).
- Successful OCR text is cached in the same database by document hash and OCR settings. Repeated uploads reuse OCR but always rerun vendor, PO, duplicate and financial decisions. Failed OCR is never cached.
- `asa_dashboard.py`: preserved legacy `Handler` for compatibility tests. Running the file starts the new Flask dashboard. `backend/` is an unused older prototype.

The AjitRawat original images are in `dataset_images/`. `dataset_cache.json` holds public reference labels. Labels enter only `evaluate_dataset()` after OCR extraction and the AP decision. They never populate production extraction or decisions.

## Run locally

Use Python 3.12. From this directory:

```powershell
python -m pip install -r requirements.txt
python asa_dashboard.py --port 8766
```

Open http://127.0.0.1:8766/. The local database is `asa_local.db`. Set `DATABASE_URL` for PostgreSQL. CLI compatibility remains:

```powershell
python asa_invoice_processor.py --invoice samples/invoice_happy.txt --po-data samples/pos.csv --history samples/history.json
```

The old text sample lacks subtotal and routes to review under the current policy. Dashboard uploads accept valid PDF, PNG and JPEG up to 8 MB.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection string. Defaults to local SQLite. |
| `APP_SECRET` | Flask signing secret, required on Render. |
| `DEMO_USERNAME`, `DEMO_PASSWORD` | Demo login, required on Render. |
| `PO_TOLERANCE_PCT` | Fractional overage allowance, default `0`, maximum `0.05`. |
| `OCR_MIN_RELIABILITY` | `DIRECT_LABEL` (default) or `MANUALLY_VERIFIED`; ordinal evidence grade, not calibrated confidence. |
| `PORT` | Production port from Render; local command accepts `--port`. |
| `MANUAL_MINUTES_PER_INVOICE` | Assumed manual handling time behind the "time saved" figure, default `8`. Shown as an assumption in the UI. |
| `SLA_HOURS_TREASURY_FRAUD`, `SLA_HOURS_VENDOR_MANAGEMENT`, `SLA_HOURS_PROCUREMENT`, `SLA_HOURS_AP_SPECIALIST` | Review deadlines per team in hours (defaults 8, 48, 48, 24). |
| `PROCESSING_WORKERS` | Background processing threads, default `2`, maximum `8`. OCR is serialised, so extra workers mainly help PDF-text documents; batch uploads queue behind this pool. |
| `AI_REVIEW_API_URL`, `AI_REVIEW_MODEL`, `AI_REVIEW_API_KEY` | Persistent AI provider configuration via server environment. The dashboard settings form only changes the running process. |

## Decision policy

Confirmed violations outrank uncertainty. Missing data is blocked or uncertain, never a pass. An approved or paid invoice with the same normalized vendor and invoice number is a business duplicate and is rejected. Repeated pending submissions remain pending with a separate warning. Partial invoices consume PO balance cumulatively. OCR does not automatically force review: critical OCR fields need direct source-label evidence and checks must pass. This structural evidence check is not a calibrated probability or proof of extraction correctness.

Non-tax-invoice documents receive a separate routing status (`NON_INVOICE`) and bypass AI invoice reconciliation. They remain visible in history but are excluded from invoice approval and exception-rate metrics. The run monitor streams database-backed events. Manual corrections are separate from original automated decisions. “Clear history” archives the visible list while retaining duplicate and PO audit records.

Completed runs, stage history, reviews and OCR cache survive server restarts when the database is persistent. The free Render Blueprint uses ephemeral SQLite, so that guarantee does not apply there. Login sessions use signed cookies valid for seven days when `APP_SECRET` is stable. A server restart loses AI settings entered only in the dashboard; set the environment variables above for persistent settings. The run trace shows provider HTTP errors without exposing the configured API key.

## Demo and tests

Run `python make_demo_assets.py` to recreate six fictional files in `samples/demo/`. Process in filename order against empty history: 2 approved, 2 rejected, 2 pending. The scanned file uses PO-1004 independently of the first PDF. See `DEMO_SCRIPT.md`.

```powershell
python -m pip install pytest
python -m pytest -q test_regressions.py test_app.py test_features.py test_workflow.py
```

To run the database-backed tests against PostgreSQL as well, point `TEST_DATABASE_URL` at an empty UTF-8 database (each test drops and recreates the schema):

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://postgres@localhost:5432/asa_test"
python -m pytest -q test_app.py test_features.py test_workflow.py
```

Optional, idempotent legacy JSON import: set `DATABASE_URL`, then run `python migrate_legacy.py`. It reads `run_history.json` and `manual_verification.json` without deleting them. Legacy checks retain no invented reviewer identity.

## Render

`render.yaml` defines a free Python web service, `/healthz`, generated secret, demo login variables, and a `DATABASE_URL` placeholder. Local development defaults to SQLite. On Render, the app requires PostgreSQL through `DATABASE_URL` because the free web service loses local files when it sleeps or redeploys. Connect this repository through Render Blueprint, supply `DEMO_USERNAME`, `DEMO_PASSWORD`, and `DATABASE_URL` in Render, and test the resulting URL. See `DEPLOYMENT.md`.

## Workspace

A sidebar workspace, light by default with a dark theme on the toggle.

| Page | What it is for |
| --- | --- |
| Today | A briefing for the person reviewing: what is waiting and how much went straight through; the pipeline from received to outcome, a 14-day volume chart, the automation ring, latest activity, next deadlines, a small ledger, what stops invoices, where processing time goes, and payables by due date. |
| Exceptions | A two-pane inbox of invoices that need a person, filtered by team and ordered by deadline. ↑/↓ moves, Enter opens. |
| Bring in invoices | Drop one file (opens its live view) or up to 25 (tracked together). A numbered scenario library runs six fictional documents, each triggering a different rule. |
| Invoice | The source document beside tabs for Summary, Checks, Extracted, Timeline and Activity. A decision dock at the bottom approves, rejects, corrects details or saves a note. |
| All invoices | Grouped by day, filter by result, date or text, export Excel/CSV, archive finished. |
| Suppliers | Standing, activity and purchase-order headroom for every supplier, with flags inline. |
| Orders & ledger | Purchase orders with consumption, the approval ledger, the supplier list, and validated CSV replacement. |
| Under the hood | The ten steps every invoice goes through, outcomes, team routing, measured reading accuracy and the optional AI connection. |

Press ⌘K (or `/`) anywhere to find an invoice or jump to a page. The reviewer name is stored in the browser and recorded on each decision.

### Review workflow

An invoice that needs a person is routed by its failing checks: possible duplicates and GSTIN or identity problems go to **Treasury & Fraud**, unknown or blocked vendors to **Vendor Management**, PO problems to **Procurement**, and everything else to an **AP Specialist**. Each team has a deadline.

A reviewer can **approve**, **reject** (with a reason, which feeds a ready-to-send vendor email), or **correct** extracted fields first. Approval re-runs the PO, duplicate and arithmetic rules on the corrected, reviewer-verified values against the current ledger; if it would over-bill a PO or duplicate an approved invoice it is refused with the rule that blocks it. The reviewer's decision is stored separately (`resolution`) and the automated decision is never overwritten. Reviewer-approved invoices count as approved for duplicate protection, PO balances and analytics. API: `POST /api/runs/<id>/resolve`.

## AP controls and workspace features

- **Master data** view: upload PO and vendor master CSVs (validated: required columns, non-negative amounts, unique POs/vendors, GSTIN checksum, known statuses). PO rows show utilisation from approved invoices. Changes apply to future decisions only. API: `GET/PUT /api/master/{po_master|vendor_master}`, `GET /api/master/<kind>.csv`.
- **Possible-duplicate control**: an approved invoice from the same vendor with the same amount, invoice date and PO under a different invoice number routes to review (`possible_duplicate`). Exact vendor + number duplicates are still rejected.
- **Future-dated invoices** (more than one day ahead) route to review (`invoice_date_future`).
- **Analytics**: `GET /api/analytics/summary` provides invoice metrics, touchless and exception rates, payables aging of approved invoices by due date, top vendors and processing-time median/p95. Dashboard tiles use it, so they cover all runs instead of the latest page.
- **Audit record**: `GET /api/runs/<id>/audit.json` downloads the decision evidence, stage trace and reviews for one run.
- **Records API**: `GET /api/runs` returns slim summaries with the latest review, filters status/date in SQL, accepts `limit`/`offset`, and reports `X-Total-Count`. Full evidence stays at `/api/runs/<id>`.
- **Hardening**: login is throttled after 5 failures in 5 minutes per client address; responses carry `nosniff`, `SAMEORIGIN` framing and `same-origin` referrer headers; large text responses are gzip-compressed.

## Known limits

The workspace supports batch intake (up to 25 files per request), an operational Inbox, searchable records, Excel and CSV exports, vendor summaries derived from run history, a dedicated exception-review workspace, and review notes for selected invoices. A batch review preserves the automated decision and records the supplied reviewer identity and note. The interface sections share the Flask application; they are not separate server deployments. Email ingestion, accounting sync, scheduled notifications, team roles, payment scheduling, and PDF coordinate highlighting are not implemented; they require provider credentials, account mapping, and document location evidence. Vendor summaries do not replace vendor-master approval.

US-style invoices ("INVOICE" without "TAX", `$` amounts, dates such as `Nov 3, 2026`) are often classified as unknown and miss dates or totals; they are routed to a person rather than approved. The heuristic extractor remains weak on real bills: the current 22-image AjitRawat benchmark is **30/90 exact comparable fields (33.3%)**. OCR evidence grades are structural, not calibrated confidence. GST checks do not establish tax-law compliance. Interrupted runs are marked `FAILED` after a worker restart. Schema version 1 is initialized additively; future changes need a migration framework. Render free web services spin down and free Postgres expires after 30 days.

First-time OCR can still be slow on CPU-limited hosting. On one local sample (`invoice_07.jpg`), recognition batch size 1 took 3.3–3.4 seconds and batch size 6 took 5.8–5.9 seconds, so batch size 1 remains the default. These local timings do not predict Render latency. Exact-document cache hits avoid OCR on subsequent runs; cold documents still require OCR.

The live monitor checks for stage updates once per second for up to 15 minutes. A slow OCR run can therefore finish while the monitor is still connected, with less database polling than before.


Reference-informed controls: separately labelled freight charges are included in arithmetic alongside discounts, GST and rounding. Copy markings are excluded from supplier-name fallback. Future dates are advisory. Announced bank-account changes route to Treasury & Fraud for independent verification. Missing identity, references, financial evidence or PO authority still block approval because they prevent payee, duplicate or overpayment checks.


### Reference bill policy and AI fallback

`reference_bill_policy.py` ports bill-policy semantics from `akshcatt/Zamp-ai` (`app/pipeline.py`): missing references, dates, OCR confidence and tax-format findings are reported; everyday bills can be recorded without a PO; exact duplicates and blocked vendors reject; changed invoice amounts, bank changes, arithmetic differences and cumulative PO overruns hold. Rounding uses the smaller of INR 10 and 2% of the compared amount. Amounts of INR 25 lakh or more require controller approval. Bank-account and look-alike email checks use optional `bank_account`, `ifsc` and `email_domain` vendor-master columns. No payment is sent by this app.

If OCR fails, returns no total or reads fewer than three key fields, the configured vision model reads the original page images (up to six pages). AI transcription provenance is recorded and the deterministic bill rules still decide. Provider failures never turn into approval. The default Render model is Gemini 3.1 Flash-Lite through its OpenAI-compatible endpoint; set `AI_REVIEW_API_KEY` in Render. Uploaded pages are sent to the configured AI provider when fallback runs. PO line inference and goods-receipt matching are enabled when the PO CSV includes a `lines` JSON array (description, sku, unit_price, qty, and optional received_qty) and `receipt_required`. Aggregate balance checks remain active for existing CSVs.

# ASA Invoice Intelligence

An accounts payable case study that reads PDF or image bills, extracts invoice fields, checks vendors and purchase orders, reconciles amounts, detects duplicates, and records `APPROVED`, `REJECTED`, or `PENDING_REVIEW`. Document approval does not authorise payment.

## Architecture

- `asa_invoice_processor.py`: embedded PDF text, scanned-page rendering, RapidOCR, classification, extraction, GSTIN and financial checks.
- `decision_policy.py`: deterministic three-outcome policy and configurable PO tolerance/OCR evidence grade.
- `app.py`: Flask dashboard, live stage stream, preview, history, review and sample evaluation.
- `storage.py`: SQLAlchemy persistence for document bytes, runs, stages and reviews. PostgreSQL on Render; SQLite locally.
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

## Decision policy

Confirmed violations outrank uncertainty. Missing data is blocked or uncertain, never a pass. An approved or paid invoice with the same normalized vendor and invoice number is a business duplicate and is rejected. Repeated pending submissions remain pending with a separate warning. Partial invoices consume PO balance cumulatively. OCR does not automatically force review: critical OCR fields need direct source-label evidence and checks must pass. This structural evidence check is not a calibrated probability or proof of extraction correctness.

Non-tax-invoice document types route to review. The run monitor streams database-backed events. Manual corrections are separate from original automated decisions. “Clear history” archives the visible list while retaining duplicate and PO audit records.

## Demo and tests

Run `python make_demo_assets.py` to recreate six fictional files in `samples/demo/`. Process in filename order against empty history: 2 approved, 2 rejected, 2 pending. The scanned file uses PO-1004 independently of the first PDF. See `DEMO_SCRIPT.md`.

```powershell
python -m pip install pytest
python -m pytest -q test_regressions.py test_app.py
```

Optional, idempotent legacy JSON import: set `DATABASE_URL`, then run `python migrate_legacy.py`. It reads `run_history.json` and `manual_verification.json` without deleting them. Legacy checks retain no invented reviewer identity.

## Render

`render.yaml` defines a Python web service, PostgreSQL, `/healthz`, generated secret, and demo login variables. Connect this repository through Render Blueprint, supply `DEMO_USERNAME` and `DEMO_PASSWORD`, and test the resulting URL. See `DEPLOYMENT.md`.

## Known limits

The heuristic extractor remains weak on real bills: the current 22-image AjitRawat benchmark is **25/90 exact comparable fields (27.8%)**. OCR evidence grades are structural, not calibrated confidence. GST checks do not establish tax-law compliance. Interrupted runs are marked `FAILED` after a worker restart. Schema version 1 is initialized additively; future changes need a migration framework. Render free web services spin down and free Postgres expires after 30 days.

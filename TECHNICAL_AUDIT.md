# ASA technical audit — 2026-11-13

## Active architecture

`app.py` is the production Flask entry point. Uploads and dataset images create
a `processing_runs` record in `storage.py`, run asynchronously in a process
thread, and stream persisted stage events to the dashboard. `asa_invoice_processor.py`
selects PyPDF text extraction for machine-readable PDFs, PyMuPDF rendering plus
RapidOCR for scanned PDF pages, and RapidOCR for images. `ai_extraction.py` can
reconcile extracted fields only when it can quote matching OCR evidence.

The deterministic decision engine is `asa_invoice_processor.decide()` with
three invoice outcomes: `APPROVED`, `REJECTED`, and `PENDING_REVIEW`.
`decision_policy.py` resolves confirmed violations ahead of uncertainty.

`backend/` and the pre-Flask sections of `asa_dashboard.py` are legacy code.
The Flask route in `asa_dashboard.py` delegates to `app.py`; no production path
uses the legacy HTTP handler.

## Working components

- Valid PDF, PNG and JPEG validation; document bytes, runs, stages and reviews
  persist through SQLAlchemy.
- Embedded PDF text and scanned-PDF OCR paths are distinct and tested.
- OCR cache avoids reprocessing the same document but repeats AP decisions.
- Duplicate and cumulative PO checks serialize their decision transaction.
- Dataset labels enter only post-decision evaluation.
- Manual reviews retain the original automated decision.
- A safe AI connection-test endpoint reports provider failures without exposing
  the API key.

## Findings and fixes in this phase

| Finding | Result |
| --- | --- |
| Non-invoices were counted in dashboard invoice/exception metrics. | Fixed: documents carry an explicit routing status; non-invoices are excluded from AP approval and exception metrics. |
| Purchase orders alone skipped AI extraction. | Fixed: all non-invoices skip AI invoice reconciliation and receive a clear routing reason. |
| Render Blueprint omitted AI environment-variable declarations. | Fixed: optional endpoint, model, key and timeout variables are declared with secret values supplied in Render. |
| Live deployment served an older build during inspection. | Deployment remains unverified until Render redeploys `main`; do not claim live verification yet. |

## Remaining risks and limits

- Current stage history stores event timestamps, rather than a separate
  start/finish aggregate record for every stage. Events remain sufficient to
  reconstruct duration.
- Gemini configuration is optional and is not testable without the deployment
  secret. Use **Test AI connection** after Render deploys the latest commit.
- The AjitRawat image dataset has no vendor master, PO ledger or GRN. It is an
  extraction benchmark, not payment-approval data.
- The 22-image field benchmark remains 33.3% exact match across its noisy,
  heterogeneous labels. It includes non-invoices and unreadable text.

## Performance and controls pass — 2026-11-13

Measured with a synthetic benchmark that seeds runs through the real
`process_run` pipeline (SQLite, 3,000 runs across 50 vendors):

| Path | Before | After |
| --- | --- | --- |
| Decision transaction (held under the global decision lock) | 1.9 ms at start → 147 ms at 3,000 runs (loads and parses all history) | 1.2 ms → 1.8–3.7 ms (indexed related rows only) |
| Seeding throughput | 89 ms/run | 18 ms/run |
| `GET /api/runs` (300 rows) | 70 ms, 2.3 MB | 16–18 ms, 225 KB; 25 KB gzipped |
| `GET /api/runs?decision=…` | 61 ms | 20 ms (composite status/created index) |
| `GET /api/runs/<id>` | 6.7 ms | 0.7–1.2 ms (stage index) |
| Live monitor poll (per second per viewer) | 7.2 ms, full run reload | 0.3–0.4 ms, new stages + status only |

| Finding | Result |
| --- | --- |
| Every decision loaded and JSON-parsed the entire run history inside the global lock. | Fixed: schema v2 summary columns and indexes; only same-vendor or same-PO rows are read. |
| `/api/runs` returned full evidence for 300 runs uncompressed (~7 KB/row). | Fixed: slim rows, SQL filters, pagination, `X-Total-Count`, gzip. |
| Dashboard metric tiles were computed in the browser from the latest 300 runs only. | Fixed: tiles use `/api/analytics/summary` over all runs. |
| Records list always showed "Unchecked" because reviews were never attached to list rows. | Fixed: latest review attached in one query. |
| Correction re-check used only the latest 300 runs (and in-flight runs) as history. | Fixed: same indexed ledger scope as live decisions. |
| Batch upload started one thread per file (up to 25). | Fixed: bounded `PROCESSING_WORKERS` pool. |
| The live monitor fetched the full run once per stage event. | Fixed: client coalesces refetches; server streams only new stages. |
| Inlined `dashboard.css` was HTML-escaped, breaking the font import and making every page load request the full dashboard a second time. | Fixed: trusted CSS rendered with `|safe`. |
| A blank `PO Number:` label took the next line's label (e.g. PO "Subtotal"). | Fixed; regression test added. The 22-image benchmark is unchanged at 30/90. |
| `test_existing_history_and_verification_preserved` depended on a gitignored local file and failed on a fresh clone. | Fixed: the test builds its own legacy fixture. |

Verification: 78 tests pass on SQLite; the 52 database-backed tests also pass on
PostgreSQL 14. A v1 PostgreSQL database created by the previous code was
upgraded in place: archived approvals still blocked a resubmitted duplicate and
still consumed PO balance.

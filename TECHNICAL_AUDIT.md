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

# Implementation report

## Audit

The active flow was `asa_dashboard.py` → `read_document` / `extract_invoice` / `decide` in `asa_invoice_processor.py` → local JSON history. `backend/` is a separate older prototype. `dashboard.css` styles the legacy page. The old monitor rendered only after a synchronous request; JSON would not survive Render restarts. OCR automatically forced review. PO extraction could capture stray words such as `rter`; delivery challans were not classified. Repeated pending submissions were treated as confirmed business duplicates.

## Changes

- Classified documents and tightened PO extraction around explicit labels. Customer order and delivery numbers are separate.
- Added field evidence metadata and ordinal OCR evidence requirements without invented numerical confidence.
- Centralized the three-outcome policy, structured PO/financial results, and cumulative PO balance handling.
- Added PostgreSQL-capable transactional storage, optional non-destructive legacy import, an authenticated Flask dashboard, true server events, history filters, manual correction audit, and Clear history archival.
- Added Render Blueprint, dependencies, fictional demo assets and expanded tests.

## Risks

The 22-image field benchmark remains 26.7%. PostgreSQL concurrency and Render operation are unverified without a live database and deployment. Future schema changes need a migration framework beyond version-1 initialization. This case study is not a production AP control system.

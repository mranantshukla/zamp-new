# AjitRawat/invoice extraction benchmark

Run on the 22 cached original images with RapidOCR and the current `extract_invoice` parser on 12 November 2026. Dataset labels were used only in `evaluate_dataset`, after extraction.

- Images processed: 22/22; OCR exceptions: 0.
- Comparable labeled fields: 90.
- Exact matches: 24/90 (26.7%).
- Mismatches: 29; extraction missing: 37.
- Labels unavailable for a mapped field: 160. Role-ambiguous GSTIN labels: 14. Other labels without a reliable extraction mapping: 25.

| Field | Exact | Mismatch | Missing extraction |
| --- | ---: | ---: | ---: |
| Invoice number | 3 | 11 | 1 |
| PO number | 1 | 0 | 0 |
| Date | 4 | 1 | 13 |
| Total | 1 | 5 | 12 |
| Subtotal | 0 | 3 | 3 |
| PAN | 3 | 7 | 0 |
| Order number | 2 | 0 | 1 |
| Customer number | 2 | 0 | 1 |
| CGST | 2 | 1 | 3 |
| SGST | 2 | 1 | 2 |
| IGST | 4 | 0 | 1 |

The score is a field-level exact match on this small dataset, not a production accuracy estimate. Label coverage and formatting vary. The result identifies invoice number, date, total, and PAN extraction as priority work. OCR-origin invoices remain in `PENDING_REVIEW` unless a confirmed violation requires rejection.

# AjitRawat/invoice extraction benchmark

Run on the 22 cached original images with RapidOCR and the current `extract_invoice` parser on 13 November 2026. Dataset labels were used only in `evaluate_dataset`, after extraction. The current OCR path first reads the original image; only low-contrast images receive a second, deskewed/denoised contrast candidate, and the higher-scoring OCR text is retained.

- Images processed: 22/22; OCR exceptions: 0; elapsed time: 78.78 seconds (3.58 seconds per image).
- Comparable labeled fields: 90.
- Exact matches: 25/90 (27.8%) with the memory-limited OCR configuration.
- Mismatches: 28; extraction missing: 37.
- Labels unavailable for a mapped field: 160. Role-ambiguous GSTIN labels: 14. Other labels without a reliable extraction mapping: 25.

| Field | Exact | Mismatch | Missing extraction |
| --- | ---: | ---: | ---: |
| Invoice number | 3 | 10 | 2 |
| PO number | 1 | 0 | 0 |
| Date | 5 | 2 | 11 |
| Total | 1 | 5 | 12 |
| Subtotal | 0 | 2 | 4 |
| PAN | 3 | 6 | 1 |
| Order number | 2 | 0 | 1 |
| Customer number | 2 | 0 | 1 |
| CGST | 2 | 1 | 3 |
| SGST | 2 | 2 | 1 |
| IGST | 4 | 0 | 1 |

The score is a field-level exact match on this small dataset, not a production accuracy estimate. Label coverage and formatting vary. Invoice number, date, total and PAN extraction need further work. OCR alone does not force review; source evidence and business validation determine the decision. Dataset labels never enter decisioning.

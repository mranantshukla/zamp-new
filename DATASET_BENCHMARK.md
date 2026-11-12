# AjitRawat invoice benchmark

Run the complete benchmark with:

```powershell
python batch_evaluate.py --output dataset_benchmark.json
```

The command processes all locally cached dataset images through the production
OCR and extraction path. Dataset labels are read only after extraction to
calculate the report; they never enter production extraction or AP decisions.

## Latest local run

Run date: 2026-11-13 (22 images, 48.65 seconds)

| Measure | Result |
| --- | ---: |
| Comparable labelled fields | 90 |
| Exact matches | 30 |
| Mismatches | 33 |
| Missing extractions | 27 |
| Field exact-match accuracy | 33.3% |
| AP decisions | 22 `PENDING_REVIEW` |

The field mapping is intentionally conservative. Some labels in this dataset
called `amount` refer to a tax or a line item rather than the invoice grand
total, while other rows contain a purchase order, delivery challan, page
fragment, or OCR-unreadable scan. Those records cannot honestly support a 90%
invoice-total benchmark.

All 22 records route to `PENDING_REVIEW` when assessed against the bundled PO
and vendor master. This is expected: their vendors and POs are not procurement
records in the demo master, and the set includes a delivery challan and a
purchase order. Treating that as an approval-rate failure and auto-approving
them would weaken the AP controls.

| Samples | Main limitation |
| --- | --- |
| 01, 09, 10 | Multi-page Dell invoice pages; OCR merges labels and values. |
| 06, 12, 22 | Low-quality or non-invoice source; 22 contains almost no readable text. |
| 07 | Delivery challan, correctly routed away from invoice approval. |
| 13, 20, 21 | Separate pages of a tabular invoice; values belong to page-level tables. |
| 15–18 | Header/value grid invoice. Date, invoice number and grand total handling improved, but dataset `amount` labels a line amount rather than the total. |
| 19 | Airline invoice; OCR and source label disagree on invoice number and grand total. |

The benchmark is useful for regression detection, but it is not evidence that
the extractor meets production accuracy. A 90% target requires a clean,
field-specific labelled test set for actual tax invoices and a visual document
model or human-reviewed training/evaluation data for the low-quality scans.

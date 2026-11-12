# Test report

Executed on Windows with Python 3.13:

```text
python -m pytest -q test_regressions.py test_app.py
39 passed, 5 warnings in 13.45s
```

Coverage includes machine-readable and scanned PDFs, images, OCR failure, classification, PO references, GSTIN, vendor master, partial PO balance, duplicates, pending resubmissions, financial discrepancies, bad uploads, corrupt PDF, manual corrections, history archival and concurrent SQLite decisions.

Real OCR of the 22 original AjitRawat images: 24 exact matches across 90 comparable labeled fields (26.7%). Classification found 20 tax invoices, one delivery challan and one unknown. Labels remain evaluation-only.

Six fictional demo files against empty history and `samples/pos.csv`: 2 `APPROVED`, 2 `REJECTED`, 2 `PENDING_REVIEW`. The scanned image is approved using PO-1004.

**Not executed:** live PostgreSQL integration/concurrency, Render deploy and public HTTPS upload. SQLite tests are not proof of those paths.

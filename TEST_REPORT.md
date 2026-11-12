# Test report

Executed on Windows with Python 3.13:

```text
python -m pytest -q test_regressions.py test_app.py
40 passed, 5 warnings in 9.83s
```

Coverage includes machine-readable and scanned PDFs, images, OCR failure, classification, PO references, GSTIN, vendor master, partial PO balance, duplicates, pending resubmissions, financial discrepancies, bad uploads, corrupt PDF, manual corrections, history archival and concurrent SQLite decisions.

Real OCR of the 22 original AjitRawat images after the memory change: 25 exact matches across 90 comparable labeled fields (27.8%). Labels remain evaluation-only.

Six fictional demo files against empty history and `samples/pos.csv`: 2 `APPROVED`, 2 `REJECTED`, 2 `PENDING_REVIEW`. The scanned image is approved using PO-1004.

**Live result:** Render Blueprint deployed and `/healthz` returned 200. An authenticated public HTTPS machine-readable PDF upload completed as `PENDING_REVIEW` for a missing PO, with stage history, document bytes and database history verified. After lowering OCR memory use, a public HTTPS run of original AjitRawat dataset image #07 completed OCR, classified the document as a delivery challan, and produced `PENDING_REVIEW` with benchmark labels kept outside decisioning. A public scanned demo invoice completed OCR and was `APPROVED` with matched PO and passing financial checks (run `50040122-65c0-467d-a5a2-6881a9488b15`). The original PDF run remained available after redeploy. Three OCR runs interrupted by deployment/restart are recorded as `FAILED`, not as successful extraction. Concurrent PostgreSQL decisions remain untested.

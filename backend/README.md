# Milestone 1 backend structure

The existing CLI remains the compatibility entry point. The `backend` package adds explicit boundaries for schemas, extraction, validation, decisioning, and orchestration.

Example:

```python
from pathlib import Path
from backend.services.processing_service import run_invoice
from asa_invoice_processor import process

result = run_invoice(Path("samples/invoice_happy.txt"), Path("samples/pos.csv"), Path("samples/history.json"), process)
print(result.as_dict())
```

The orchestrator emits stage events that the dashboard can display and later persist to SQLite.

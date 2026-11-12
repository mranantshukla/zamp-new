"""Orchestrates the deterministic invoice workflow and emits audit events."""
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4
from ..core.schemas import ProcessingEvent, ProcessingResult

def run_invoice(invoice_path: Path, po_path: Path, history_path: Path | None, processor):
    events = []
    def event(stage, status, detail=""):
        events.append(ProcessingEvent(stage, status, detail))
    event("DOCUMENT_RECEIVED", "COMPLETED", invoice_path.name)
    event("TEXT_EXTRACTION", "IN_PROGRESS")
    try:
        decision = processor(invoice_path, po_path, history_path)
        event("TEXT_EXTRACTION", "COMPLETED")
        event("FIELD_EXTRACTION", "COMPLETED", "Structured invoice fields created")
        event("PO_MATCHING", "COMPLETED", "PO lookup completed")
        event("FINANCIAL_VALIDATION", "COMPLETED", f"{len(decision.checks)} checks evaluated")
        event("DECISION", "COMPLETED", decision.status)
        return ProcessingResult(str(uuid4()), asdict(decision), events)
    except Exception as exc:
        event("PROCESSING", "FAILED", str(exc))
        raise

"""Single deterministic AP policy. Evidence grades are ordinal, not probabilities."""
from dataclasses import dataclass
import os

PASS = "VERIFIED_PASS"
VIOLATION = "CONFIRMED_VIOLATION"
UNCERTAIN = "UNCERTAIN"
BLOCKED = "BLOCKED"
NA = "NOT_APPLICABLE"

RELIABILITY = {"NONE": 0, "CONTEXTUAL": 1, "DIRECT_LABEL": 2, "MANUALLY_VERIFIED": 3}

@dataclass(frozen=True)
class PolicyConfig:
    po_tolerance_pct: float = 0.0
    ocr_min_reliability: str = "DIRECT_LABEL"

    @classmethod
    def from_environment(cls):
        pct = float(os.getenv("PO_TOLERANCE_PCT", "0"))
        grade = os.getenv("OCR_MIN_RELIABILITY", "DIRECT_LABEL").upper()
        if not 0 <= pct <= 0.05: raise ValueError("PO_TOLERANCE_PCT must be between 0 and 0.05")
        if grade not in RELIABILITY: raise ValueError("Unknown OCR_MIN_RELIABILITY")
        return cls(pct, grade)

def field_reliability(invoice, field):
    meta = (invoice.get("field_metadata") or {}).get(field) or {}
    if meta.get("manual_correction"): return "MANUALLY_VERIFIED"
    return meta.get("reliability", "NONE")

def sufficient_ocr_evidence(invoice, config):
    critical = ("invoice_number", "vendor", "po_number", "date", "total", "subtotal", "tax")
    missing = [f for f in critical if RELIABILITY.get(field_reliability(invoice, f), 0) < RELIABILITY[config.ocr_min_reliability]]
    return missing

def resolve(checks):
    """Confirmed violation outranks uncertainty; only all verified passes approve."""
    if any(c.get("outcome") == VIOLATION for c in checks): return "REJECTED"
    if any(c.get("outcome") in (UNCERTAIN, BLOCKED) for c in checks): return "PENDING_REVIEW"
    return "APPROVED"

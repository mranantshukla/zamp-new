"""Explainable pre-human review based on extraction and validation evidence.

This module deliberately does not alter the AP decision. It creates a compact,
auditable recommendation for the review queue from the same document evidence
used by the decision engine; no dataset labels or invented confidence values
enter the review.
"""
from __future__ import annotations


CRITICAL_FIELDS = ("invoice_number", "vendor", "po_number", "date", "total")


def review_invoice(invoice: dict, decision) -> dict:
    metadata = invoice.get("field_metadata") or {}
    missing = [field for field in CRITICAL_FIELDS if not invoice.get(field)]
    low_evidence = [field for field in CRITICAL_FIELDS
                    if invoice.get(field) and metadata.get(field, {}).get("reliability") not in
                    ("DIRECT_LABEL", "MANUALLY_VERIFIED")]
    flagged = []
    for check in getattr(decision, "checks", []):
        outcome = check.get("outcome")
        if outcome in ("CONFIRMED_VIOLATION", "UNCERTAIN", "BLOCKED"):
            flagged.append({"check": check.get("check"), "outcome": outcome, "detail": check.get("detail")})

    status = decision.status
    if status == "APPROVED" and not missing and not low_evidence:
        recommendation = "AUTO_APPROVE"
        summary = "All critical fields have direct evidence and applicable checks passed."
    elif status == "REJECTED":
        recommendation = "CONFIRM_REJECTION"
        summary = "A confirmed business-rule violation requires AP confirmation before payment action."
    else:
        recommendation = "ESCALATE_TO_HUMAN"
        parts = []
        if missing: parts.append("Missing: " + ", ".join(missing))
        if low_evidence: parts.append("Weak source evidence: " + ", ".join(low_evidence))
        if not parts: parts.append("Business or extraction evidence requires human assessment")
        summary = "; ".join(parts) + "."

    return {
        "status": "COMPLETED",
        "recommendation": recommendation,
        "summary": summary,
        "missing_critical_fields": missing,
        "low_evidence_fields": low_evidence,
        "flagged_checks": flagged,
        "decision_preserved": status,
    }

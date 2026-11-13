"""Exception routing: which team must look at an invoice, and by when.

The decision engine says *whether* a person is needed; this module says *who*.
Routing is derived only from the decision's own checks, so it is explainable:
the first matching check in priority order picks the team.
"""
from __future__ import annotations

import os

# Plain-language names for decision checks, shown in the UI and audit views.
CHECK_LABELS = {
    "payment_change": "Changed payment details verified independently",
    "required_fields": "All key details are present",
    "ocr_field_evidence": "Each detail sits next to its printed label",
    "po_match": "Purchase order found",
    "po_authorisation": "Purchase order is open",
    "vendor_master": "Vendor is on file",
    "vendor_authorisation": "Vendor is not blocked",
    "vendor_identity": "One clear seller GSTIN",
    "vendor_match": "Vendor matches the purchase order",
    "seller_gstin": "Seller GSTIN is valid",
    "buyer_gstin": "Buyer GSTIN is valid",
    "vendor_gstin_match": "GSTIN matches the vendor on file",
    "seller_buyer_distinct": "Seller and buyer are different",
    "duplicate_detection": "Not already approved or paid",
    "duplicate_submission": "Not a resubmission of a waiting invoice",
    "possible_duplicate": "No lookalike invoice already approved",
    "invoice_date_future": "Invoice date is not in the future",
    "po_remaining": "Within the purchase order's remaining balance",
    "invoice_math": "Subtotal, tax and total add up",
    "gst_components": "GST parts add up to the tax",
    "gst_tax_type": "Consistent GST type (IGST vs CGST/SGST)",
    "gst_split_rate": "CGST and SGST rates match",
    "invoice_arithmetic_available": "Amounts available to check the maths",
    "po_balance_available": "Purchase order balance can be checked",
    "document_type": "Document is a tax invoice",
}

TEAMS = {
    "Treasury & Fraud": {"hours": 8, "handles": "Possible duplicates, GSTIN mismatches and identity problems"},
    "Vendor Management": {"hours": 48, "handles": "Unknown or blocked vendors"},
    "Procurement": {"hours": 48, "handles": "Missing, closed or over-billed purchase orders"},
    "AP Specialist": {"hours": 24, "handles": "Unreadable documents, missing details and arithmetic"},
}

# Highest-risk first: a payment-integrity concern outranks a paperwork gap.
ROUTES = (
    ("Treasury & Fraud", {"payment_change", "possible_duplicate", "vendor_gstin_match", "vendor_identity",
                          "seller_buyer_distinct", "seller_gstin", "buyer_gstin"}),
    ("Vendor Management", {"vendor_master", "vendor_authorisation"}),
    ("Procurement", {"po_match", "po_authorisation", "po_remaining", "po_balance_available", "vendor_match"}),
    ("AP Specialist", set()),  # fallback for everything else
)


def sla_hours(team: str) -> int:
    env = os.getenv("SLA_HOURS_" + team.upper().replace(" & ", "_").replace(" ", "_"))
    try:
        return int(env) if env else TEAMS[team]["hours"]
    except (ValueError, KeyError):
        return TEAMS.get(team, {"hours": 24})["hours"]


def failing_checks(checks) -> list[str]:
    return [c["check"] for c in checks or [] if c.get("outcome") not in ("VERIFIED_PASS", "ADVISORY", None)]


def route(status: str, checks) -> dict | None:
    """Team and SLA for an invoice that needs a person; None otherwise."""
    if status != "PENDING_REVIEW":
        return None
    failing = failing_checks(checks)
    for team, names in ROUTES:
        hit = next((c for c in failing if c in names), None)
        if hit or not names:
            reason = CHECK_LABELS.get(hit or (failing[0] if failing else ""), "Needs a person to confirm")
            return {"team": team, "sla_hours": sla_hours(team), "because": hit or (failing[0] if failing else None),
                    "reason": reason}
    return None

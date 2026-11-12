"""Explainable pre-human review based on extraction and validation evidence.

This module deliberately does not alter the AP decision. It creates a compact,
auditable recommendation for the review queue from the same document evidence
used by the decision engine; no dataset labels or invented confidence values
enter the review.
"""
from __future__ import annotations

import json
from urllib import request


CRITICAL_FIELDS = ("invoice_number", "vendor", "po_number", "date", "total")


def _model_second_opinion(invoice: dict, decision, config: dict) -> dict:
    """Use an OpenAI-compatible endpoint only when an operator configured it.

    Only structured fields and validation evidence are sent, never the source
    document bytes or ground-truth dataset labels. The model response remains
    advisory and is intentionally unable to alter the official AP decision.
    """
    endpoint, model, api_key = (config.get("endpoint") or "").strip(), (config.get("model") or "").strip(), config.get("api_key") or ""
    if not (endpoint and model and api_key):
        return {"status": "NOT_CONFIGURED"}
    evidence = {
        "invoice": {key: invoice.get(key) for key in ("invoice_number", "vendor", "gstin", "date", "po_number", "subtotal", "tax", "total")},
        "field_metadata": invoice.get("field_metadata") or {},
        "checks": getattr(decision, "checks", []),
        "official_decision": decision.status,
    }
    payload = {"model": model, "temperature": 0,
               "messages": [{"role": "system", "content": "You are an invoice evidence reviewer. Return concise JSON with keys summary, concerns, and recommendation. Recommendation may only be AUTO_APPROVE, CONFIRM_REJECTION, or ESCALATE_TO_HUMAN. Do not override the official decision."},
                            {"role": "user", "content": json.dumps(evidence, default=str)}]}
    try:
        req = request.Request(endpoint, data=json.dumps(payload).encode(), method="POST",
                              headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
        with request.urlopen(req, timeout=20) as response:
            data = json.loads(response.read().decode())
        content = data["choices"][0]["message"]["content"]
        if content.startswith("```"): content = content.strip("` \n").removeprefix("json\n")
        result = json.loads(content)
        return {"status": "COMPLETED", "model": model, "response": result}
    except Exception as exc:
        return {"status": "UNAVAILABLE", "model": model, "error": str(exc)[:180]}


def review_invoice(invoice: dict, decision, config: dict | None = None) -> dict:
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

    result = {
        "status": "COMPLETED",
        "recommendation": recommendation,
        "summary": summary,
        "missing_critical_fields": missing,
        "low_evidence_fields": low_evidence,
        "flagged_checks": flagged,
        "decision_preserved": status,
    }
    result["model_second_opinion"] = _model_second_opinion(invoice, decision, config or {})
    return result

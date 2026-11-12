"""Optional model-assisted reconciliation of OCR text.

The model is never given dataset labels.  It can propose fields only with an
OCR quote; proposals without a matching quote are discarded.  Deterministic
validation and the AP decision remain downstream of this module.
"""
from __future__ import annotations

import json
import os
import re
from urllib import request

from asa_invoice_processor import money

FIELDS = ("invoice_number", "vendor", "po_number", "gstin", "date", "total", "subtotal", "tax", "discount")


def _normal(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _call_model(text: str, config: dict) -> dict:
    endpoint = (config.get("endpoint") or "").strip()
    model = (config.get("model") or "").strip()
    api_key = config.get("api_key") or ""
    if not (endpoint and model and api_key):
        return {"status": "NOT_CONFIGURED", "fields": {}}
    prompt = {
        "task": "Extract invoice header fields from OCR text. Do not guess. For every value, include an exact OCR evidence quote that proves it.",
        "required_output": {"fields": {"invoice_number": {"value": "string or null", "evidence": "exact OCR quote"}, "vendor": {"value": "string or null", "evidence": "exact OCR quote"}, "po_number": {"value": "string or null", "evidence": "exact OCR quote"}, "gstin": {"value": "string or null", "evidence": "exact OCR quote"}, "date": {"value": "date exactly as printed or null", "evidence": "exact OCR quote"}, "total": {"value": "number or null", "evidence": "exact OCR quote"}, "subtotal": {"value": "number or null", "evidence": "exact OCR quote"}, "tax": {"value": "number or null", "evidence": "exact OCR quote"}, "discount": {"value": "number or null", "evidence": "exact OCR quote"}}},
        "ocr_text": text[:int(os.getenv("AI_OCR_MAX_CHARS", "12000"))],
    }
    payload = {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": "Return only valid JSON. Never infer absent data."},
                            {"role": "user", "content": json.dumps(prompt)}]}
    try:
        req = request.Request(endpoint, data=json.dumps(payload).encode(), method="POST",
                              headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
        with request.urlopen(req, timeout=float(os.getenv("AI_REQUEST_TIMEOUT", "12"))) as response:
            response_data = json.loads(response.read().decode())
        content = response_data["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.strip("` \n").removeprefix("json\n")
        return {"status": "COMPLETED", "model": model, "fields": json.loads(content).get("fields", {})}
    except Exception as exc:
        return {"status": "UNAVAILABLE", "model": model, "error": str(exc)[:180], "fields": {}}


def reconcile_ocr(invoice: dict, text: str, config: dict) -> tuple[dict, dict]:
    """Merge AI proposals only when their cited OCR evidence verifies locally."""
    result = _call_model(text, config)
    proposals = result.get("fields") if isinstance(result.get("fields"), dict) else {}
    merged = dict(invoice)
    metadata = dict(invoice.get("field_metadata") or {})
    accepted, rejected = [], []
    for field in FIELDS:
        proposed = proposals.get(field)
        if not isinstance(proposed, dict):
            continue
        value, evidence = proposed.get("value"), str(proposed.get("evidence") or "")
        if value in (None, "") or not evidence or _normal(evidence) not in _normal(text):
            rejected.append(field)
            continue
        raw_value = value
        if field in ("total", "subtotal", "tax", "discount"):
            value = money(str(value))
            value_in_evidence = value is not None and money(evidence) == value
        else:
            value_in_evidence = bool(_normal(raw_value)) and _normal(raw_value) in _normal(evidence)
        if value is None or not value_in_evidence:
            rejected.append(field)
            continue
        # Do not overwrite a deterministic, label-backed extraction.  The model
        # is useful for missing/contextual fields, not for silently replacing it.
        old = metadata.get(field, {})
        if invoice.get(field) is not None and old.get("reliability") == "DIRECT_LABEL":
            continue
        merged[field] = value
        metadata[field] = {"value": value, "evidence": evidence[:240], "method": "ai_ocr_reconciliation",
                           "reliability": "AI_CORROBORATED", "validation_status": "OCR_EVIDENCE_VERIFIED",
                           "manual_correction": False}
        accepted.append(field)
    merged["field_metadata"] = metadata
    details = dict(merged.get("details") or {})
    for key in ("vendor", "date"):
        if key in accepted: details["seller" if key == "vendor" else "invoice_date"] = merged[key]
    merged["details"] = details
    result.update(accepted_fields=accepted, rejected_fields=rejected)
    return merged, result

"""Bill approval semantics ported from akshcatt/Zamp-ai app/pipeline.py.

Maps the reference's soft findings, everyday bills, rounding and hold/reject
precedence onto this application's persisted check schema.
"""
import re
from asa_invoice_processor import normalize_identity, parse_date

SOFT = {"required_fields", "ocr_field_evidence", "invoice_date_missing", "invoice_date_future",
        "buyer_gstin", "seller_gstin", "gst_tax_type", "gst_split_rate",
        "po_match", "po_balance_available", "vendor_master"}


def small_difference(base):
    return round(min(10.0, abs(base or 0) * .02), 2)


def apply_bill_policy(inv, checks, history, po, master=()):
    checks = [dict(c) for c in checks]
    details = inv.get("details") or {}
    kind = details.get("bill_kind", "tax_invoice")
    informal = kind != "tax_invoice" or details.get("already_paid", False)
    def add(name, passed, detail, outcome="UNCERTAIN"):
        checks.append(dict(check=name, passed=passed, detail=detail, category="review",
                           outcome="VERIFIED_PASS" if passed else outcome))
    for c in checks:
        if c["passed"]: continue
        if c["check"] in SOFT or (informal and c["check"] in {"po_match", "po_balance_available", "vendor_master"}):
            c.update(outcome="ADVISORY", category="advisory")
        if c["check"] == "invoice_math":
            total, sub = inv.get("total"), inv.get("subtotal")
            if total is not None and sub is not None:
                expected = sub - abs(inv.get("discount") or 0) + (inv.get("tax") or 0) + (details.get("shipping_amount") or 0) + (details.get("rounded_off") or 0)
                if abs(total - expected) <= max(.05, small_difference(expected)):
                    c.update(passed=True, outcome="VERIFIED_PASS", detail=f"Totals differ by {abs(total-expected):.2f}, within reference rounding tolerance")
                else: c.update(outcome="UNCERTAIN", category="review")
        if c["check"] == "po_remaining": c["outcome"] = "UNCERTAIN"
        if c["check"] == "possible_duplicate": c.update(outcome="ADVISORY", category="advisory")
        if c["check"] == "duplicate_detection":
            prior = [h for h in history if normalize_identity(h.get("vendor")) == normalize_identity(inv.get("vendor"))
                     and normalize_identity(h.get("invoice_number")) == normalize_identity(inv.get("invoice_number"))
                     and h.get("status") in ("APPROVED", "PAID")]
            if prior and inv.get("total") is not None and all(h.get("total") is not None and abs(h["total"]-inv["total"]) > max(.05, small_difference(h["total"])) for h in prior):
                c.update(outcome="UNCERTAIN", detail="Same invoice number with changed amount; possible corrected invoice")
    if inv.get("total") is None or inv.get("total", 0) <= 0:
        add("amount_readable", False, "A positive readable total is required")
    if not inv.get("vendor"):
        add("payee_identified", False, "No seller name could be read")
    if not inv.get("invoice_number"):
        day, total = parse_date(inv.get("date")), inv.get("total")
        repeat = any(normalize_identity(h.get("vendor")) == normalize_identity(inv.get("vendor"))
                     and h.get("status") in ("APPROVED", "PAID") and day and parse_date(h.get("date")) == day
                     and total is not None and h.get("total") is not None
                     and abs(h["total"]-total) <= max(.05, small_difference(total)) for h in history)
        if repeat: add("duplicate_without_number", False, "Same seller, date and amount already booked", "CONFIRMED_VIOLATION")
    if (inv.get("total") or 0) >= 2500000:
        add("approval_authority", False, "Amount is at least 25 lakh; controller approval required")
    if inv.get("tax") is None and inv.get("subtotal") is not None and inv.get("total") is not None:
        expected = inv["subtotal"] - abs(inv.get("discount") or 0) + (details.get("shipping_amount") or 0) + (details.get("rounded_off") or 0)
        if abs(expected-inv["total"]) <= max(.05, small_difference(expected)):
            for c in checks:
                if c["check"] == "invoice_arithmetic_available": c.update(passed=True, outcome="VERIFIED_PASS", detail="No GST charged; subtotal and total reconcile")
    from difflib import SequenceMatcher
    record = next((m for m in master if normalize_identity(m.get("vendor")) == normalize_identity(inv.get("vendor"))), {})
    if record.get("status", "").upper() in {"APPROVED", "ACTIVE"} and inv.get("vendor"):
        for c in checks:
            if c["check"] == "vendor_identity" and not c["passed"]:
                c.update(outcome="ADVISORY", category="advisory", detail="Seller verified in supplier master; printed GST identity remains ambiguous")
    account, expected_account = details.get("account_number"), record.get("bank_account")
    if expected_account and account and str(account).replace(" ", "") != str(expected_account).replace(" ", ""):
        add("bank_account_match", False, "Printed bank account differs from the supplier account on file; verify through an established contact")
    if record.get("ifsc") and details.get("ifsc") and record["ifsc"].upper() != details["ifsc"].upper():
        add("bank_account_match", False, "Printed IFSC differs from the supplier record")
    actual_domain = (details.get("email") or "").split("@")[-1].lower()
    expected_domain = (record.get("email_domain") or "").lower()
    if actual_domain and expected_domain and actual_domain != expected_domain and not actual_domain.endswith("." + expected_domain):
        if SequenceMatcher(None, actual_domain, expected_domain).ratio() > .5:
            add("sender_domain", False, "Look-alike supplier email domain differs from the domain on file")
    if po and po.get("lines"):
        import json
        rows = json.loads(po["lines"]) if isinstance(po["lines"], str) else po["lines"]
        items = [{"description": li.get("description", ""), "quantity": li.get("quantity"), "unit_price": li.get("unit_price"), "amount": li.get("amount")} for li in inv.get("line_items") or []]
        if items and all(li["quantity"] is not None and li["amount"] is not None for li in items):
            matched = _match_lines(items, {"lines": rows})
            expected = sum((m["invoice_line"]["quantity"] or 0)*m["po_line"]["unit_price"] for m in matched if m["po_line"])
            net = (inv.get("subtotal") or sum(li["amount"] for li in items)) - abs(inv.get("discount") or 0) + (details.get("shipping_amount") or 0)
            add("po_line_match", all(m["po_line"] for m in matched) and net-expected <= min(.02*expected, 40000), "PO line price and quantity comparison")
            add("line_arithmetic", all(li["unit_price"] is None or abs(li["quantity"]*li["unit_price"]-li["amount"]) <= max(.05, small_difference(li["amount"])) for li in items), "Line quantities multiplied by unit prices must match printed amounts")
            if po.get("receipt_required"):
                add("goods_received", all(m["po_line"] and m["po_line"].get("received_qty", 0) >= m["invoice_line"]["quantity"] for m in matched), "Billed quantities must have been received")
    return checks

from difflib import SequenceMatcher
_STOP = {"of", "the", "and", "a", "an", "to", "for", "in", "x", "with", "per"}


def tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _STOP}


def text_sim(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return round(max(jac, seq * 0.9), 3)


def _match_lines(inv_lines: list, po: dict) -> list:
    out = []
    for li in inv_lines:
        best, best_s, best_text = None, 0.0, 0.0
        for pl in po["lines"]:
            ts = text_sim(li["description"], pl["description"] + " " + pl["sku"])
            # Structured signals (exact unit price, quantity within PO) outweigh wording: vendors
            # describe the same item differently ("LTL Freight CHI -> CMH" vs "LTL freight, Chicago IL to Columbus OH").
            price_eq = li["unit_price"] is not None and abs(li["unit_price"] - pl["unit_price"]) < 0.005
            qty_ok = li["quantity"] is not None and 0 < li["quantity"] <= pl["qty"]
            s = 0.5 * ts + (0.35 if price_eq else 0.0) + (0.15 if qty_ok else 0.0)
            if s > best_s:
                best, best_s, best_text = pl, s, ts
        matched = best is not None and (best_s >= 0.5 or best_text >= 0.6)
        out.append({"invoice_line": li, "po_line": best if matched else None, "score": round(best_s, 2)})
    return out




def infer_purchase_order(inv, pos):
    """Reference score: .55 line fit + .25 amount fit + .20 receipt support."""
    import json
    candidates = []
    items = inv.get("line_items") or []
    if not items: return None
    lines = [{"description": li.get("description", ""), "quantity": li.get("quantity"), "unit_price": li.get("unit_price"), "amount": li.get("amount")} for li in items]
    net = (inv.get("subtotal") or 0) - abs(inv.get("discount") or 0) + (inv.get("details", {}).get("shipping_amount") or 0)
    for po in pos:
        if normalize_identity(po.get("vendor")) != normalize_identity(inv.get("vendor")) or str(po.get("status") or "OPEN").upper() not in ("OPEN", "APPROVED"): continue
        rows = po.get("lines") or []
        if isinstance(rows, str): rows = json.loads(rows)
        if not rows: continue
        matches = _match_lines(lines, {"lines": rows})
        line_score = sum(m["score"] for m in matches if m["po_line"]) / max(1, len(matches))
        remaining = po.get("remaining", 0)
        amount_fit = 0 if net > remaining + min(.02*remaining, 40000) else max(0, 1-abs(remaining-net)/max(remaining,1))
        receipt_fit = 1 if not po.get("receipt_required") else (1 if all(m["po_line"] and m["po_line"].get("received_qty", 0) >= (m["invoice_line"]["quantity"] or 0) for m in matches) else .3)
        score = round(.55*line_score+.25*amount_fit+.20*receipt_fit, 3)
        candidates.append((score, po))
    candidates.sort(key=lambda x: -x[0])
    if candidates and candidates[0][0] >= .85 and candidates[0][0]-(candidates[1][0] if len(candidates)>1 else 0) >= .15:
        return candidates[0][1]
    return None

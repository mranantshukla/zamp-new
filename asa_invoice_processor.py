"""ASA case study solution: invoice PDF/text -> explainable AP decision.

Usage:
  python asa_invoice_processor.py --invoice samples/invoice_happy.txt --po-data samples/pos.csv
  python asa_invoice_processor.py --invoice samples/invoice_duplicate.txt --po-data samples/pos.csv --history samples/history.json
"""
from __future__ import annotations
import argparse, csv, json, re, threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from decision_policy import PolicyConfig, PASS, VIOLATION, UNCERTAIN, BLOCKED, resolve, sufficient_ocr_evidence

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None
try:
    import fitz
except Exception:
    fitz = None
try:
    from rapidocr_onnxruntime import RapidOCR
except Exception:
    RapidOCR = None
try:
    import cv2
    import numpy as np
except Exception:
    cv2 = np = None

MONEY = r"(?:[$₹€£]\s*)?([0-9][0-9, ]*(?:\.\d{2})?)"
OCR_LOCK = threading.Lock()
OCR_ENGINE = None

@dataclass
class Decision:
    status: str
    invoice: dict
    matched_po: dict | None
    checks: list[dict]
    reasons: list[str]
    next_action: str
    confidence: str
    po_matching: dict | None = None
    financial_validation: dict | None = None
    document_type: str = "UNKNOWN"

def classify_document(text: str) -> str:
    head = " ".join(text[:2500].upper().split())
    for pattern, kind in (
        (r"\bDELIVERY\s*CHALLAN\b", "DELIVERY_CHALLAN"),
        (r"\bCREDIT\s*NOTE\b", "CREDIT_NOTE"),
        (r"\bDEBIT\s*NOTE\b", "DEBIT_NOTE"),
        (r"\bPRO\s*FORMA\s*INVOICE\b", "PROFORMA_INVOICE"),
        (r"\b(?:TAX\s*INVOICE|TAXINVOICE|TAR\s*INVOICE)\b", "TAX_INVOICE"),
    ):
        if re.search(pattern, head): return kind
    if re.search(r"\bINVOICE\s*(?:NO\.?|NUMBER|#)\b", head): return "TAX_INVOICE"
    return "UNKNOWN"

def normalize_po(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())

def extract_reference(lines, pattern):
    """Only accept a reference attached to an explicit label or its next line."""
    label = re.compile(pattern, re.I)
    for i, line in enumerate(lines):
        match = label.match(line)
        if not match: continue
        tail = line[match.end():].strip(" :#.-")
        candidates = [tail] if tail else []
        if not candidates and i + 1 < len(lines): candidates.append(lines[i + 1].strip(" :#.-"))
        for candidate in candidates:
            token = re.match(r"([A-Z0-9][A-Z0-9/_-]{3,})\b", candidate, re.I)
            if token and not re.search(r"^(?:number|order|supply|rter|date|und)$", token.group(1), re.I):
                return token.group(1)
    return None

def _usable(text):
    return len(re.findall(r"[A-Za-z0-9]", text)) >= 30

def _ocr_score(text: str) -> int:
    """Rank candidate OCR output without inventing a confidence value."""
    labels = len(re.findall(r"\b(?:invoice|vendor|supplier|total|gstin|purchase\s*order|p\.?o\.?)\b", text, re.I))
    structured = len(re.findall(r"\b(?:\d{2}[A-Z0-9]{13}|[A-Z]{2,}[/-]\d+)\b", text, re.I))
    return len(re.findall(r"[A-Za-z0-9]", text)) + labels * 40 + structured * 20


def _enhanced_ocr_candidate(data: bytes) -> bytes | None:
    """Create a conservative OCR candidate only for visibly weak images.

    The source bytes always remain the first candidate. Denoising, contrast and
    deskew are applied only when OpenCV can decode a low-contrast input; callers
    compare both OCR outputs rather than assuming preprocessing helps.
    """
    if cv2 is None or np is None:
        return None
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    contrast = float(gray.std())
    # A blank or near-blank page has no document signal to enhance.
    if contrast < 2 or contrast >= 48:
        return None
    denoised = cv2.fastNlMeansDenoising(gray, None, 9, 7, 21)
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(denoised)
    # Deskew a mild scan skew. Extreme rotations are left to the OCR engine so
    # that a noisy page cannot be made worse by a brittle geometric transform.
    ink = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    points = cv2.findNonZero(ink)
    if points is not None:
        angle = cv2.minAreaRect(points)[-1]
        angle = -(90 + angle) if angle < -45 else -angle
        if 0.3 <= abs(angle) <= 12:
            height, width = enhanced.shape
            matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
            enhanced = cv2.warpAffine(enhanced, matrix, (width, height), flags=cv2.INTER_CUBIC,
                                      borderMode=cv2.BORDER_REPLICATE)
    ok, encoded = cv2.imencode(".png", enhanced)
    return encoded.tobytes() if ok else None


def _ocr_once(data: bytes) -> str:
    global OCR_ENGINE
    if RapidOCR is None:
        raise RuntimeError("RapidOCR is not installed")
    # Reuse models and serialize inference on memory-limited demo instances.
    with OCR_LOCK:
        if OCR_ENGINE is None:
            OCR_ENGINE = RapidOCR(use_angle_cls=False, det_model_path=None, det_limit_type="max",
                                  det_limit_side_len=1280, rec_model_path=None, rec_batch_num=1)
        result, _ = OCR_ENGINE(data)
    text = "\n".join(item[1] for item in (result or [])).strip()
    if not _usable(text):
        raise RuntimeError("OCR returned no usable text")
    return text


def _ocr_image(data):
    original = _ocr_once(data)
    enhanced = _enhanced_ocr_candidate(data)
    if not enhanced:
        return original
    try:
        improved = _ocr_once(enhanced)
    except RuntimeError:
        return original
    return improved if _ocr_score(improved) > _ocr_score(original) else original

def read_document(data: bytes, suffix: str):
    """Return text, method, and page count. Labels are never inputs here."""
    suffix = suffix.lower()
    if suffix in (".jpg", ".jpeg", ".png"):
        return _ocr_image(data), "ocr", 1
    if suffix == ".pdf":
        if PdfReader is None:
            raise RuntimeError("PDF text extraction requires pypdf")
        from io import BytesIO
        pages = list(PdfReader(BytesIO(data)).pages)
        text_parts = [(page.extract_text() or "").strip() for page in pages]
        scanned = [i for i, part in enumerate(text_parts) if not _usable(part)]
        if scanned:
            if fitz is None:
                raise RuntimeError("Scanned PDF rendering requires PyMuPDF")
            with fitz.open(stream=data, filetype="pdf") as document:
                for i in scanned:
                    # 300 DPI preserves small invoice text; preprocessing only
                    # runs after OCR comparison proves it is helpful.
                    pix = document[i].get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72), alpha=False)
                    text_parts[i] = _ocr_image(pix.tobytes("png"))
        return "\n".join(text_parts), "pdf+ocr" if scanned else "pdf_text", len(pages)
    if suffix == ".txt":
        text = data.decode("utf-8")
        if not text.strip(): raise RuntimeError("Empty invoice text")
        return text, "text", 1
    raise RuntimeError(f"Unsupported invoice format: {suffix}")

def read_input(path: Path) -> str:
    return read_document(path.read_bytes(), path.suffix)[0]

def normalize_identity(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())

def history_vendor(row, history_path):
    if row.get("vendor"): return row["vendor"]
    # Earlier dashboard rows stored only a bundled sample filename. Recover
    # identity from that local sample when possible; retain the row untouched.
    name = Path(str(row.get("source") or row.get("scenario") or "")).name
    sample = history_path.parent / "samples" / name
    if name.endswith(".txt") and sample.is_file():
        return extract_invoice(sample.read_text(encoding="utf-8")).get("vendor")
    return None

def valid_gstin(value):
    value = str(value or "").upper()
    if not re.fullmatch(r"[0-3][0-9][A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", value):
        return False
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    factor, total = 2, 0
    for char in reversed(value[:-1]):
        product = chars.index(char) * factor
        total += product // 36 + product % 36
        factor = 1 if factor == 2 else 2
    return chars[(36 - total % 36) % 36] == value[-1]

def money(value: str | None) -> float | None:
    if not value: return None
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d{1,2})?", value)
    return float(match.group(0).replace(",", "")) if match else None

def extract_invoice(text: str) -> dict:
    clean = " ".join(text.split())
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    amount_re = r"(?<![A-Za-z0-9])([0-9][0-9,]*(?:\.\d{1,2})?)(?![A-Za-z0-9])"

    def pick(patterns, source=clean):
        for p in patterns:
            m = re.search(p, source, re.I)
            if m: return m.group(1).strip()
        return None

    def label_value(label_pattern):
        pattern = re.compile(rf"^\s*(?:{label_pattern})(?![A-Za-z])\s*(?::|#|[-.]|no\.?\s*[:#.-]?)?\s*(.*)$", re.I)
        for index, line in enumerate(lines):
            match = pattern.match(line)
            if not match: continue
            value = match.group(1).strip(" :#.-")
            if value and value.lower() not in {"no", "n/a", "na"}: return value
            if index + 1 < len(lines):
                return lines[index + 1].strip(" :#.-")
        return None

    def block_after(label_pattern, stop_pattern):
        start = next((i for i, line in enumerate(lines) if re.search(label_pattern, line, re.I)), None)
        if start is None: return None
        values = []
        for line in lines[start + 1:]:
            if re.search(stop_pattern, line, re.I): break
            values.append(line)
        return " ".join(values[:8]) or None

    def dated(value):
        if not value: return None
        found = pick([r"(\d{4}-\d{2}-\d{2})", r"(\d{2}[/-]\d{2}[/-]\d{4})", r"(\d{2}-\d{2}-\d{4})"], value)
        if not found: return None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try: datetime.strptime(found, fmt); return found
            except ValueError: pass
        return None

    def tax_detail(name):
        for index, line in enumerate(lines):
            if not re.search(rf"\b{name}\b", line, re.I): continue
            segment_lines = []
            for candidate in lines[index:index + 6]:
                if segment_lines and re.search(r"\b(?:cgst|sgst|igst|cess|total\s*(?:tax|gst))\b", candidate, re.I): break
                segment_lines.append(candidate)
            segment = " ".join(segment_lines)
            rate = pick([r"(\d+(?:\.\d+)?)\s*%"], segment)
            without_rate = re.sub(r"\d+(?:\.\d+)?\s*%", "", segment)
            amounts = re.findall(amount_re, without_rate)
            if rate or amounts:
                chosen = amounts[-1] if re.search(r"margin|taxable", segment, re.I) and amounts else (amounts[0] if amounts else None)
                return {"rate": float(rate) if rate else None, "amount": money(chosen) if chosen else None}
        return None

    invoice_number = label_value(r"invoice\s*(?:number|no\.?|#)|inv\.?\s*(?:number|no\.?)")
    if not invoice_number or invoice_number.strip(" ,.-").lower() in {"to", "original", "copy"}:
        invoice_index = next((i for i, line in enumerate(lines) if re.search(r"invoice\s*(?:number|no)|invoice\s*no", line, re.I)), None)
        if invoice_index is not None:
            invoice_number = next((line for line in lines[invoice_index + 1:invoice_index + 5] if re.search(r"[A-Z0-9]+[/\-][A-Z0-9/\-]+", line, re.I)), invoice_number)
    po_number = extract_reference(lines, r"^(?:cust(?:omer)?\.?\s*p\.?o\.?|p\.?\s*o\.?|purchase\s*order)(?:\s*(?:number|no\.?|#))?\b")
    delivery_number = extract_reference(lines, r"^(?:delivery|challan)\s*(?:number|no\.?|#)\b")
    customer_order_number = extract_reference(lines, r"^customer\s*order\s*(?:number|no\.?|#)\b")
    # OCR occasionally inserts a space within a printed label (for example "Ve ndor").
    # Accept that narrow variation only when it is a line-leading field label.
    vendor_label = r"trade\s*/?\s*legal\s*name|ve\s*ndor|supplier|from"
    vendor = label_value(vendor_label) or label_value(r"name")
    if vendor and re.search(r"gst|pan|tax|reg\.?\s*no", vendor, re.I):
        vendor = next((line for line in lines[lines.index(vendor) + 1:] if not re.search(r"gst|pan|tax|reg\.?\s*no", line, re.I)), vendor) if vendor in lines else vendor
    if not vendor:
        tax_invoice_index = next((i for i, line in enumerate(lines) if re.search(r"tax\s*invoice", line, re.I)), len(lines))
        vendor = next((line for line in lines[:tax_invoice_index] if not re.search(r"plot|contact|email|gst|tax|address|original|copy", line, re.I) and len(line) > 2), None)
    buyer = label_value(r"bill\s*to|to")
    if not buyer or not buyer.strip(" ,.-") or re.search(r"gst|invoice|address|reg\.?\s*no|^to[,.-]*$", buyer, re.I):
        to_index = next((i for i, line in enumerate(lines) if re.match(r"^\s*to\s*[, :.-]*$", line, re.I)), None)
        if to_index is not None:
            buyer = next((line for line in lines[to_index + 1:] if not re.search(r"invoice|date|p\.?\s*o\.?|gst|address|original|copy", line, re.I) and not re.match(r"^[A-Z]{2,}/\d", line)), buyer)
    gstins = re.findall(r"(?<![A-Z0-9])\d{2}[A-Z0-9]{13}(?![A-Z0-9])", clean, re.I)
    buyer_gstins, seller_gstins = [], []
    for i, line in enumerate(lines):
        if not re.search(r"gst(?:in|\s*(?:reg\.?\s*)?no\.?)", line, re.I): continue
        candidate = re.search(r"\b\d{2}[A-Z0-9]{13}\b", line, re.I)
        if not candidate and i + 1 < len(lines):
            candidate = re.search(r"\b\d{2}[A-Z0-9]{13}\b", lines[i + 1], re.I)
        if not candidate: continue
        target = buyer_gstins if re.search(r"bill\s*to|ship\s*to|party|customer", line, re.I) else seller_gstins
        target.append(candidate.group(0).upper())
    buyer_gstin = buyer_gstins[0] if len(set(buyer_gstins)) == 1 and buyer_gstins else None
    gstin = seller_gstins[0] if len(set(seller_gstins)) == 1 and seller_gstins else None
    if not gstin and len(set(gstins)) == 1 and not buyer_gstins: gstin = gstins[0].upper()
    identity_ambiguous = len(set(seller_gstins)) > 1 or (len(set(gstins)) > 1 and not seller_gstins)
    invoice_date = dated(label_value(r"invoice\s*date|inv\.?\s*date|date"))
    due_date = dated(label_value(r"due\s*date|payment\s*due"))
    order_number = label_value(r"order\s*(?:number|no\.?)")
    customer_number = label_value(r"customer\s*(?:number|no\.?)")
    payment_terms = label_value(r"payment\s*terms|terms\s*of\s*payment")
    place_of_supply = label_value(r"place\s*of\s*supply|placeof\s*supply")
    tax_type = label_value(r"tax\s*type")
    salesperson = label_value(r"salesperson|sales\s*person")
    pan = label_value(r"pan(?:\s*no\.?)?")

    total_raw = label_value(r"grand\s*total|invoice\s*total|amount\s*due|total\s*amount|total")
    if total_raw and money(total_raw) is None:
        total_raw = next((line for line in lines[max(0, lines.index(total_raw)):][:4] if money(line) is not None), None) if total_raw in lines else None
    if not total_raw: total_raw = pick([rf"(?:grand\s*total|invoice\s*total|amount\s*due)\s*[:#-]?\s*{MONEY}"])
    total_value = money(total_raw)
    if total_value is None:
        total_value = money(pick([r"grand\s*total.*?(\d[\d,]*(?:\.\d{1,2})?)", r"\bonly\s+(\d[\d,]*(?:\.\d{1,2})?)"], clean))
    subtotal_raw = label_value(r"sub\s*-?\s*total|subtotal|taxable\s*value|net\s*before\s*tax")
    discount_raw = label_value(r"discount|rebate|less\s*:?")
    rounded_off_raw = label_value(r"rounded\s*off|round\s*off")
    tax_raw = label_value(r"total\s*tax|total\s*gst|tax\s*amount")
    if not tax_raw: tax_raw = pick([rf"(?:tax|vat|gst)\s*[:#-]?\s*{MONEY}"])
    tax_breakdown = {name.upper(): detail for name in ("cgst", "sgst", "igst", "cess") if (detail := tax_detail(name))}
    tax_value = money(tax_raw)
    explicit_tax = money(pick([r"(?:total\s*tax|total\s*gst).*?([0-9][0-9,]*\.[0-9]{2})\s+rupees"], clean))
    if explicit_tax is not None:
        tax_value = explicit_tax
    elif tax_value is None or any(abs(tax_value - (item["rate"] or -999)) < 0.001 for item in tax_breakdown.values()):
        tax_value = money(pick([r"(?:total\s*tax|total\s*gst).*?([0-9][0-9,]*\.[0-9]{2})"], clean))
    if tax_value is None:
        tax_sum = sum(item["amount"] or 0 for item in tax_breakdown.values())
        tax_value = tax_sum or None

    phone = pick([r"(?:contact|phone|mobile|ph\.?)\s*(?:no\.?)?\s*[:#-]?\s*(\+?\d[\d ()-]{7,}\d)"])
    email = pick([r"([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})"])
    ifsc = pick([r"\b([A-Z]{4}0[A-Z0-9]{6})\b"])
    account = pick([r"(?:a/c|account)\s*(?:no\.?)?\s*[:#-]?\s*(\d{6,})"])
    bank_name = label_value(r"bank\s*name")
    if not bank_name:
        bank_name = pick([r"\bbank\s*:\s*([A-Za-z][A-Za-z ]+?)(?=\s*(?:,|branch|a/c|ifsc)\b)"])
    if bank_name and bank_name.strip().lower() in {"details", "detail"}: bank_name = None
    reverse_charge = label_value(r"whether\s*tax\s*payable\s*under\s*reverse\s*charge|reverse\s*charge")
    billing_address = block_after(r"billing\s*address|bill\s*to", r"shipping\s*address|customer\s*(?:no|number)|invoice\s*(?:no|number)|gstin")
    shipping_address = block_after(r"shipping\s*address|ship\s*to", r"customer\s*(?:no|number)|invoice\s*(?:no|number)|gstin")

    line_items = []
    header_index = next((i for i, line in enumerate(lines) if re.search(r"description|particulars\s+of\s+goods", line, re.I)), None)
    if header_index is not None:
        for line in lines[header_index + 1:]:
            if re.search(r"^(sub\s*-?total|grand\s*total|total\s*gst|total\s*tax|terms|bank\s*details)", line, re.I): break
            if len(line) > 2 and not re.fullmatch(r"[0-9 .,%/-]+", line) and not re.fullmatch(r"(?:hsn|rate|dsc\s*%|price|qty|quantity|net\s*amt|gst\s*%|amount|description|particulars?)", line, re.I):
                line_items.append({"description": line})
        line_items = line_items[:20]

    details = {
        "seller": vendor, "buyer": buyer, "buyer_gstin": buyer_gstin,
        "identity_ambiguous": identity_ambiguous,
        "billing_address": billing_address, "shipping_address": shipping_address,
        "invoice_date": invoice_date, "due_date": due_date, "order_number": order_number,
        "customer_order_number": customer_order_number, "delivery_number": delivery_number,
        "customer_number": customer_number, "payment_terms": payment_terms,
        "place_of_supply": place_of_supply, "tax_type": tax_type, "salesperson": salesperson,
        "pan": pan, "phone": phone, "email": email, "reverse_charge": reverse_charge,
        "bank_name": bank_name, "account_last4": account[-4:] if account else None, "ifsc": ifsc,
        "rounded_off": money(rounded_off_raw), "tax_breakdown": tax_breakdown,
    }
    def field_evidence(key, value):
        labels = {"invoice_number": r"\binv(?:oice)?\s*(?:no\.?|number|#)", "po_number": r"\b(?:p\.?o\.?|purchase\s*order|cust\.?\s*po)\b",
                  "vendor": r"\b(?:ve\s*ndor|supplier|trade\s*/?\s*legal\s*name|from)\b",
                  "gstin": r"\b(?:gstin|gst\s*reg\.?\s*no)\b", "date": r"\b(?:invoice\s*date|inv\.?\s*date)\b",
                  "total": r"\b(?:grand\s*total|invoice\s*total|amount\s*due|total\s*amount)\b",
                  "subtotal": r"\b(?:subtotal|sub\s*-\s*total|taxable\s*value)\b", "tax": r"\b(?:total\s*tax|total\s*gst|tax\s*amount|tax)\b"}
        needle = str(value)
        if isinstance(value, float): needle = f"{value:.2f}"
        for i, line in enumerate(lines):
            if not re.search(labels[key], line, re.I): continue
            region = " ".join(lines[i:i+3])
            if needle.casefold() in region.casefold() or (isinstance(value, float) and str(int(value)) in region):
                return {"value":value, "evidence":region[:240], "method":"label_and_context",
                        "reliability":"DIRECT_LABEL", "validation_status":"NOT_CHECKED", "manual_correction":False}
        return {"value":value, "evidence":None, "method":"heuristic", "reliability":"CONTEXTUAL",
                "validation_status":"NOT_CHECKED", "manual_correction":False}
    fields = {"invoice_number":invoice_number, "po_number":po_number, "vendor":vendor, "gstin":gstin,
              "date":invoice_date, "total":total_value, "subtotal":money(subtotal_raw), "tax":tax_value}
    return {"invoice_number": invoice_number, "po_number": po_number, "vendor": vendor, "gstin": gstin,
            "date": invoice_date, "total": total_value, "subtotal": money(subtotal_raw),
            "discount": money(discount_raw), "tax": tax_value, "details": details,
            "line_items": line_items, "raw_text": clean, "document_type": classify_document(text),
            "field_metadata": {key: field_evidence(key, value) for key, value in fields.items() if value is not None}}

def load_pos(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("po_amount", "invoiced_to_date"):
            r[k] = float(r[k])
        r["remaining"] = r["po_amount"] - r["invoiced_to_date"]
    return rows

def decide(inv, po_path, history_path=None, *, method="text", vendor_master_path=None, history_rows=None):
    policy = PolicyConfig.from_environment()
    pos = load_pos(po_path)
    history = history_rows if history_rows is not None else (json.loads(history_path.read_text(encoding="utf-8")) if history_path and history_path.exists() else [])
    if not isinstance(history, list): history = []
    history = [row for row in history if row.get("event_type") != "dataset_evaluation"]
    checks, reasons = [], []
    def check(name, passed, detail, category="confirmed", outcome=None):
        state = outcome or (PASS if passed else UNCERTAIN if category == "review" else VIOLATION)
        checks.append({"check": name, "passed": passed, "detail": detail, "category": category, "outcome": state})
        if state != PASS:
            reasons.append(detail)
    required = [k for k in ("invoice_number", "po_number", "vendor", "date", "total") if not inv.get(k)]
    check("required_fields", not required, "Missing: " + ", ".join(required) if required else "All required fields present", "review")
    if "ocr" in method:
        weak = sufficient_ocr_evidence(inv, policy)
        check("ocr_field_evidence", not weak,
              "Critical OCR fields lack required source evidence: " + ", ".join(weak) if weak else
              "Critical OCR fields have direct label evidence; this is not a calibrated probability", "review")
    po_candidates = [p for p in pos if normalize_po(inv.get("po_number")) and normalize_po(p["po_number"]) == normalize_po(inv.get("po_number"))]
    po = po_candidates[0] if len(po_candidates) == 1 else None
    po_status = "NOT_IDENTIFIED" if not inv.get("po_number") else "AMBIGUOUS" if len(po_candidates) > 1 else "NOT_FOUND" if not po else "MATCHED"
    po_detail = (f"Matched {po['po_number']} against PO master" if po else
                 "No PO reference identified on document" if po_status == "NOT_IDENTIFIED" else
                 f"More than one master PO matches {inv.get('po_number')}" if po_status == "AMBIGUOUS" else
                 f"PO {inv.get('po_number')} does not exist in PO master")
    check("po_match", po is not None, po_detail, "review")
    if po and str(po.get("status") or "").upper() in ("BLOCKED", "CLOSED", "PROHIBITED"):
        check("po_authorisation", False, f"PO {po['po_number']} is {po['status']}")
    master_path = vendor_master_path or po_path.with_name("vendor_master.csv")
    if master_path.exists():
        with master_path.open(newline="", encoding="utf-8") as f: master = list(csv.DictReader(f))
    else:
        master = [{"vendor": p["vendor"], "gstin": p.get("gstin", "")} for p in pos]
    matches = [m for m in master if normalize_identity(m.get("vendor")) == normalize_identity(inv.get("vendor"))]
    check("vendor_master", bool(matches), "Vendor found in master" if matches else "Vendor identity missing or absent from master", "review")
    if any(str(m.get("status") or "").upper() in ("BLOCKED", "PROHIBITED", "UNAUTHORISED") for m in matches):
        check("vendor_authorisation", False, "Vendor is blocked in vendor master")
    if inv.get("details", {}).get("identity_ambiguous"):
        check("vendor_identity", False, "Ambiguous seller GSTIN in document", "review")
    if po:
        vendor_ok = bool(inv.get("vendor")) and normalize_identity(inv["vendor"]) == normalize_identity(po["vendor"])
        check("vendor_match", vendor_ok, f"Invoice={inv.get('vendor')}; PO={po['vendor']}",
              "review" if not matches or ("ocr" in method and not inv.get("field_metadata", {}).get("vendor", {}).get("evidence")) else "confirmed")
    seller = inv.get("gstin")
    buyer = inv.get("details", {}).get("buyer_gstin")
    if seller:
        check("seller_gstin", valid_gstin(seller), f"Seller GSTIN {seller} checksum/structure {'valid' if valid_gstin(seller) else 'invalid'}", "review" if "ocr" in method else "confirmed")
    if buyer:
        check("buyer_gstin", valid_gstin(buyer), f"Buyer GSTIN {buyer} checksum/structure {'valid' if valid_gstin(buyer) else 'invalid'}", "review")
    expected_gstins = {str(m.get("gstin") or "").upper() for m in matches if m.get("gstin")}
    if expected_gstins:
        check("vendor_gstin_match", seller in expected_gstins, f"Seller GSTIN {seller or 'missing'}; vendor master {', '.join(sorted(expected_gstins))}", "review" if not seller or "ocr" in method else "confirmed")
    if seller and buyer:
        check("seller_buyer_distinct", seller != buyer, "Seller and buyer GSTIN must be distinct", "review")
    key = (normalize_identity(inv.get("vendor")), normalize_identity(inv.get("invoice_number")))
    previous = [r for r in history if bool(all(key)) and
                (normalize_identity(r.get("vendor") or (history_vendor(r, history_path) if history_path else None)),
                 normalize_identity(r.get("invoice_number"))) == key]
    duplicate = any(r.get("status") in ("APPROVED", "PAID") for r in previous)
    repeated_pending = not duplicate and any(r.get("status") == "PENDING_REVIEW" for r in previous)
    check("duplicate_detection", not duplicate, "No prior approved or paid invoice with this vendor and number" if not duplicate else
          "Same vendor and invoice number already approved or paid")
    if repeated_pending:
        check("duplicate_submission", False, "Previously submitted invoice is still pending review", "review")
    if po and inv.get("total") is not None:
        prior = sum(float(r.get("total") or 0) for r in history if r.get("status") == "APPROVED" and normalize_identity(r.get("po_number")) == normalize_identity(po["po_number"]) and r.get("total") is not None)
        remaining = max(0, po["remaining"] - prior)
        po["remaining"] = remaining
        allowed = remaining + po["po_amount"] * policy.po_tolerance_pct
        within = inv["total"] > 0 and inv["total"] <= allowed + 0.01
        check("po_remaining", within, f"Invoice={inv['total']:.2f}; remaining PO={remaining:.2f}; prior approvals={prior:.2f}; tolerance={policy.po_tolerance_pct:.1%}",
              "review" if "ocr" in method and not within else "confirmed")
        if not within: po_status = "BLOCKED"
    if inv.get("subtotal") is not None and inv.get("total") is not None and inv.get("tax") is not None:
        expected = inv["subtotal"] - (inv.get("discount") or 0) + inv["tax"] + (inv.get("details", {}).get("rounded_off") or 0)
        math_ok = abs(expected - inv["total"]) <= 0.01
        check("invoice_math", math_ok, f"Computed={expected:.2f}; stated={inv['total']:.2f}", "review" if "ocr" in method else "confirmed")
    components = inv.get("details", {}).get("tax_breakdown") or {}
    amounts = [item.get("amount") for item in components.values()]
    if inv.get("tax") is not None and amounts and all(amount is not None for amount in amounts):
        component_total = sum(amounts)
        check("gst_components", abs(component_total - inv["tax"]) <= 0.01,
              f"GST components={component_total:.2f}; stated tax={inv['tax']:.2f}",
              "review" if "ocr" in method else "confirmed")
    if "IGST" in components and ("CGST" in components or "SGST" in components):
        check("gst_tax_type", False, "Both IGST and CGST/SGST appear on the bill; verify tax treatment", "review")
    if "CGST" in components and "SGST" in components:
        cgst_rate, sgst_rate = components["CGST"].get("rate"), components["SGST"].get("rate")
        if cgst_rate is not None and sgst_rate is not None:
            check("gst_split_rate", abs(cgst_rate - sgst_rate) <= 0.001,
                  f"CGST rate={cgst_rate}%; SGST rate={sgst_rate}%", "review" if "ocr" in method else "confirmed")
    financial_names = ("po_remaining", "invoice_math", "gst_components", "gst_tax_type", "gst_split_rate")
    financial_checks = [item for item in checks if item["check"] in financial_names]
    if inv.get("subtotal") is None or inv.get("tax") is None or inv.get("total") is None:
        missing = [key for key in ("subtotal", "tax", "total") if inv.get(key) is None]
        check("invoice_arithmetic_available", False, "Cannot validate invoice arithmetic: missing " + ", ".join(missing), "review", BLOCKED)
        financial_checks.append(checks[-1])
    if not po:
        check("po_balance_available", False, "PO balance validation blocked: " + po_detail, "review", BLOCKED)
        financial_checks.append(checks[-1])
    financial_status = ("FAILED" if any(c["outcome"] == VIOLATION for c in financial_checks)
                        else "BLOCKED_MISSING_DATA" if any(c["check"] in ("invoice_arithmetic_available", "po_balance_available") for c in financial_checks)
                        else "PARTIALLY_VALIDATED" if any(not c["passed"] for c in financial_checks)
                        else "PASSED" if financial_checks else "NOT_APPLICABLE")
    document_type = inv.get("document_type") or (classify_document(inv.get("raw_text", "")) if inv.get("raw_text") else "TAX_INVOICE")
    if document_type != "TAX_INVOICE":
        check("document_type", False, f"{document_type} is not eligible for automatic invoice approval", "review")
    status = resolve(checks)
    if status == "REJECTED":
        status, action, confidence = "REJECTED", "Route to AP exception queue", "HIGH"
    elif status == "PENDING_REVIEW":
        status, action, confidence = "PENDING_REVIEW", "Verify extraction and vendor/PO evidence", "LOW"
    else:
        status, action, confidence = "APPROVED", "Queue for payment approval", "HIGH"
    po_matching = {"status": po_status, "evidence": inv.get("po_number"), "explanation": po_detail,
                   "matched_po": po.get("po_number") if po else None}
    financial = {"status": financial_status,
                 "checks": [{**c, "financial_outcome": "PASSED" if c["passed"] else "FAILED" if c["outcome"] == VIOLATION else "BLOCKED"}
                            for c in financial_checks]}
    return Decision(status, {k:v for k,v in inv.items() if k != "raw_text"}, po, checks, reasons, action, confidence,
                    po_matching, financial, document_type)

def process(invoice_path: Path, po_path: Path, history_path: Path | None, tolerance=0.02, vendor_master_path=None) -> Decision:
    text, method, _ = read_document(invoice_path.read_bytes(), invoice_path.suffix)
    return decide(extract_invoice(text), po_path, history_path, method=method, vendor_master_path=vendor_master_path)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--invoice", required=True); ap.add_argument("--po-data", required=True); ap.add_argument("--history")
    args = ap.parse_args(); d = process(Path(args.invoice), Path(args.po_data), Path(args.history) if args.history else None)
    print(json.dumps(asdict(d), indent=2))

if __name__ == "__main__": main()

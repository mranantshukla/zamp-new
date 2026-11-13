"""PO and vendor master data: validation, storage and fallback.

Master data uploaded through the dashboard is stored in the database so that it
survives restarts on hosts with ephemeral disks (Render). When nothing has been
uploaded, the bundled CSV files under ``samples/`` remain the source.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import storage
from asa_invoice_processor import normalize_identity, normalize_po, valid_gstin

MAX_BYTES = 1024 * 1024
MAX_ROWS = 10000
KINDS = {
    "po_master": {"file": "pos.csv", "required": ("po_number", "vendor", "po_amount", "invoiced_to_date"),
                  "optional": ("status", "gstin", "currency")},
    "vendor_master": {"file": "vendor_master.csv", "required": ("vendor",),
                      "optional": ("gstin", "status")},
}
PO_STATUSES = {"", "OPEN", "APPROVED", "BLOCKED", "CLOSED", "PROHIBITED"}
VENDOR_STATUSES = {"", "APPROVED", "ACTIVE", "BLOCKED", "PROHIBITED", "UNAUTHORISED"}


class MasterDataError(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


def parse(kind: str, content: str) -> list[dict]:
    """Validate CSV content and return clean rows; raise MasterDataError."""
    spec = KINDS[kind]
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise MasterDataError(["Master data file exceeds 1 MB"])
    reader = csv.DictReader(io.StringIO(content.lstrip("﻿")))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [h for h in spec["required"] if h not in headers]
    if missing:
        raise MasterDataError(["Missing required column(s): " + ", ".join(missing)])
    errors, rows, seen = [], [], set()
    for line, raw in enumerate(reader, start=2):
        if len(rows) >= MAX_ROWS:
            errors.append(f"More than {MAX_ROWS} rows"); break
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if not any(row.values()):
            continue
        clean = {k: row.get(k, "") for k in spec["required"] + spec["optional"]}
        if not clean["vendor"]:
            errors.append(f"Row {line}: vendor is required")
        if clean.get("gstin"):
            clean["gstin"] = clean["gstin"].upper()
            if not valid_gstin(clean["gstin"]):
                errors.append(f"Row {line}: GSTIN {clean['gstin']} fails structure/checksum validation")
        status = clean.get("status", "").upper()
        clean["status"] = status
        if kind == "po_master":
            key = normalize_po(clean["po_number"])
            if not key:
                errors.append(f"Row {line}: po_number is required")
            elif key in seen:
                errors.append(f"Row {line}: duplicate PO {clean['po_number']}")
            seen.add(key)
            for field in ("po_amount", "invoiced_to_date"):
                try:
                    value = float(clean[field].replace(",", ""))
                    if value < 0: raise ValueError
                    clean[field] = value
                except ValueError:
                    errors.append(f"Row {line}: {field} must be a non-negative number")
            if isinstance(clean["po_amount"], float) and isinstance(clean["invoiced_to_date"], float) \
                    and clean["invoiced_to_date"] > clean["po_amount"] + 0.01:
                errors.append(f"Row {line}: invoiced_to_date exceeds po_amount")
            if status not in PO_STATUSES:
                errors.append(f"Row {line}: unknown PO status {status}")
        else:
            key = normalize_identity(clean["vendor"])
            if key and key in seen:
                errors.append(f"Row {line}: duplicate vendor {clean['vendor']}")
            seen.add(key)
            if status not in VENDOR_STATUSES:
                errors.append(f"Row {line}: unknown vendor status {status}")
        rows.append(clean)
    if errors:
        raise MasterDataError(errors)
    if not rows:
        raise MasterDataError(["Master data contains no rows"])
    return rows


def to_csv(kind: str, rows: list[dict]) -> str:
    spec = KINDS[kind]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(spec["required"] + spec["optional"]), extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return out.getvalue()


def load(kind: str, samples_dir: Path) -> tuple[list[dict] | None, dict]:
    """Return (rows, info). rows is None when no master of this kind exists,
    which lets the decision engine apply its documented fallback."""
    stored = storage.get_master(kind)
    if stored:
        return parse(kind, stored["content"]), {"source": "database", "updated_by": stored["updated_by"],
                                                "updated_at": stored["updated_at"].isoformat()}
    path = samples_dir / KINDS[kind]["file"]
    if path.is_file():
        return parse(kind, path.read_text(encoding="utf-8")), {"source": "file", "path": path.name}
    return None, {"source": "none"}


def save(kind: str, content: str, updated_by: str) -> list[dict]:
    rows = parse(kind, content)
    storage.save_master(kind, to_csv(kind, rows), updated_by)
    return rows


def decision_masters(samples_dir: Path) -> dict:
    """Master rows for decide(); a missing kind falls back inside decide()."""
    masters = {}
    for kind in KINDS:
        stored = storage.get_master(kind)
        if stored:
            masters[kind] = parse(kind, stored["content"])
    return masters

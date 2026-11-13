"""Transactional run/document storage. PostgreSQL in deployment, SQLite for local tests."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (create_engine, MetaData, Table, Column, String, DateTime, Float, Index,
                        LargeBinary, Integer, Text, ForeignKey, select, insert, update, text, func,
                        inspect, or_, and_)

ROOT = Path(__file__).parent
url = os.getenv("DATABASE_URL") or "sqlite:///" + str(ROOT / "asa_local.db").replace("\\", "/")
if url.startswith("postgres://"):
    url = "postgresql://" + url[len("postgres://"):]
if url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(url, pool_pre_ping=True, future=True)
decision_lock = threading.Lock()
meta = MetaData()
migrations = Table("schema_migrations", meta, Column("version", Integer, primary_key=True))
documents = Table("documents", meta, Column("id", String(36), primary_key=True),
                  Column("filename", String(180), nullable=False), Column("mime", String(80), nullable=False),
                  Column("content", LargeBinary, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False))
runs = Table("processing_runs", meta, Column("id", String(36), primary_key=True),
             Column("document_id", String(36), ForeignKey("documents.id")),
             Column("created_at", DateTime(timezone=True), nullable=False),
             Column("source", String(180), nullable=False), Column("event_type", String(40), nullable=False),
             Column("status", String(30), nullable=False), Column("payload", Text, nullable=False),
             # Schema v2 summary columns. The JSON payload stays the source of
             # truth for evidence; these copies make ledger lookups, filters and
             # analytics indexable instead of parsing every payload per request.
             Column("vendor", String(200)), Column("vendor_key", String(200)),
             Column("invoice_key", String(200)), Column("po_key", String(100)),
             Column("total", Float), Column("invoice_date", String(40)), Column("due_date", String(40)),
             Column("processing_seconds", Float), Column("routing", String(30)),
             Column("archived", Integer, nullable=False, server_default="0"))
SUMMARY_COLUMNS = ("vendor", "vendor_key", "invoice_key", "po_key", "total", "invoice_date",
                   "due_date", "processing_seconds", "routing", "archived")
stages = Table("processing_stages", meta, Column("id", Integer, primary_key=True, autoincrement=True),
               Column("run_id", String(36), ForeignKey("processing_runs.id"), nullable=False),
               Column("stage", String(50), nullable=False), Column("status", String(15), nullable=False),
               Column("detail", Text, nullable=False), Column("at", DateTime(timezone=True), nullable=False))
reviews = Table("manual_reviews", meta, Column("id", String(36), primary_key=True),
                Column("run_id", String(36), ForeignKey("processing_runs.id"), nullable=False),
                Column("reviewer", String(80), nullable=False), Column("outcome", String(30), nullable=False),
                Column("note", Text, nullable=False), Column("corrections", Text, nullable=False),
                Column("revised_decision", Text), Column("at", DateTime(timezone=True), nullable=False))
master_data = Table("master_data", meta, Column("kind", String(40), primary_key=True),
                    Column("content", Text, nullable=False), Column("updated_by", String(80), nullable=False),
                    Column("updated_at", DateTime(timezone=True), nullable=False))
ocr_cache = Table("ocr_cache", meta, Column("key", String(100), primary_key=True),
                  Column("text", Text, nullable=False), Column("method", String(20), nullable=False),
                  Column("pages", Integer, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False))

INDEXES = (Index("ix_runs_created_at", runs.c.created_at), Index("ix_runs_status_created", runs.c.status, runs.c.created_at),
           Index("ix_runs_vendor_key", runs.c.vendor_key), Index("ix_runs_po_key", runs.c.po_key),
           Index("ix_stages_run_id", stages.c.run_id, stages.c.id), Index("ix_reviews_run_id", reviews.c.run_id))
SCHEMA_VERSION = 2

def now(): return datetime.now(timezone.utc)

def _key(value):
    """Same normalisation as asa_invoice_processor.normalize_identity."""
    import re
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold()) or None

def _number(value):
    try: return None if value in (None, "") else float(value)
    except (TypeError, ValueError): return None

def summary_values(payload):
    """Indexable columns derived from a run payload."""
    timings = payload.get("timings") or {}
    return {"vendor": (str(payload["vendor"])[:200] if payload.get("vendor") else None),
            "vendor_key": (_key(payload.get("vendor")) or "")[:200] or None,
            "invoice_key": (_key(payload.get("invoice_number")) or "")[:200] or None,
            "po_key": (_key(payload.get("po_number")) or "")[:100] or None,
            "total": _number(payload.get("total")),
            "invoice_date": str(payload.get("date") or (payload.get("invoice") or {}).get("date") or "")[:40] or None,
            "due_date": str(payload.get("due_date") or "")[:40] or None,
            "processing_seconds": _number(timings.get("total_seconds")),
            "routing": (payload.get("document_routing") or {}).get("status"),
            "archived": 1 if payload.get("archived") else 0}

def initialize():
    """Create or additively migrate the schema. Existing data is never dropped."""
    with engine.begin() as conn:
        meta.create_all(conn)
        version = conn.execute(select(func.max(migrations.c.version))).scalar()
        if version is None:
            conn.execute(insert(migrations).values(version=1)); version = 1
        if version < 2:
            # v2: summary columns, indexes and master data. create_all() does not
            # add columns to an existing table, so add any that are missing.
            existing = {c["name"] for c in inspect(conn).get_columns("processing_runs")}
            for name in SUMMARY_COLUMNS:
                if name in existing: continue
                ddl = runs.c[name].type.compile(dialect=conn.dialect)
                default = " NOT NULL DEFAULT 0" if name == "archived" else ""
                conn.exec_driver_sql(f"ALTER TABLE processing_runs ADD COLUMN {name} {ddl}{default}")
            for row in conn.execute(select(runs.c.id, runs.c.payload)).mappings():
                conn.execute(update(runs).where(runs.c.id == row["id"])
                             .values(**summary_values(json.loads(row["payload"] or "{}"))))
            conn.execute(insert(migrations).values(version=2))
        for index in INDEXES:
            index.create(conn, checkfirst=True)

def recover_interrupted():
    """Close runs left active by a terminated single-worker process."""
    with engine.begin() as conn:
        for row in conn.execute(select(runs).where(runs.c.status == "RUNNING")).mappings():
            payload = json.loads(row["payload"] or "{}")
            payload.update(error="Worker stopped before processing completed",
                           reasons=["Processing interrupted; resubmit for review"])
            conn.execute(update(runs).where(runs.c.id == row["id"]).values(
                status="FAILED", payload=json.dumps(payload)))
            conn.execute(insert(stages).values(run_id=row["id"], stage="Processing interrupted", status="FAILED",
                                               detail="Worker stopped before processing completed", at=now()))

def create_run(source, event_type, document=None, mime=None):
    rid = str(uuid.uuid4()); did = str(uuid.uuid4()) if document is not None else None
    with engine.begin() as conn:
        if did:
            conn.execute(insert(documents).values(id=did, filename=source, mime=mime, content=document, created_at=now()))
        conn.execute(insert(runs).values(id=rid, document_id=did, created_at=now(), source=source,
                                         event_type=event_type, status="RUNNING", payload="{}"))
    return rid

def cached_ocr(key):
    with engine.connect() as conn:
        row = conn.execute(select(ocr_cache.c.text, ocr_cache.c.method, ocr_cache.c.pages)
                           .where(ocr_cache.c.key == key)).first()
        return tuple(row) if row else None

def save_ocr(key, value):
    from sqlalchemy.exc import IntegrityError
    try:
        with engine.begin() as conn:
            conn.execute(insert(ocr_cache).values(key=key, text=value[0], method=value[1],
                                                  pages=value[2], created_at=now()))
    except IntegrityError:
        pass  # A concurrent run stored the same document first.

def add_stage(rid, stage, status, detail=""):
    with engine.begin() as conn:
        conn.execute(insert(stages).values(run_id=rid, stage=stage, status=status, detail=detail, at=now()))

def finish_run(rid, payload, status):
    with engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == rid).values(
            payload=json.dumps(payload, default=str), status=status, **summary_values(payload)))

# Bulky evidence omitted from list responses; the full run is at /api/runs/<id>.
HEAVY_KEYS = ("invoice", "decision", "ai_review", "ai_extraction", "comparisons")

def _row(row, slim=False):
    value = {k: v for k, v in dict(row).items() if k not in SUMMARY_COLUMNS}
    payload = json.loads(value.pop("payload") or "{}")
    if slim:
        for key in HEAVY_KEYS: payload.pop(key, None)
    value.update(payload)
    value["archived"] = bool(row.get("archived") or payload.get("archived"))
    value["run_id"] = value.pop("id")
    value["time"] = value.pop("created_at").isoformat()
    return value

def get_run(rid):
    with engine.connect() as conn:
        row = conn.execute(select(runs).where(runs.c.id == rid)).mappings().first()
        if not row: return None
        result = _row(row)
        result["stages"] = [dict(s) for s in conn.execute(select(stages.c.stage, stages.c.status, stages.c.detail, stages.c.at)
                                                       .where(stages.c.run_id == rid).order_by(stages.c.id)).mappings()]
        result["reviews"] = [dict(r) for r in conn.execute(select(reviews.c.reviewer, reviews.c.outcome, reviews.c.note,
                                                                    reviews.c.corrections, reviews.c.revised_decision, reviews.c.at)
                                                        .where(reviews.c.run_id == rid).order_by(reviews.c.at)).mappings()]
        return result

def _list_query(include_archived=False, status=None, day=None, search=None):
    query = select(runs)
    if not include_archived: query = query.where(runs.c.archived == 0)
    if status: query = query.where(runs.c.status == status)
    if day:
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        query = query.where(runs.c.created_at >= start, runs.c.created_at < start + timedelta(days=1))
    if search and search.isascii():
        # Coarse SQL pre-filter; callers apply the exact field filter afterwards.
        pattern = "%" + search.replace("\\", "").replace("%", "").replace("_", "") + "%"
        query = query.where(or_(runs.c.payload.ilike(pattern), runs.c.source.ilike(pattern), runs.c.id.ilike(pattern),
                                 runs.c.status.ilike(pattern)))
    return query

def list_runs(limit=300, include_archived=False, *, offset=0, status=None, day=None, search=None, slim=False):
    """Newest-first runs. Filtering and pagination happen in SQL."""
    with engine.connect() as conn:
        query = _list_query(include_archived, status, day, search)
        result = conn.execute(query.order_by(runs.c.created_at.desc(), runs.c.id).limit(limit).offset(offset)).mappings()
        rows = [_row(r, slim=slim) for r in result]
        if slim and rows:
            # Attach only the latest review per run, in one query, for list views.
            latest = {}
            for r in conn.execute(select(reviews.c.run_id, reviews.c.reviewer, reviews.c.outcome, reviews.c.at)
                                  .where(reviews.c.run_id.in_([r["run_id"] for r in rows]))
                                  .order_by(reviews.c.at)).mappings():
                latest[r["run_id"]] = {"reviewer": r["reviewer"], "outcome": r["outcome"], "at": r["at"].isoformat()}
            for row in rows:
                row["reviews"] = [latest[row["run_id"]]] if row["run_id"] in latest else []
        return rows

def count_runs(include_archived=False, status=None, day=None):
    with engine.connect() as conn:
        query = _list_query(include_archived, status, day).with_only_columns(func.count()).order_by(None)
        return conn.execute(query).scalar() or 0

def archive_history():
    """Hide completed runs from the dashboard while retaining the immutable AP ledger."""
    with engine.begin() as conn:
        return conn.execute(update(runs).where(runs.c.status != "RUNNING", runs.c.archived == 0)
                            .values(archived=1)).rowcount

def run_status(rid):
    with engine.connect() as conn:
        return conn.execute(select(runs.c.status).where(runs.c.id == rid)).scalar()

def stages_after(rid, after_id=0):
    """Stage events newer than after_id; used by the live event stream."""
    with engine.connect() as conn:
        return [dict(s) for s in conn.execute(
            select(stages.c.id, stages.c.stage, stages.c.status, stages.c.detail, stages.c.at)
            .where(stages.c.run_id == rid, stages.c.id > after_id).order_by(stages.c.id)).mappings()]

def _decision_history(conn, invoice, exclude=None):
    """Only the ledger rows that can affect this decision: same vendor (duplicate
    checks) or same PO (cumulative balance). Indexed, so cost does not grow with
    total history size."""
    vendor_key, po_key = _key(invoice.get("vendor")), _key(invoice.get("po_number"))
    related = [c for c in ((runs.c.vendor_key == vendor_key) if vendor_key else None,
                           (runs.c.po_key == po_key) if po_key else None) if c is not None]
    if not related: return []
    query = (select(runs.c.id, runs.c.status, runs.c.event_type, runs.c.vendor_key, runs.c.invoice_key,
                    runs.c.po_key, runs.c.total, runs.c.invoice_date)
             .where(runs.c.status != "RUNNING", runs.c.event_type != "dataset_evaluation", or_(*related)))
    if exclude: query = query.where(runs.c.id != exclude)
    return [{"run_id": r["id"], "status": r["status"], "event_type": r["event_type"], "vendor": r["vendor_key"],
             "invoice_number": r["invoice_key"], "po_number": r["po_key"], "total": r["total"],
             "date": r["invoice_date"]} for r in conn.execute(query).mappings()]

def decision_history(invoice, exclude=None):
    with engine.connect() as conn:
        return _decision_history(conn, invoice, exclude)

def document_for_run(rid):
    with engine.connect() as conn:
        return conn.execute(select(documents.c.content, documents.c.mime, documents.c.filename)
                            .join(runs, runs.c.document_id == documents.c.id).where(runs.c.id == rid)).first()

def record_review(rid, reviewer, outcome, note, corrections, revised_decision=None):
    with engine.begin() as conn:
        if not conn.execute(select(runs.c.id).where(runs.c.id == rid)).first(): raise ValueError("Run not found")
        conn.execute(insert(reviews).values(id=str(uuid.uuid4()), run_id=rid, reviewer=reviewer, outcome=outcome,
                                            note=note, corrections=json.dumps(corrections),
                                            revised_decision=json.dumps(revised_decision, default=str) if revised_decision else None,
                                            at=now()))

def locked_decision(rid, invoice, po_path, method, extra=None, finalize=None, masters=None):
    """Serialize PO consumption and duplicate decisions with the run commit."""
    from asa_invoice_processor import decide
    masters = masters or {}
    with decision_lock:
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql": conn.execute(text("SELECT pg_advisory_xact_lock(9183415)"))
            history = _decision_history(conn, invoice)
            decision = decide(invoice, po_path, method=method, history_rows=history,
                              pos=masters.get("po_master"), vendor_master=masters.get("vendor_master"))
            final_extra, final_stages = finalize(decision) if finalize else ({}, [])
            payload = {"invoice": decision.invoice, "invoice_number": invoice.get("invoice_number"),
                       "vendor": invoice.get("vendor"), "gstin": invoice.get("gstin"),
                       "po_number": invoice.get("po_number"), "total": invoice.get("total"),
                       "due_date": (invoice.get("details") or {}).get("due_date"),
                       "document_type": decision.document_type, "method": method,
                       "decision": decision.__dict__, "reasons": decision.reasons,
                       **(extra or {}), **final_extra}
            for name, status, detail in final_stages:
                conn.execute(insert(stages).values(run_id=rid, stage=name, status=status,
                                                   detail=detail, at=now()))
            conn.execute(update(runs).where(runs.c.id == rid).values(
                payload=json.dumps(payload, default=str), status=decision.status, **summary_values(payload)))
            return decision

def get_master(kind):
    with engine.connect() as conn:
        row = conn.execute(select(master_data).where(master_data.c.kind == kind)).mappings().first()
        return dict(row) if row else None

def save_master(kind, content, updated_by):
    with engine.begin() as conn:
        values = dict(content=content, updated_by=updated_by, updated_at=now())
        if conn.execute(update(master_data).where(master_data.c.kind == kind).values(**values)).rowcount == 0:
            conn.execute(insert(master_data).values(kind=kind, **values))

def approved_by_po():
    """Approved invoice value per normalised PO reference, for PO utilisation."""
    with engine.connect() as conn:
        return {r[0]: (r[1] or 0.0, r[2]) for r in conn.execute(
            select(runs.c.po_key, func.sum(runs.c.total), func.count())
            .where(runs.c.status == "APPROVED", runs.c.event_type != "dataset_evaluation", runs.c.po_key.is_not(None))
            .group_by(runs.c.po_key))}

def analytics_rows(include_archived=True):
    """Lightweight column-only rows for analytics; never parses payloads."""
    query = select(runs.c.id, runs.c.status, runs.c.event_type, runs.c.vendor, runs.c.vendor_key, runs.c.total,
                   runs.c.due_date, runs.c.invoice_date, runs.c.processing_seconds, runs.c.routing,
                   runs.c.created_at, runs.c.source)
    if not include_archived: query = query.where(runs.c.archived == 0)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(query).mappings()]

def import_legacy(path, manual_path=None):
    """Idempotent optional import, invoked explicitly by the migration command."""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    manual = json.loads(Path(manual_path).read_text(encoding="utf-8")) if manual_path and Path(manual_path).exists() else {}
    with engine.begin() as conn:
        for item in rows:
            rid = str(item.get("run_id") or uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(item, sort_keys=True)))
            if conn.execute(select(runs.c.id).where(runs.c.id == rid)).first(): continue
            payload = {k:v for k,v in item.items() if k not in ("run_id", "time", "source", "status", "event_type")}
            if rid in manual: payload["legacy_manual_check"] = manual[rid]
            try: created = datetime.fromisoformat(item["time"])
            except Exception: created = now()
            conn.execute(insert(runs).values(id=rid, document_id=None, created_at=created,
                                             source=item.get("source") or item.get("scenario") or "Legacy run",
                                             event_type=item.get("event_type") or "legacy",
                                             status=item.get("status") or "PENDING_REVIEW", payload=json.dumps(payload),
                                             **summary_values(payload)))

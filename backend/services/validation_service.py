"""Validation boundary. Financial decisions remain deterministic."""
from asa_invoice_processor import process

def validate(invoice_path, po_path, history_path=None):
    return process(invoice_path, po_path, history_path)

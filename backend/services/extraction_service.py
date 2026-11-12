"""Extraction boundary. OCR can be added here without changing decision rules."""
from pathlib import Path
from asa_invoice_processor import read_input, extract_invoice

def extract(path: Path) -> dict:
    return extract_invoice(read_input(path))

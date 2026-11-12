"""Run every cached AjitRawat invoice image through the production OCR pipeline.

Labels are loaded only after extraction to produce a benchmark report; they are
never sent to `extract_invoice` or `decide`.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from asa_dashboard import DATASET_IMAGES, dataset_rows, evaluate_dataset
from asa_invoice_processor import decide, extract_invoice, read_document


def run_all(output: Path | None = None, include_decisions: bool = True) -> dict:
    rows = dataset_rows()
    report, outcomes, decisions, started = [], Counter(), Counter(), time.monotonic()
    for index, wrapped in enumerate(rows, 1):
        image = DATASET_IMAGES / f"invoice_{index:02d}.jpg"
        item = {"sample": index, "source": image.name}
        try:
            text, method, pages = read_document(image.read_bytes(), image.suffix)
            invoice = extract_invoice(text)
            header = json.loads(wrapped["row"]["ground_truth"]).get("gt_parse", {}).get("header", {})
            comparisons, accuracy = evaluate_dataset(invoice, header)
            field_outcomes = {field: outcome for field, _, _, outcome in comparisons}
            outcomes.update(field_outcomes.values())
            item.update(method=method, pages=pages, document_type=invoice["document_type"],
                        field_accuracy=accuracy, fields={key: invoice.get(key) for key in
                        ("invoice_number", "vendor", "po_number", "date", "total", "subtotal", "tax")},
                        outcomes=field_outcomes)
            if include_decisions:
                # Each document is assessed independently. This reports whether
                # it contains enough AP evidence; it does not fabricate a PO or
                # carry one dataset sample's approval into the next.
                decision = decide(invoice, Path("samples") / "pos.csv", method=method, history_rows=[])
                item.update(decision=decision.status, reasons=decision.reasons)
                decisions[decision.status] += 1
        except Exception as exc:
            item.update(error=str(exc))
        report.append(item)
    comparable = outcomes["Exact match"] + outcomes["Mismatch"] + outcomes["Extraction missing"]
    result = {"samples": len(report), "seconds": round(time.monotonic() - started, 2),
              "field_outcomes": dict(outcomes),
              "decision_distribution": dict(decisions),
              "field_exact_match_accuracy": round(outcomes["Exact match"] / comparable, 4) if comparable else None,
              "runs": report}
    if output:
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="dataset_benchmark.json")
    parser.add_argument("--extraction-only", action="store_true",
                        help="skip AP decisions and report OCR extraction only")
    args = parser.parse_args()
    result = run_all(Path(args.output), include_decisions=not args.extraction_only)
    print(json.dumps({key: value for key, value in result.items() if key != "runs"}, indent=2))

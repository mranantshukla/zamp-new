"""Decision boundary kept separate for future policy/versioning."""
def summarize(decision):
    return {"status": decision.status, "confidence": decision.confidence,
            "reasons": decision.reasons, "next_action": decision.next_action}

from dataclasses import dataclass, field
from typing import Any

@dataclass
class ProcessingEvent:
    stage: str
    status: str
    detail: str = ""

@dataclass
class ProcessingResult:
    run_id: str
    decision: dict[str, Any]
    events: list[ProcessingEvent] = field(default_factory=list)

    def as_dict(self):
        return {"run_id": self.run_id, "decision": self.decision,
                "events": [e.__dict__ for e in self.events]}

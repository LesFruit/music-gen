from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(slots=True)
class InputInfo:
    path: str
    duration_s: float
    sr: int


@dataclass(slots=True)
class JobManifest:
    job_id: str
    input: InputInfo
    artifacts: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)

    @classmethod
    def create(
        cls, input_path: Path, duration_s: float, sr: int, job_id: str | None = None
    ) -> "JobManifest":
        return cls(
            job_id=job_id or str(uuid.uuid4()),
            input=InputInfo(path=str(input_path), duration_s=float(duration_s), sr=int(sr)),
        )

    def add_artifact(self, key: str, path: Path) -> None:
        self.artifacts[key] = str(path)

    def add_metric(self, key: str, value: float) -> None:
        self.metrics[key] = float(value)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def add_decision(self, message: str) -> None:
        self.decisions.append(message)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["input"]["duration_s"] = round(payload["input"]["duration_s"], 4)
        return payload

    def write(self, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

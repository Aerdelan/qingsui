from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Literal


JobStatus = Literal["queued", "running", "completed", "failed"]
JobKind = Literal["translation", "setup"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class TextRegion:
    polygon: list[tuple[int, int]]
    text: str
    confidence: float
    translated_text: str = ""
    orientation: Literal["horizontal", "vertical"] = "horizontal"
    text_color: tuple[int, int, int] = (30, 30, 30)
    weight: Literal["normal", "bold"] = "normal"
    background_color: tuple[int, int, int] = (255, 255, 255)

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        xs = [point[0] for point in self.polygon]
        ys = [point[1] for point in self.polygon]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass(slots=True)
class JobRecord:
    id: str
    kind: JobKind
    filename: str
    status: JobStatus = "queued"
    progress: int = 0
    stage: str = "等待处理"
    message: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    input_path: Path | None = None
    output_path: Path | None = None
    detected_type: str | None = None
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["input_path"] = str(self.input_path) if self.input_path else None
        result["output_path"] = str(self.output_path) if self.output_path else None
        result["download_url"] = (
            f"/api/jobs/{self.id}/download" if self.status == "completed" and self.output_path else None
        )
        return result


class JobStore:
    def __init__(self) -> None:
        self._items: dict[str, JobRecord] = {}
        self._lock = RLock()

    def add(self, record: JobRecord) -> None:
        with self._lock:
            self._items[record.id] = record

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._items.get(job_id)

    def list(self) -> list[JobRecord]:
        with self._lock:
            return sorted(self._items.values(), key=lambda item: item.created_at, reverse=True)

    def update(self, job_id: str, **changes: Any) -> JobRecord:
        with self._lock:
            record = self._items[job_id]
            for key, value in changes.items():
                setattr(record, key, value)
            record.updated_at = utc_now()
            return record

    def append_warning(self, job_id: str, warning: str) -> None:
        with self._lock:
            record = self._items[job_id]
            record.warnings.append(warning)
            record.updated_at = utc_now()

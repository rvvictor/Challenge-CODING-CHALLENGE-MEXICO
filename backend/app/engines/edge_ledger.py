from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


class EdgeLedger:
    """Append-only JSONL audit stream for judge replay and decision inspection."""

    def __init__(self, path: str | Path = ".aurelion/edge-ledger.jsonl", memory_limit: int = 800):
        self.path = Path(path)
        self.memory_limit = memory_limit
        self.records: list[dict[str, Any]] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = {
            "id": f"EL-{now_ms()}-{uuid.uuid4().hex[:6]}",
            "time": now_ms(),
            "type": kind,
            "payload": payload,
        }
        self.records.insert(0, record)
        self.records = self.records[: self.memory_limit]
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True))
                handle.write("\n")
        except OSError:
            pass
        return record

    def latest(self, limit: int = 120) -> list[dict[str, Any]]:
        return self.records[:limit]

    def reset(self) -> None:
        self.records = []
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "records": len(self.records),
            "latest": self.latest(60),
        }

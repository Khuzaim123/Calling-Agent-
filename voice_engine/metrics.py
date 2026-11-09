"""Structured JSONL logging for per-turn metrics and session events."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class JsonlLogger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        rec = {"ts": time.time(), **record}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

"""Small file-backed aggregate counters for v6 UX verification.

Only event names and integer counts are stored. User IDs, prompts, message
content, and guild-specific personal data never enter this file.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Mapping


class MetricsStore:
    def __init__(self, path: str | os.PathLike[str] = "bot_metrics.json") -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> dict[str, int]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(raw, Mapping):
            return {}
        return {
            str(key): int(value)
            for key, value in raw.items()
            if isinstance(key, str) and isinstance(value, int) and value >= 0
        }

    def _write(self, counters: Mapping[str, int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(dict(counters), ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.path)

    def increment(self, event: str, amount: int = 1) -> None:
        if not event or amount < 0:
            raise ValueError("event must be non-empty and amount must be non-negative")
        with self._lock:
            counters = self._read()
            counters[event] = counters.get(event, 0) + amount
            self._write(counters)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return self._read()

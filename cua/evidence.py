"""Run directory + structured JSONL log. Every record passes through the redactor."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .policy import Redactor


class RunLog:
    def __init__(self, kind: str, root: str | Path = "runs", redactor: Redactor | None = None):
        self.run_id = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.dir = Path(root) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor()
        self._f = (self.dir / "log.jsonl").open("a")
        self._t0 = time.monotonic()
        self._shots = 0

    def event(self, kind: str, /, **data: Any) -> None:
        rec = {"t": round(time.monotonic() - self._t0, 3), "event": kind, **data}
        self._f.write(json.dumps(self.redactor.obj(rec), default=str) + "\n")
        self._f.flush()

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(self.redactor.obj(obj), indent=2, default=str))
        return p

    def write_text(self, name: str, text: str) -> Path:
        p = self.dir / name
        p.write_text(self.redactor.text(text))
        return p

    def capture(self, surface, label: str) -> list[str]:
        """Rich failure signal: masked screenshot + redacted text snapshot of all frames."""
        self._shots += 1
        stem = f"{self._shots:02d}-{label}"
        paths = []
        try:
            paths.append(surface.screenshot(self.dir / f"{stem}.png"))
        except Exception as e:
            self.event("capture_error", what="screenshot", error=str(e))
        try:
            text, _ = surface.observe()
            paths.append(str(self.write_text(f"{stem}.snapshot.txt", text)))
        except Exception as e:
            self.event("capture_error", what="snapshot", error=str(e))
        return paths

    def close(self) -> None:
        self._f.close()

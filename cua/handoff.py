"""Human-in-the-loop control transfer on the *same* live session.

Control model: a single-owner lease.
    AUTOMATION --escalate()--> AWAITING_HUMAN --operator attaches--> HUMAN
    HUMAN --operator `resume`--> RESYNC --checkpoint match--> AUTOMATION

* The automation never acts while it does not hold the lease (the replay/agent
  loops only call the surface between `escalate()` returning and the next escalation).
* The live session is exposed as a CDP endpoint. A human uses the headed browser
  window directly, or any operator console attaches to the endpoint (that is the
  seam for a real co-browsing UI; here the "console" is the CLI).
* Everything the human does is captured by an in-page hook while they hold control,
  redacted, and attached to the run evidence.
* Signalling is file-based (runs/<id>/control/) so the operator surface can be a
  separate process — the CLI here, a web console / ticket queue in production.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .evidence import RunLog
from .surface import WebSurface


class Owner(str, Enum):
    automation = "automation"
    awaiting_human = "awaiting_human"
    human = "human"


@dataclass
class Handoff:
    request_id: str
    reason: str
    context: dict[str, Any]
    started: float = field(default_factory=time.time)
    human_actions: list[dict] = field(default_factory=list)
    resumed_by: str | None = None
    note: str | None = None
    outcome: str = "pending"  # resumed | timed_out | aborted

    def summary(self) -> dict:
        return {
            "request_id": self.request_id, "reason": self.reason, "outcome": self.outcome,
            "resumed_by": self.resumed_by, "note": self.note,
            "seconds_in_human_control": round(time.time() - self.started, 1),
            "human_actions": self.human_actions,
        }


class ControlChannel:
    def __init__(self, surface: WebSurface, log: RunLog, timeout_s: int = 900):
        self.surface = surface
        self.log = log
        self.timeout_s = timeout_s
        self.owner = Owner.automation
        self.dir = log.dir / "control"
        self.dir.mkdir(exist_ok=True)
        self._write_state()

    def _write_state(self, **extra) -> None:
        (self.dir / "state.json").write_text(json.dumps({"owner": self.owner.value, **extra}, indent=2))

    def escalate(self, reason: str, context: dict[str, Any]) -> Handoff:
        """Pause automation, publish an intervention request, block until the operator
        hands control back (or timeout). Caller must re-sync state afterwards."""
        h = Handoff(request_id=f"ir-{uuid.uuid4().hex[:8]}", reason=reason, context=context)
        shot = self.log.capture(self.surface, f"intervention-{h.request_id}")
        request = {
            "request_id": h.request_id,
            "run_id": self.log.run_id,
            "reason": reason,
            **context,
            "evidence": shot,
            "live_session": {"cdp_endpoint": self.surface.cdp_endpoint, "url": self.surface.page.url},
            "how_to_resume": f"cua operator resume {self.log.run_id} --by <name> [--note ...]",
        }
        self.log.write_json(f"intervention-{h.request_id}.json", request)
        self.log.event("escalated", request_id=h.request_id, reason=reason, **context)

        self.owner = Owner.awaiting_human
        self._write_state(request_id=h.request_id)
        banner = ["", "=" * 72, f"  INTERVENTION REQUESTED  {h.request_id}",
                  f"  reason : {self.log.redactor.text(reason)}",
                  f"  live   : {self.surface.cdp_endpoint}  (or use the open browser window)",
                  f"  resume : cua operator resume {self.log.run_id} --by <name>", "=" * 72, ""]
        print("\n".join(banner), file=sys.stderr, flush=True)  # stdout carries the result

        def on_event(ev: dict) -> None:
            if self.owner == Owner.awaiting_human:  # first human input = human took control
                self.owner = Owner.human
                self._write_state(request_id=h.request_id)
                self.log.event("control_taken", request_id=h.request_id, by="human")
            if self.owner == Owner.human:
                ev = self.log.redactor.obj(ev)
                h.human_actions.append(ev)
                self.log.event("human_action", request_id=h.request_id, **ev)

        self.surface.on_human_event(on_event)
        resume_file = self.dir / "resume.json"
        resume_file.unlink(missing_ok=True)
        deadline = time.time() + self.timeout_s
        try:
            while time.time() < deadline:
                self.surface.idle(300)  # keeps the event loop pumping human-event bindings
                if resume_file.exists():
                    sig = json.loads(resume_file.read_text() or "{}")
                    resume_file.unlink()
                    if sig.get("abort"):
                        h.outcome = "aborted"
                    else:
                        h.outcome = "resumed"
                    h.resumed_by, h.note = sig.get("by"), sig.get("note")
                    break
            else:
                h.outcome = "timed_out"
        finally:
            self.surface.on_human_event(None)
            self.owner = Owner.automation
            self._write_state()
        self.log.event("control_returned", **h.summary())
        return h


def signal_resume(run_dir: Path, by: str, note: str | None = None, abort: bool = False) -> Path:
    p = run_dir / "control" / "resume.json"
    if not p.parent.exists():
        raise FileNotFoundError(f"no control channel for {run_dir}")
    p.write_text(json.dumps({"by": by, "note": note, "abort": abort, "at": time.time()}))
    return p

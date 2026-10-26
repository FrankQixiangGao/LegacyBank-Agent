"""Discovery: an LLM drives the live surface toward a goal; the Recorder turns what it
did into a Capability.

The model only ever sees an observation (text rendering of all frames with ref ids)
and can only act through the tools below. Every tool call goes through the policy
before it touches the UI. Credentials are never shown to the model: it types the
placeholder {{secret:name}} and the executor substitutes the value.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import anthropic

from .evidence import RunLog
from .handoff import ControlChannel
from .policy import Policy
from .recorder import Recorder
from .schema import AppProfile, BusinessOutcomeSpec, Capability, ScreenKind, Target, ValueRef
from .surface import WebSurface

MODEL = "claude-opus-5-5"

SYSTEM = """You operate a legacy back-office banking application on behalf of a bank's AI agent.
Your run is being recorded and turned into a deterministic, replayable automation, so
act like a careful human operator demonstrating the flow once.

Each observation lists every frame of the screen. Elements you can act on or read have
ids like [e3] or [f2e5]; use those ids in tool calls. Rows of tables are shown as
`row: cell | cell | ...`.

Rules:
- Take the most direct path to the goal. One action per tool call.
- Values that come from the goal (member numbers, names, amounts...) are inputs of the
  capability: pass them with `param_name` (snake_case) and a description, so replay can
  be invoked with different values. Never hard-code them.
- Credentials: type exactly the placeholder given (e.g. {{secret:operator_id}}). You
  never see real credentials.
- For screen-changing clicks, give `expect_text`: a short, stable piece of text that
  proves the next screen is the right one (a screen title, not data that varies).
- Use `extract` for every value the goal asks you to read, on the element that holds
  just that value.
- Do not take irreversible actions (posting, transfers, opening/closing accounts) unless
  the goal explicitly requires it; such actions require human approval anyway.
- If you are blocked (unexpected error, missing access, ambiguous situation), call
  `request_human` with a clear reason rather than guessing.
- When the goal is achieved and values are extracted, call `finish`."""


def _tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": required,
                             "additionalProperties": False}}


_INTENT = {"type": "string", "description": "Why this step, in operator terms (shown to reviewers)."}
_SENS = {"type": "string", "enum": ["public", "internal", "pii", "financial"]}

TOOLS = [
    _tool("click", "Click a button, link or control.",
          {"ref": {"type": "string"}, "intent": _INTENT,
           "expect_text": {"type": "string", "description": "Stable text that should appear after the click."}},
          ["ref", "intent"]),
    _tool("fill", "Type a value into a text field (replaces existing content).",
          {"ref": {"type": "string"}, "intent": _INTENT, "value": {"type": "string"},
           "param_name": {"type": "string", "description": "Set when the value comes from the goal."},
           "param_description": {"type": "string"}, "param_sensitivity": _SENS},
          ["ref", "intent", "value"]),
    _tool("select", "Choose an option (by its visible text) in a select box.",
          {"ref": {"type": "string"}, "intent": _INTENT, "option": {"type": "string"},
           "param_name": {"type": "string"}, "param_description": {"type": "string"}, "param_sensitivity": _SENS},
          ["ref", "intent", "option"]),
    _tool("extract", "Read a value the goal asks for. Becomes a typed output of the capability.",
          {"ref": {"type": "string"}, "name": {"type": "string", "description": "snake_case output name"},
           "type": {"type": "string", "enum": ["string", "integer", "decimal", "date"]},
           "description": {"type": "string"}, "sensitivity": _SENS},
          ["ref", "name", "type", "description", "sensitivity"]),
    _tool("request_human", "Escalate to a human operator who will take over this live session.",
          {"reason": {"type": "string"}}, ["reason"]),
    _tool("finish", "End the run.",
          {"status": {"type": "string", "enum": ["success", "failed"]},
           "summary": {"type": "string"},
           "capability_name": {"type": "string", "description": "snake_case verb_object, e.g. lookup_savings_balance"},
           "capability_summary": {"type": "string", "description": "One sentence: what it does, inputs, outputs."}},
          ["status", "summary"]),
]


@dataclass
class DiscoveryResult:
    status: str  # success | failed | stuck | max_steps | timeout
    run_id: str
    summary: str
    capability: Capability | None = None
    artifact_path: str | None = None


class DiscoveryAgent:
    def __init__(self, surface: WebSurface, policy: Policy, profile: AppProfile, log: RunLog, *,
                 base_url: str, vault: dict[str, str], control: ControlChannel | None = None,
                 max_steps: int = 25, timeout_s: int = 600, client: Any = None, model: str = MODEL):
        self.s, self.policy, self.profile, self.log = surface, policy, profile, log
        self.base_url = base_url.rstrip("/")
        self.vault = vault
        self.control = control
        self.max_steps, self.timeout_s = max_steps, timeout_s
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.rec = Recorder(surface, base_url)
        self.refs: dict[str, tuple[str | None, str]] = {}
        self.extracted: dict[str, Any] = {}
        for name, value in vault.items():
            log.redactor.register(value, f"secret:{name}")

    # ------------------------------------------------------------ loop

    def run(self, goal: str, entry_url: str) -> DiscoveryResult:
        self.goal = goal
        # The goal holds raw inputs (member numbers...) we can't classify until the model
        # declares them as params, so only its fingerprint is logged now; the
        # parameterized goal is logged at the end and stored in the artifact.
        self.log.event("discovery_start", goal_sha1=hashlib.sha1(goal.encode()).hexdigest()[:12],
                       entry=entry_url, model=self.model)
        d = self.policy.check("goto", url=entry_url)
        if not d.allowed:
            return DiscoveryResult("failed", self.log.run_id, f"entry URL denied: {d.reason}")
        self.s.goto(entry_url)
        self.rec.record_goto(entry_url, "Open the application sign-on page")

        secrets = ", ".join("{{secret:%s}}" % n for n in self.vault) or "(none)"
        messages: list[dict] = [{"role": "user", "content":
                                 f"Goal: {goal}\n\nAvailable credential placeholders: {secrets}\n\n"
                                 f"Current screen:\n{self._observe()}"}]
        t_end = time.monotonic() + self.timeout_s
        seen: list[str] = []
        for turn in range(1, self.max_steps + 1):
            if time.monotonic() > t_end:
                return self._stop("timeout", "discovery timed out")
            resp = self._call(messages)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                return self._stop("failed", "model refused")
            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    self.log.event("agent_note", turn=turn, text=b.text.strip()[:600])
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                messages.append({"role": "user", "content": "Continue: act with a tool, or call finish."})
                continue
            results = []
            done: DiscoveryResult | None = None
            for call in calls:
                if done is not None:
                    results.append({"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                                    "content": "run already finished"})
                    continue
                out, is_error, done = self._dispatch(call.name, dict(call.input), turn)
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": out, "is_error": is_error})
            if done is not None:
                return done
            obs = self._observe()
            # dead-end detection: same screen three actions in a row
            seen.append(hashlib.sha1(re.sub(r"\[[f\d]*e\d+\]", "", obs).encode()).hexdigest())
            if len(seen) >= 3 and len(set(seen[-3:])) == 1 and not self.extracted:
                if self.control:
                    self._escalate("no progress: the screen did not change across 3 actions")
                    obs = self._observe()
                    seen.clear()
                else:
                    return self._stop("stuck", "no progress across 3 actions")
            results.append({"type": "text", "text": f"Current screen:\n{obs}"})
            messages.append({"role": "user", "content": results})
        return self._stop("max_steps", f"stopped after {self.max_steps} turns")

    _fallbacks = True

    def _call(self, messages: list[dict]):
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if self._fallbacks else {}
        try:
            return self.client.beta.messages.create(
                model=self.model, max_tokens=16000, system=SYSTEM, tools=TOOLS, messages=messages,
                output_config={"effort": "medium"}, **extra,
            )
        except anthropic.BadRequestError as e:
            if self._fallbacks and "fallback" in str(e).lower():  # account without the beta
                self._fallbacks = False
                return self._call(messages)
            raise

    def _observe(self) -> str:
        text, self.refs = self.s.observe()
        return text

    def _stop(self, status: str, summary: str) -> DiscoveryResult:
        self.log.capture(self.s, f"stop-{status}")
        self.log.event("discovery_end", status=status, summary=summary)
        return DiscoveryResult(status, self.log.run_id, summary)

    def _escalate(self, reason: str) -> None:
        h = self.control.escalate(reason, {"goal": self.goal, "recorded_steps": len(self.rec.steps)})
        self.rec.notes.append(f"human intervened after {self.rec.steps[-1].id}: {reason} "
                              f"({len(h.human_actions)} human actions, see run evidence); review before approval")

    # ------------------------------------------------------------ tools

    def _target(self, ref: str, desc: str, for_extract: bool = False) -> tuple[Target, Any, dict]:
        if ref not in self.refs:
            raise KeyError(f"unknown ref {ref}; use an id from the current screen")
        frame, r = self.refs[ref]
        loc = self.s.by_ref(frame, r)
        info = self.s.describe(loc)
        target = self.rec.build_target(frame, r, desc, for_extract=for_extract)
        return target, loc, info

    def _dispatch(self, name: str, a: dict, turn: int) -> tuple[str, bool, DiscoveryResult | None]:
        if a.get("param_name") and a.get("param_sensitivity") in ("pii", "financial"):
            self.log.redactor.register(a.get("value") or a.get("option"), a["param_name"])
        self.log.event("tool_call", turn=turn, tool=name, input=a)
        try:
            if name == "finish":
                return "ok", False, self._finish(a)
            if name == "request_human":
                if not self.control:
                    return "no human operator available in this mode; finish with status failed", True, None
                self._escalate(a["reason"])
                return "human operator handed control back; re-check the screen", False, None
            if name not in ("click", "fill", "select", "extract"):
                return f"unknown tool {name}", True, None

            ref = a["ref"]
            frame = self.refs.get(ref, (None, ""))[0]
            pre = self.s.describe(self.s.by_ref(frame, ref)) if ref in self.refs else {}
            decision = self.policy.check(name, element=pre, base_url=pre.get("baseUrl"))
            if not decision.allowed or decision.needs_approval:
                self.log.event("policy_denied", tool=name, reason=decision.reason or "approval required")
                return f"DENIED by policy: {decision.reason}. Choose another way or request_human.", True, None

            desc = (a.get("intent") or a.get("description") or name).strip()
            target, loc, info = self._target(ref, desc, for_extract=(name == "extract"))
            if name == "click":
                step = self.rec.record_action("click", target, a["intent"], risk=decision.risk)
                self.s.click(loc)
                self.rec.postcondition(step, a.get("expect_text"), frame)
                out = "clicked"
            elif name in ("fill", "select"):
                raw = a["value"] if name == "fill" else a["option"]
                m = re.fullmatch(r"\{\{secret:(\w+)\}\}", raw.strip())
                if m:
                    if m.group(1) not in self.vault:
                        return f"no secret named {m.group(1)}", True, None
                    vref, actual = self.rec.add_secret(m.group(1)), self.vault[m.group(1)]
                elif a.get("param_name"):
                    sens = a.get("param_sensitivity", "internal")
                    vref = self.rec.add_param(a["param_name"], raw, a.get("param_description", ""), sens)
                    if sens in ("pii", "financial"):
                        self.log.redactor.register(raw, a["param_name"])
                    actual = raw
                else:
                    vref, actual = ValueRef(literal=raw), raw
                    if raw and raw.lower() in self.goal.lower():
                        self.rec.notes.append(f"literal {raw!r} also appears in the goal; consider parameterizing")
                self.rec.record_action(name, target, a["intent"], value=vref)
                (self.s.fill if name == "fill" else self.s.select)(loc, actual)
                out = "ok"
            else:  # extract
                raw = self.s.read(loc)
                if a["sensitivity"] in ("pii", "financial"):
                    self.log.redactor.register(raw, a["name"])
                self.rec.add_output(a["name"], a["type"], a["description"], a["sensitivity"])
                self.rec.record_action("extract", target, a["description"], output=a["name"])
                self.extracted[a["name"]] = raw
                out = f"read {a['name']} = {raw!r}"
            self.log.event("action_ok", turn=turn, tool=name, step=self.rec.steps[-1].id,
                           locators=[l.strategy for l in target.locators])
            return out, False, None
        except Exception as e:  # report to the model; it can choose another way
            self.log.event("tool_error", tool=name, error=f"{type(e).__name__}: {e}")
            return f"error: {type(e).__name__}: {e}", True, None

    def _finish(self, a: dict) -> DiscoveryResult:
        if a["status"] != "success":
            return self._stop("failed", a.get("summary", ""))
        if not self.extracted and not any(st.expect for st in self.rec.steps):
            return self._stop("failed", "finish(success) without any verified state")
        name = re.sub(r"[^a-z0-9_]", "_", (a.get("capability_name") or "capability").lower())
        outcomes = [BusinessOutcomeSpec(code=sc.id, description=sc.description, screen=sc.id)
                    for sc in self.profile.screens if sc.kind == ScreenKind.business_outcome]
        cap = self.rec.build(
            cap_id=f"{self.profile.product}.{name}", summary=a.get("capability_summary") or a["summary"],
            product=self.profile.product, profile=self.profile.id, run_id=self.log.run_id,
            model=self.model, goal=self.goal, business_outcomes=outcomes,
        )
        if any("human intervened" in n for n in cap.provenance.notes):
            cap.provenance.notes.append("flow contains human-performed steps not captured as artifact steps")
        self.log.event("discovery_end", status="success", summary=a["summary"], steps=len(cap.steps),
                       goal=cap.provenance.goal)
        return DiscoveryResult("success", self.log.run_id, a["summary"], capability=cap)

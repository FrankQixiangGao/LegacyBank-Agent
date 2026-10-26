"""Deterministic replay: Capability + params -> ReplayResult. No LLM anywhere here.

Per step:
  1. classify the current screen against the AppProfile (interrupts first)
  2. resolve the target: candidates in order, exactly-one-match only, polled until timeout;
     every poll re-classifies the screen, so a "not found" page or a notice is noticed
     immediately instead of after a locator timeout
  3. policy check, act
  4. verify the step's postcondition (same polling + classification)
Then verify the capability's success checkpoint and return typed outputs.

Screen kinds map onto the result contract:
  business_outcome -> BusinessOutcome (a normal return, not an error)
  recoverable      -> run the profile's handler, retry (bounded), record it in the trace
  needs_human      -> escalate on the live session if a ControlChannel is attached, else Failure
  failure          -> Failure with evidence
"""

from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from .evidence import RunLog
from .handoff import ControlChannel
from .policy import Policy
from .schema import (
    AppProfile, BusinessOutcome, Capability, Checkpoint, Failure, ReplayResult, Risk, Screen,
    ScreenKind, Sensitivity, Step, StepTrace, Success,
)
from .surface import WebSurface, substitute

POLL_MS = 250
TARGET_TIMEOUT_MS = 10_000
MAX_HANDOFFS = 2


class StepError(Exception):
    def __init__(self, result: BusinessOutcome | Failure):
        self.result = result


def parse_value(raw: str, type_: str) -> Any:
    s = raw.strip()
    if type_ == "decimal":
        neg = s.startswith("-") or s.endswith("-") or (s.startswith("(") and s.endswith(")"))
        num = re.sub(r"[^\d.]", "", s)
        try:
            d = Decimal(num)
        except InvalidOperation:
            raise ValueError(f"not a decimal: {raw!r}")
        return str(-d if neg else d)
    if type_ == "integer":
        return int(re.sub(r"[^\d-]", "", s))
    return s


class ReplayEngine:
    def __init__(self, cap: Capability, profile: AppProfile, surface: WebSurface, policy: Policy, log: RunLog,
                 *, base_url: str, params: dict[str, str], vault: dict[str, str],
                 control: ControlChannel | None = None, approve_irreversible: bool = False):
        self.cap, self.profile, self.s, self.policy, self.log = cap, profile, surface, policy, log
        self.base_url = base_url.rstrip("/")
        self.params = params
        self.vault = vault
        self.control = control
        self.approve_irreversible = approve_irreversible
        self.values = {**params, "base_url": self.base_url}
        self.trace: list[StepTrace] = []
        self.outputs: dict[str, Any] = {}
        self.handoffs: list[dict] = []
        self.drift: list[str] = []

    # ------------------------------------------------------------------ entry

    def run(self) -> ReplayResult:
        pre = self._preflight()  # also registers sensitive params with the redactor
        self.log.event("replay_start", capability=self.cap.id, version=self.cap.version, params=self.params)
        result = pre or self._execute()
        rr = ReplayResult(capability=self.cap.id, version=self.cap.version, run_id=self.log.run_id,
                          result=result, trace=self.trace, drift=self.drift, handoffs=self.handoffs)
        self.log.event("replay_end", status=result.status, result=result.model_dump())
        return rr

    def _preflight(self) -> Failure | None:
        for spec in self.cap.inputs:
            v = self.params.get(spec.name)
            if v is None:
                return Failure(code="invalid_input", step=None, expected=f"param '{spec.name}'", observed="missing")
            if spec.pattern and not re.fullmatch(spec.pattern, v):
                return Failure(code="invalid_input", step=None,
                               expected=f"'{spec.name}' matching {spec.pattern}", observed=v)
            if spec.sensitivity in (Sensitivity.pii, Sensitivity.financial, Sensitivity.secret):
                self.log.redactor.register(v, spec.name)
        for sec in self.cap.secrets:
            if sec.name not in self.vault:
                return Failure(code="internal", step=None, expected=f"secret '{sec.name}' in vault", observed="missing")
            self.log.redactor.register(self.vault[sec.name], f"secret:{sec.name}")
        if self.cap.max_risk == Risk.irreversible and not (self.cap.status == "approved" and self.approve_irreversible):
            return Failure(code="policy_denied", step=None,
                           expected="approved capability + explicit approval for irreversible steps",
                           observed=f"status={self.cap.status}, approve_irreversible={self.approve_irreversible}")
        return None

    def _execute(self) -> Success | BusinessOutcome | Failure:
        i = 0
        while i < len(self.cap.steps):
            step = self.cap.steps[i]
            t0 = time.monotonic()
            tr = StepTrace(step=step.id, status="ok")
            try:
                self._run_step(step, tr)
                tr.ms = int((time.monotonic() - t0) * 1000)
                self.trace.append(tr)
                i += 1
            except StepError as e:
                tr.ms = int((time.monotonic() - t0) * 1000)
                tr.status = "failed"
                self.trace.append(tr)
                res = e.result
                if isinstance(res, BusinessOutcome) or not self._escalatable(res):
                    return res
                resume_at = self._handoff(res, step)
                if resume_at is None:
                    return res
                i = resume_at
        try:
            self._wait_checkpoint(self.cap.success, "success")
        except StepError as e:
            return e.result
        return Success(outputs=self.outputs)

    # ------------------------------------------------------------------ one step

    def _run_step(self, step: Step, tr: StepTrace) -> None:
        self.log.event("step_start", step=step.id, action=step.action, intent=step.intent)
        self._handle_screen(step, tr)
        if step.action == "goto":
            url = substitute(step.url or "", self.values)
            d = self.policy.check("goto", url=url)
            if not d.allowed:
                raise StepError(Failure(code="policy_denied", step=step.id, expected=url, observed=d.reason))
            self.s.goto(url)
        else:
            loc = self._resolve(step, tr)
            info = self.s.describe(loc)
            d = self.policy.check(step.action, element=info, base_url=info.get("baseUrl"))
            irreversible = step.risk == Risk.irreversible or d.risk == Risk.irreversible
            if not d.allowed or (irreversible and not self.approve_irreversible):
                raise StepError(Failure(code="policy_denied", step=step.id, expected=step.intent,
                                        observed=d.reason or "irreversible step without approval"))
            if step.action == "click":
                self.s.click(loc)
            elif step.action in ("fill", "select"):
                v = self._value(step)
                (self.s.fill if step.action == "fill" else self.s.select)(loc, v)
            elif step.action == "extract":
                raw = self.s.read(loc)
                spec = next(o for o in self.cap.outputs if o.name == step.output)
                try:
                    val = parse_value(raw, spec.type)
                except ValueError as e:
                    raise StepError(self._fail("checkpoint_failed", step, f"{spec.name} as {spec.type}", str(e)))
                if spec.sensitivity in (Sensitivity.pii, Sensitivity.financial):
                    self.log.redactor.register(raw, spec.name)
                    self.log.redactor.register(str(val), spec.name)
                self.outputs[spec.name] = val
        if step.expect:
            self._wait_checkpoint(step.expect, step.id, tr)
        self.log.event("step_ok", step=step.id, locator=tr.locator_used, recoveries=tr.recoveries)

    def _value(self, step: Step) -> str:
        v = step.value
        if v is None:
            raise StepError(Failure(code="internal", step=step.id, expected="value", observed="none"))
        if v.param is not None:
            return self.params[v.param]
        if v.secret is not None:
            return self.vault[v.secret]
        return v.literal or ""

    def _resolve(self, step: Step, tr: StepTrace):
        assert step.target
        deadline = time.monotonic() + TARGET_TIMEOUT_MS / 1000
        last = None
        while True:
            self._handle_screen(step, tr)
            last = self.s.try_resolve(step.target, self.values)
            if last.locator is not None:
                tr.locator_used = f"{last.strategy}#{last.index}"
                if last.index > 0:
                    self.drift.append(f"{step.id}: primary locator failed, resolved via {last.strategy}")
                    self.log.event("drift", step=step.id, used=last.strategy, index=last.index)
                return last.locator
            if time.monotonic() > deadline:
                break
            self.s.idle(POLL_MS)
        code = "target_ambiguous" if last and last.ambiguous else "target_not_found"
        raise StepError(self._fail(code, step, f"{step.target.description} via "
                                   f"{[l.strategy for l in step.target.locators]}",
                                   f"ambiguous: {last.ambiguous}" if last and last.ambiguous else "no candidate matched"))

    def _wait_checkpoint(self, cp: Checkpoint, label: str, tr: StepTrace | None = None) -> None:
        deadline = time.monotonic() + cp.timeout_ms / 1000
        tr = tr or StepTrace(step=label, status="ok")
        while True:
            self._handle_screen(None, tr, label=label)
            texts = self.s.visible_text()
            failing = [c for c in cp.all_of if not self.s.check(c, self.values, texts)]
            if not failing:
                return
            if time.monotonic() > deadline:
                observed = "; ".join(f"{c.kind}:{substitute(c.value or (c.target.description if c.target else ''), self.values)}"
                                     for c in failing)
                raise StepError(Failure(code="checkpoint_failed", step=label, expected=cp.description,
                                        observed=f"not satisfied: {observed}",
                                        evidence=self.log.capture(self.s, f"checkpoint-{label}")))
            self.s.idle(POLL_MS)

    # ------------------------------------------------------------------ screens

    def classify(self, texts: dict | None = None) -> Screen | None:
        texts = texts if texts is not None else self.s.visible_text()
        for sc in self.profile.screens:
            if all(self.s.check(c, self.values, texts) for c in sc.match):
                return sc
        return None

    def _handle_screen(self, step: Step | None, tr: StepTrace, label: str | None = None) -> None:
        """Deal with whatever known exceptional screen is showing. Returns when the
        screen is clear (possibly after recovery); raises for anything else."""
        attempts: dict[str, int] = {}
        sid = step.id if step else label
        while (sc := self.classify()) is not None:
            self.log.event("screen", step=sid, screen=sc.id, kind=sc.kind.value)
            if sc.kind == ScreenKind.business_outcome:
                raise StepError(BusinessOutcome(code=sc.id, description=sc.description, step=sid))
            if sc.kind == ScreenKind.recoverable and sc.handler:
                n = attempts[sc.id] = attempts.get(sc.id, 0) + 1
                if n > sc.handler.max_attempts:
                    raise StepError(self._fail("unrecoverable_screen", step, f"'{sc.id}' to clear",
                                               f"still present after {n - 1} attempts", sid=sid, screen=sc.id))
                if sc.handler.kind == "wait":
                    self.s.idle(sc.handler.wait_ms)
                elif sc.handler.kind == "click" and sc.handler.target:
                    r = self.s.try_resolve(sc.handler.target, self.values)
                    if r.locator is None:
                        raise StepError(self._fail("unrecoverable_screen", step, sc.handler.target.description,
                                                   "handler target not found", sid=sid, screen=sc.id))
                    self.s.click(r.locator)
                tr.status = "recovered"
                tr.recoveries.append(sc.id)
                self.log.event("recovered", step=sid, screen=sc.id, attempt=n)
                continue
            code = "needs_human" if sc.kind == ScreenKind.needs_human else "unrecoverable_screen"
            raise StepError(self._fail(code, step, "no blocking screen", sc.description, sid=sid, screen=sc.id))

    def _fail(self, code: str, step: Step | None, expected: str, observed: str, *, sid: str | None = None,
              screen: str | None = None) -> Failure:
        sid = sid or (step.id if step else None)
        return Failure(code=code, step=sid, expected=expected, observed=observed, screen=screen,
                       evidence=self.log.capture(self.s, f"{code}-{sid}"))

    # ------------------------------------------------------------------ escalation

    def _escalatable(self, f: Failure) -> bool:
        return (self.control is not None and len(self.handoffs) < MAX_HANDOFFS
                and f.code not in ("policy_denied", "invalid_input", "internal"))

    def _handoff(self, f: Failure, step: Step) -> int | None:
        assert self.control
        h = self.control.escalate(
            f"{f.code}: {f.observed}",
            {"capability": f"{self.cap.id}@v{self.cap.version}", "step": step.id, "step_intent": step.intent,
             "expected": f.expected, "screen": f.screen},
        )
        self.handoffs.append(h.summary())
        f.escalation = h.request_id
        if h.outcome != "resumed":
            return None
        return self._resync()

    def _resync(self) -> int | None:
        """The human may have done anything. Don't assume — observe, then find the
        furthest step whose postcondition holds right now and continue after it."""
        sc = self.classify()
        if sc is not None and sc.kind != ScreenKind.recoverable:
            self.log.event("resync_blocked", screen=sc.id)
            return None
        texts = self.s.visible_text()
        for idx in range(len(self.cap.steps) - 1, -1, -1):
            st = self.cap.steps[idx]
            if st.expect and all(self.s.check(c, self.values, texts) for c in st.expect.all_of):
                self.log.event("resync", resume_after=st.id)
                self.trace.append(StepTrace(step=f"resync->{st.id}", status="human"))
                return idx + 1
        if self.cap.max_risk == Risk.read_only:
            self.log.event("resync", resume_after=None, note="no checkpoint matched; restarting read-only flow")
            return 0
        self.log.event("resync_failed")
        return None

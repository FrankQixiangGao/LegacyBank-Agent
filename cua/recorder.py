"""Turns the actions of a live (LLM- or human-driven) run into a Capability.

The recorder works from the *element*, not from the model's transcript: before each
action it describes the element in-page and derives locator candidates, then
verifies every candidate resolves to exactly that one element. Unverified
candidates are dropped, so replay never relies on a guess made at record time.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .schema import (
    AppRef, Capability, Checkpoint, Condition, Locator, OutputSpec, ParamSpec, Provenance,
    Risk, SecretSpec, Sensitivity, Step, Target, ValueRef,
)
from .policy import Redactor
from .surface import WebSurface

_NUMERIC = re.compile(r"^[-$\d.,/ ]+$")


class Recorder:
    def __init__(self, surface: WebSurface, base_url: str):
        self.s = surface
        self.base_url = base_url.rstrip("/")
        self.steps: list[Step] = []
        self.params: dict[str, tuple[ParamSpec, str]] = {}  # name -> (spec, recorded value)
        self.secrets: dict[str, SecretSpec] = {}
        self.outputs: dict[str, OutputSpec] = {}
        self.notes: list[str] = []

    # ------------------------------------------------------------- helpers

    @property
    def values(self) -> dict[str, str]:
        return {n: v for n, (_, v) in self.params.items()}

    def _templatize(self, s: str) -> str:
        """Replace recorded param values with {param} placeholders (canonicalization:
        `MBR# = 12345` becomes `MBR# = {member_id}`)."""
        for name, (_, value) in sorted(self.params.items(), key=lambda kv: -len(kv[1][1])):
            if value and value in s:
                s = s.replace(value, "{" + name + "}")
        return s

    @staticmethod
    def redactor_probe(s: str) -> str:
        return Redactor().text(s)

    def _next_id(self) -> str:
        return f"s{len(self.steps) + 1}"

    # ------------------------------------------------------------- targets

    def build_target(self, frame: str | None, ref: str, description: str, for_extract: bool = False) -> Target:
        el = self.s.by_ref(frame, ref)
        info = self.s.describe(el)
        cands: list[Locator] = []
        tag = info["tag"]
        if info.get("role") and info.get("name"):
            cands.append(Locator(strategy="role", args={"role": info["role"], "name": info["name"]}))
        if info.get("labelCell") and tag in ("input", "select", "textarea"):
            cands.append(Locator(strategy="label_cell", args={"label": info["labelCell"]}))
        row = info.get("row")
        if row and row.get("headers"):
            headers, cells, idx = row["headers"], row["cells"], row["index"]
            # A row that contains an input value is that record's row: its other cells are
            # the record's data (name, branch...). Keying on them would persist PII and
            # would not generalize, so only the parameterized key is allowed there.
            record_row = any(val and val in c for _, val in self.params.values() for c in cells)
            keyed = []
            for j, (h, v) in enumerate(zip(headers, cells)):
                if j == idx or not h or not v:
                    continue
                if record_row and self._templatize(v) == v:
                    continue
                if self.redactor_probe(v) != v:  # amounts, SSNs, account numbers are never keys
                    continue
                args: dict[str, Any] = {"key_column": h, "key": v}
                if headers[idx]:
                    args["column"] = headers[idx]
                else:
                    args["index"] = idx
                if tag != "td":
                    args["pick"] = tag
                # rank: keys named in the step's intent, then descriptive over numeric codes
                keyed.append(((v.lower() not in description.lower(), bool(_NUMERIC.match(v))), args))
            keyed.sort(key=lambda kv: kv[0])
            cands += [Locator(strategy="row_cell", args=a) for _, a in keyed]
        if not for_extract and info.get("text") and tag in ("a", "button", "input"):
            cands.append(Locator(strategy="text", args={"text": info["text"]}))
        if info.get("nameAttr"):
            cands.append(Locator(strategy="css", args={"css": f'{tag}[name="{info["nameAttr"]}"]'}))
        cands.append(Locator(strategy="css", args={"css": info["cssPath"]}))

        verified, dropped = [], []
        for c in cands:
            c = Locator(strategy=c.strategy, args={k: self._templatize(v) if isinstance(v, str) else v
                                                   for k, v in c.args.items()})
            try:
                pl = self.s.candidate_locator(frame, c, self.values)
                ok = pl is not None and pl.count() == 1 and self.s.same_element(el, pl)
            except Exception:
                ok = False
            (verified if ok else dropped).append(c)
        # A parameterized candidate encodes *which* record (row keyed by {member_id});
        # it outranks a generic one that only happens to be unique in this recording.
        verified.sort(key=lambda c: 0 if any("{" in str(v) for v in c.args.values()) else 1)
        if dropped:
            self.notes.append(f"{description}: dropped non-unique/unverified locators "
                              f"{[d.strategy for d in dropped]}")
        if not verified:
            raise LookupError(f"no verifiable locator for {description}")
        return Target(description=description, frame=frame, locators=verified)

    # ------------------------------------------------------------- steps

    def record_goto(self, url: str, intent: str) -> Step:
        templ = url.replace(self.base_url, "{base_url}", 1)
        step = Step(id=self._next_id(), intent=intent, action="goto", url=self._templatize(templ))
        self.steps.append(step)
        return step

    def record_action(self, action: str, target: Target, intent: str, *, value: ValueRef | None = None,
                      output: str | None = None, risk: Risk = Risk.read_only) -> Step:
        step = Step(id=self._next_id(), intent=intent, action=action, target=target, value=value,
                    output=output, risk=risk)
        self.steps.append(step)
        return step

    def add_param(self, name: str, value: str, description: str, sensitivity: str, type_: str = "string") -> ValueRef:
        pattern = r"^\d+$" if value.isdigit() else None
        spec = ParamSpec(name=name, type=type_, description=description,
                         sensitivity=Sensitivity(sensitivity), pattern=pattern)
        self.params[name] = (spec, value)
        return ValueRef(param=name)

    def add_secret(self, name: str) -> ValueRef:
        self.secrets.setdefault(name, SecretSpec(name=name, description=f"credential '{name}' from tenant vault"))
        return ValueRef(secret=name)

    def add_output(self, name: str, type_: str, description: str, sensitivity: str) -> None:
        self.outputs[name] = OutputSpec(name=name, type=type_, description=description,
                                        sensitivity=Sensitivity(sensitivity))

    def postcondition(self, step: Step, expect_text: str | None, frame_hint: str | None) -> None:
        """After a screen-changing action: assert we landed where we meant to.
        Uses the agent's expected text (verified now) and any param value visible on
        the new screen (so the checkpoint pins *which* record, not just which page)."""
        texts = self.s.visible_text()
        conds: list[Condition] = []
        if expect_text:
            frame = next((f for f, t in texts.items() if expect_text in t), "__missing__")
            if frame == "__missing__":
                self.notes.append(f"{step.id}: expected text {expect_text!r} not visible after action; not asserted")
            else:
                conds.append(Condition(kind="text_visible", value=self._templatize(expect_text), frame=frame))
        for name, (spec, value) in self.params.items():
            target_frame = frame_hint if frame_hint in texts else None
            if value and value in texts.get(target_frame, "") and spec.sensitivity != Sensitivity.secret:
                conds.append(Condition(kind="text_visible", value="{" + name + "}", frame=target_frame))
        if conds:
            step.expect = Checkpoint(description=f"after {step.id}: {step.intent}", all_of=conds)

    # ------------------------------------------------------------- build

    def build(self, *, cap_id: str, summary: str, product: str, profile: str, run_id: str, model: str,
              goal: str, business_outcomes: list) -> Capability:
        conds: list[Condition] = []
        for st in self.steps:
            if st.action == "extract" and st.target:
                conds.append(Condition(kind="target_visible", target=st.target))
        last_nav = next((st for st in reversed(self.steps) if st.expect), None)
        if last_nav:
            conds = list(last_nav.expect.all_of) + conds
        if not conds:
            raise ValueError("cannot build a success checkpoint: no postconditions or extracts recorded")
        risk = max((st.risk for st in self.steps), key=list(Risk).index, default=Risk.read_only)
        return Capability(
            id=cap_id, summary=summary,
            app=AppRef(product=product, profile=profile),
            max_risk=risk,
            inputs=[spec for spec, _ in self.params.values()],
            secrets=list(self.secrets.values()),
            outputs=list(self.outputs.values()),
            business_outcomes=business_outcomes,
            steps=self.steps,
            success=Checkpoint(description="capability goal reached and all outputs readable", all_of=conds),
            provenance=Provenance(recorded_from_run=run_id, recorded_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                  model=model, goal=self._templatize(goal), notes=self.notes),
        )

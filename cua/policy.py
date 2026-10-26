"""Guardrails, enforced in the executor (never just in the prompt).

Three independent layers:
  1. Action check   — every agent/replay action is checked before it runs
                      (action type, destination origin/path, irreversible-looking target).
  2. Network guard  — a Playwright route on the whole context aborts any document
                      navigation outside the allowlist, whoever triggered it.
  3. Redaction      — every log/evidence write goes through `Redactor`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import yaml
from pydantic import BaseModel

from .schema import Risk


class PolicyConfig(BaseModel):
    allowed_origins: list[str]
    denied_paths: list[str] = []
    allowed_actions: list[str] = ["goto", "click", "fill", "select", "extract"]
    irreversible_patterns: list[str] = []
    # block: never; require_approval: only with an explicit human approval
    irreversible_mode: str = "require_approval"


@dataclass
class Decision:
    allowed: bool
    risk: Risk = Risk.read_only
    reason: str = ""
    needs_approval: bool = False


class Policy:
    def __init__(self, cfg: PolicyConfig):
        self.cfg = cfg
        self._denied = [re.compile(p) for p in cfg.denied_paths]
        self._irrev = [re.compile(p) for p in cfg.irreversible_patterns]

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls(PolicyConfig(**yaml.safe_load(Path(path).read_text())))

    def url_allowed(self, url: str) -> tuple[bool, str]:
        u = urlparse(url)
        if u.scheme in ("about", "data", "blob"):
            return True, ""
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in self.cfg.allowed_origins:
            return False, f"origin {origin} not in allowlist"
        for p in self._denied:
            if p.search(u.path):
                return False, f"path {u.path} is denied by policy ({p.pattern})"
        return True, ""

    def classify(self, label: str) -> Risk:
        return Risk.irreversible if any(p.search(label or "") for p in self._irrev) else Risk.read_only

    def check(self, action: str, *, url: str | None = None, element: dict[str, Any] | None = None,
              base_url: str | None = None) -> Decision:
        if action not in self.cfg.allowed_actions:
            return Decision(False, reason=f"action '{action}' not allowed")
        if url:
            ok, why = self.url_allowed(url)
            if not ok:
                return Decision(False, reason=why)
        if element:
            href = element.get("href")
            if href and base_url:
                ok, why = self.url_allowed(urljoin(base_url, href))
                if not ok:
                    return Decision(False, reason=f"link target blocked: {why}")
            label = " ".join(filter(None, [element.get("name"), element.get("text")]))
            if action == "click" and self.classify(label) == Risk.irreversible:
                if self.cfg.irreversible_mode == "block":
                    return Decision(False, Risk.irreversible, f"irreversible action '{label}' is blocked")
                return Decision(True, Risk.irreversible, f"irreversible action '{label}'", needs_approval=True)
        return Decision(True)

    def install_network_guard(self, context, on_block: Callable[[str, str], None],
                              exempt: Callable[[], bool] = lambda: False) -> None:
        def handle(route, request):
            if request.resource_type == "document" and not exempt():
                ok, why = self.url_allowed(request.url)
                if not ok:
                    on_block(request.url, why)
                    return route.abort("blockedbyclient")
            return route.continue_()

        context.route("**/*", handle)


# --------------------------------------------------------------------------- redaction

_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"-?\$?\b\d{1,3}(?:,\d{3})*\.\d{2}\b"), "[AMOUNT]"),
    (re.compile(r"\b\d{12,19}\b"), "[ACCT]"),
]


@dataclass
class Redactor:
    """Masks known sensitive values (params/secrets/outputs registered at runtime)
    plus pattern-based PII. Applied to everything that reaches disk."""

    known: dict[str, str] = field(default_factory=dict)  # value -> label

    def register(self, value: str | None, label: str) -> None:
        if value and len(str(value)) >= 2:
            self.known[str(value)] = label

    def text(self, s: str) -> str:
        for value in sorted(self.known, key=len, reverse=True):
            s = s.replace(value, f"[{self.known[value]}]")
        for pat, repl in _PATTERNS:
            s = pat.sub(repl, s)
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o

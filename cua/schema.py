"""Typed contracts: the capability artifact, the app profile, and the replay result.

Layering (see REPORT.md §2):
  AppProfile  — per vendor product (+version range). Screens every flow in that app
                can run into (session expired, notices, access denied...) and how to
                classify/handle them. Shared by all capabilities and all tenants.
  Capability  — one business flow (inputs -> steps -> outputs). References a profile,
                never a tenant: the base URL and credentials are bound at invocation.
  ReplayResult— what the calling agent gets back: success | business_outcome | failure.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Sensitivity(str, Enum):
    public = "public"
    internal = "internal"
    pii = "pii"  # identifies a person (member #, name, SSN)
    financial = "financial"  # balances, account numbers
    secret = "secret"  # credentials; never leaves the vault, never logged


class Risk(str, Enum):
    read_only = "read_only"  # navigation, search, reading
    reversible = "reversible"  # e.g. filling a draft form
    irreversible = "irreversible"  # posting, opening/closing accounts, money movement


# ------------------------------------------------------------------ targeting


class Locator(_M):
    """One way to find a control. A Target holds several, most robust first; every
    one was verified at record time to match exactly the acted-on element.

    strategy:
      role        — accessibility role + accessible name ({"role","name"})
      label_cell  — legacy form idiom: input in the table cell after a label cell ({"label"})
      row_cell    — table data: cell in the row whose first cell is `row_key`, at column
                    `column` (header text) or `index` ({"row_key","column"|"index"})
      text        — visible text, exact ({"text"})
      css         — structural path; last resort, brittle by construction ({"css"})
    """

    strategy: Literal["role", "label_cell", "row_cell", "text", "css"]
    args: dict[str, Any]  # may contain {param} placeholders, bound at replay


class Target(_M):
    description: str  # human-readable: "Member # input in search form"
    frame: str | None = None  # frame name for frameset apps; None = top document
    locators: list[Locator] = Field(min_length=1)


# ------------------------------------------------------------------ conditions


class Condition(_M):
    kind: Literal["text_visible", "text_absent", "url_matches", "target_visible"]
    value: str | None = None  # text or url regex
    target: Target | None = None
    frame: str | None = None  # where to look for text; None = any frame


class Checkpoint(_M):
    description: str
    all_of: list[Condition] = Field(min_length=1)
    timeout_ms: int = 10_000


# ------------------------------------------------------------------ contract


class ParamSpec(_M):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["string", "integer", "decimal", "date"]
    description: str
    sensitivity: Sensitivity = Sensitivity.internal
    pattern: str | None = None  # validated before any UI interaction


class SecretSpec(_M):
    name: str  # resolved from the per-tenant vault at invocation (env var in this MVP)
    description: str


class OutputSpec(_M):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["string", "integer", "decimal", "date"]
    description: str
    sensitivity: Sensitivity = Sensitivity.internal


class BusinessOutcomeSpec(_M):
    """A legitimate non-success answer the caller must handle (not an error)."""

    code: str  # e.g. "member_not_found"
    description: str
    screen: str  # AppProfile screen id that signals it


# ------------------------------------------------------------------ steps


class ValueRef(_M):
    """Exactly one of param / secret / literal. Literals are allowed only for
    values that are part of the flow itself (e.g. a fixed menu choice)."""

    param: str | None = None
    secret: str | None = None
    literal: str | None = None


class Step(_M):
    id: str
    intent: str  # why this step exists — written for the human reviewer
    action: Literal["goto", "click", "fill", "select", "extract"]
    target: Target | None = None
    url: str | None = None  # goto only; may contain {base_url} and {param} placeholders
    value: ValueRef | None = None  # fill/select
    output: str | None = None  # extract: name of the OutputSpec it fills
    risk: Risk = Risk.read_only
    expect: Checkpoint | None = None  # postcondition, verified before the next step


class AppRef(_M):
    product: str  # vendor product id, e.g. "memberlink"
    profile: str  # AppProfile id this flow was recorded against
    versions: str = "*"  # product versions this artifact is believed to work on


class Provenance(_M):
    recorded_from_run: str
    recorded_at: str
    model: str
    goal: str  # redacted
    notes: list[str] = []


class Capability(_M):
    schema_version: str = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z0-9_.]+$")  # "memberlink.lookup_share_savings_balance"
    version: int = 1
    status: Literal["draft", "approved", "deprecated"] = "draft"
    summary: str
    app: AppRef
    max_risk: Risk = Risk.read_only
    inputs: list[ParamSpec] = []
    secrets: list[SecretSpec] = []
    outputs: list[OutputSpec] = []
    business_outcomes: list[BusinessOutcomeSpec] = []
    steps: list[Step]
    success: Checkpoint
    provenance: Provenance


# ------------------------------------------------------------------ app profile


class ScreenKind(str, Enum):
    business_outcome = "business_outcome"  # legitimate answer; stop and report
    recoverable = "recoverable"  # handle deterministically and continue
    needs_human = "needs_human"  # can't proceed safely without a person
    failure = "failure"  # hard error; stop with evidence


class Handler(_M):
    kind: Literal["click", "wait"]
    target: Target | None = None  # click
    wait_ms: int = 0
    max_attempts: int = 3


class Screen(_M):
    id: str
    kind: ScreenKind
    description: str
    match: list[Condition] = Field(min_length=1)  # all must hold
    handler: Handler | None = None  # recoverable only


class AppProfile(_M):
    id: str
    product: str
    versions: str
    screens: list[Screen]


# ------------------------------------------------------------------ results


class StepTrace(_M):
    step: str
    status: Literal["ok", "recovered", "failed", "skipped", "human"]
    locator_used: str | None = None  # which strategy resolved: drift signal if not the first
    recoveries: list[str] = []
    ms: int = 0


class Success(_M):
    status: Literal["success"] = "success"
    outputs: dict[str, Any]


class BusinessOutcome(_M):
    status: Literal["business_outcome"] = "business_outcome"
    code: str
    description: str
    step: str


class Failure(_M):
    status: Literal["failure"] = "failure"
    code: Literal[
        "invalid_input",
        "target_not_found",
        "target_ambiguous",
        "checkpoint_failed",
        "unrecoverable_screen",
        "needs_human",
        "policy_denied",
        "timeout",
        "internal",
    ]
    step: str | None
    expected: str
    observed: str
    screen: str | None = None
    evidence: list[str] = []  # paths to screenshot / snapshot
    escalation: str | None = None  # intervention request id, if one was raised


Outcome = Annotated[Union[Success, BusinessOutcome, Failure], Field(discriminator="status")]


class ReplayResult(_M):
    capability: str
    version: int
    run_id: str
    result: Outcome
    trace: list[StepTrace]
    drift: list[str] = []  # steps that resolved via a fallback locator
    handoffs: list[dict] = []

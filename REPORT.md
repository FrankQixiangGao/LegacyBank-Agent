# Design report

## 1. Architecture

```
          goal + entry URL                      capability + params (+ tenant base_url, vault)
                │                                              │
     ┌──────────▼──────────┐                        ┌──────────▼──────────┐
     │ DiscoveryAgent       │  Claude (tool use)    │ ReplayEngine         │  no LLM
     │ observe→decide→act   │                       │ classify→resolve→act │
     └──────────┬──────────┘                        │ →verify, resync      │
                │ every action                      └──────────┬──────────┘
     ┌──────────▼──────────┐   ┌───────────────┐               │
     │ Recorder             │   │ AppProfile    │◄──────────────┤ screen classification
     │ element → verified   │   │ (per vendor   │               │
     │ locators, params,    │   │  product)     │               │
     │ checkpoints          │   └───────────────┘               │
     └──────────┬──────────┘                                    │
                ▼                                               │
      artifacts/<id>/vN.yaml ───────────────────────────────────┘
                │
   both paths ──┴──► Policy (action check + network route guard) ──► Surface (WebSurface: Playwright)
                     ControlChannel (lease, CDP-exposed live session)    RunLog (redacted JSONL, masked shots)
```

**Key decisions**

- **One process, synchronous, Python + Playwright.** The hard parts are the
  contracts: the artifact, the result, and the control lease. Infrastructure doesn't
  help with those. Each component sits behind a seam that can become a service later:
  the Surface, the ControlChannel signalling, the artifact store, and the vault.
- **Proxy target: a deliberately hostile mock** (`mock_bank/`, "MemberLink 3.2").
  It has framesets, labels that live in sibling `<td>`s, no ids or test ids, and
  server-rendered screens with generic titles. I chose a local app over a public demo
  site for two reasons. It lets me inject the runtime conditions that matter (notices,
  slow host, session expiry, access denial, abends). And it carries no ToS or real-PII
  risk.
- **Perception: a DOM-derived text rendering, not screenshots.** Every frame is shown
  as lines and table rows, and each actionable or readable element gets a ref
  (`[f2e5]`). The decisive reason is that the model acts on a *ref bound to a real
  element*. The recorder can then derive and verify robust locators from that element.
  Screenshot + coordinates gives you nothing replayable. The rendering does not depend
  on clean semantics: it works on tables, frames, and unlabeled inputs. For pixel-only
  surfaces (Citrix, terminal emulators), see §4.
- **Model:** `claude-opus-5-5` at `effort: medium`, using the custom tools `click`,
  `fill`, `select`, `extract`, `request_human`, and `finish`. Credentials appear to
  the model only as placeholders (`{{secret:operator_password}}`), and the executor
  substitutes the real value.

## 2. Artifact schema

The schema is in `cua/schema.py`, and an example is `evidence/artifact.yaml`. It is
built around the **contract first**, because the reader is a calling agent or a
reviewer, not a test runner:

| Section | Purpose |
|---|---|
| `id`, `version`, `status` (draft/approved/deprecated), `summary` | Identity; versions are immutable files (`v1.yaml`, `v2.yaml`) reviewed like code |
| `app: {product, profile, versions}` | Which **vendor product** it targets. It never names a tenant: base URL and credentials are bound at invocation |
| `inputs[]` | Typed params with `sensitivity` and a `pattern` checked *before* the UI is touched |
| `secrets[]` | Named credentials resolved from the tenant vault; values are never stored |
| `outputs[]` | Typed outputs (`decimal` is returned as an exact string) with `sensitivity` |
| `business_outcomes[]` | Legitimate non-success answers the caller must handle (`member_not_found`, `access_denied`, …) |
| `max_risk` | `read_only` / `reversible` / `irreversible`; gates unattended replay |
| `steps[]` | `intent` (for humans), `action`, `target`, `value: {param\|secret\|literal}`, `risk`, `expect` (postcondition) |
| `success` | Final checkpoint: last screen's conditions plus every output target readable |
| `provenance` | Source run, model, parameterized goal, recorder notes (dropped locators, human interventions) |

How the targeting is built:

- **A target is a ranked list of locator strategies, and every entry was verified at
  record time** to match exactly the acted-on element. The strategies are, in order:
  1. `role`: accessible role + name
  2. `label_cell`: the legacy "label in the neighbouring cell" idiom
  3. `row_cell`: the cell in the row whose `key_column` equals `key`, at column X
  4. `text`
  5. `css` by `name` attribute
  6. structural `css`, as a last resort
- **Canonicalization.** Recorded input values become placeholders, so
  `MBR# = 12345` becomes `MBR# = {member_id}` in locators, URLs, checkpoints, and the
  stored goal.
- **A parameterized locator outranks a generic one.** At record time the
  `View` link happened to be unique. Replay prefers the row keyed by `{member_id}`
  anyway, because that candidate encodes *which* record to open.
- **Locators never contain record data.** In a record's own row, the other cells
  (name, branch) are PII and don't generalize, so they are never used as keys. Amounts
  and account numbers are never keys either. A test asserts this.
- **Checkpoints pin the record, not just the page.** After opening the member, the
  postcondition checks both `MEMBER DETAIL` and `{member_id}`.

**The AppProfile is a separate layer.** A capability describes *its* flow. The
profile (`profiles/memberlink.yaml`) describes every screen the product can show —
session expired, broadcast notice, processing, access denied, not found, abend — and
how to classify and handle each one. The profile is shared by every capability and
every tenant on that product. That keeps error handling out of each recording, which
matters because discovery only ever sees the happy path.

## 3. Determinism & error handling

**Determinism**

- **Exactly-one-match or nothing.** Candidates are tried in order, and a candidate
  that matches more than one element is skipped and reported as ambiguous. The engine
  never takes the first of several matches.
- **No fixed sleeps.** Every wait is a poll (250 ms) with a deadline: target
  resolution, postconditions, and the success checkpoint all work this way.
- **Inputs are validated before the first UI action.** Bad input fails as
  `invalid_input` with an empty trace.
- **Drift is reported, not hidden.** If a target resolves through any candidate but
  the first, the step is listed in `drift[]` with the strategy that matched. Healing is
  tolerated but always visible. This is the signal for §4.

**Runtime conditions.** On *every* poll iteration — before acting, while waiting for a
target, while waiting for a checkpoint — the current screen is classified against the
AppProfile. A "not found" page is therefore recognized immediately, instead of
surfacing ten seconds later as a vague locator timeout. Each screen kind maps directly
onto the result contract:

| Screen kind | Example (mock) | Engine response | Result |
|---|---|---|---|
| `business_outcome` | ML-0207 not found, ML-0403 access denied | stop | `BusinessOutcome{code, step}` (exit 0) |
| `recoverable` | system notice, "processing…" | run the profile handler (click / wait), bounded retries, logged in `trace[].recoveries` | continues |
| `needs_human` | session expired, sign-on failed | escalate if `--handoff`, else stop | `Failure{needs_human}` |
| `failure` | host ABEND | stop, capture | `Failure{unrecoverable_screen}` |
| none, but target missing or checkpoint unmet | unknown state | capture; escalate if enabled | `Failure{target_not_found \| target_ambiguous \| checkpoint_failed}` |

Every `Failure` carries:

- `step`
- `expected` (what the step needed)
- `observed` (what was actually there)
- `screen`
- `evidence[]`: a masked screenshot and a redacted text snapshot
- `escalation`: the intervention request id, if a human was asked

Access denied is classified as a *business outcome*. It is deterministic, retrying
won't fix it, and the decision belongs to the caller (for example, routing to a
supervisor queue). Session expiry is `needs_human` in the MVP. With a vault-backed
re-auth sub-flow in the profile, it would become `recoverable`.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The engine and the artifact only refer to `Target`,
`Locator`, and `Condition`. `WebSurface` is the only module that knows about the DOM.
A desktop surface would implement the same operations (`observe`, `try_resolve`,
`act`, `check`, `screenshot`) against UIA or AX accessibility trees:

| Strategy | Desktop (UIA / AX) equivalent |
|---|---|
| `role` | control type + name |
| `label_cell` | `LabeledBy`, or the nearest label to the left |
| `row_cell` | grid row/column patterns |
| `css` | AutomationId path |

The `frame` field generalizes to "window/pane". Truly pixel-only surfaces (Citrix,
3270 emulators) would get a vision Surface. It would add a `visual_anchor` strategy:
OCR text plus a relative offset, verified at record time like the others. The flow,
contract, result taxonomy, and profile are unchanged.

**Multi-tenant reuse** is designed, not built:

- **What's shared vs. bound per tenant.**
  - A capability is per **product + version range**. Tenant specifics — base URL,
    vault, credentials — are bound at invocation; that already works via `--base-url`
    and the vault.
  - Branding and configuration differences go into a **tenant overlay**:
    `{tenant, capability, patches}`. Patches are keyed by step id: replacement or added
    locator candidates, label text maps (`"Member #:" → "Account #:"`), extra
    `recoverable` screens. The effective artifact is base ⊕ overlay, and every change
    stays reviewable and attributable.
- **Version selection.** The profile carries a product version range. Picking it
  from a fingerprint (the banner's "MemberLink 3.2") would come next.
- **Drift management.**
  - `drift[]` events are aggregated per tenant × capability. A rising fallback rate
    flags a tenant before it breaks.
  - Nightly canary replays against a synthetic member, per tenant, turn silent breakage
    into an alert.
  - A failing tenant gets a re-discovery on its instance, and the diff against the base
    becomes an overlay rather than a fork.

## 5. Escalation & handoff

**Detecting "stuck"**
- *Discovery:* the model calls `request_human`, or the screen is unchanged across
  three actions (dead-end), or the policy needs an approval.
- *Replay:* a `needs_human` screen, or a target or checkpoint failure on an unknown
  screen.
- Never on `policy_denied`: a human cannot be used to bypass the policy.

**Control model: a single-owner lease.**

```
AUTOMATION → AWAITING_HUMAN → HUMAN → (resume) → RESYNC → AUTOMATION
```

- It is persisted in `runs/<id>/control/state.json`, so it is always known who holds
  control.
- The engine does not act while it doesn't hold the lease. While it waits, it pumps
  the browser event loop.

**Intervention request.** `intervention-<id>.json` contains:
- the capability and step, the step's intent, what was expected, and why it stopped
- the screen id
- a masked screenshot
- the **live session's CDP endpoint**, plus the resume command

**Same live session.** The browser runs as a persistent (default) context with
remote debugging enabled. The human either uses the headed window, or attaches from
another process with `connect_over_cdp`. Either way they see the same cookies, frames,
and page. An init script in every frame captures their clicks, changes, and submits.
These are recorded only while the human holds the lease, and they are redacted:
password fields always show `***`, and known values are masked.

**Handing back.** `cua operator resume <run> --by <name>` writes a signal file; a
web console would write the same signal. The engine **does not trust its prior
position**. It re-classifies the screen. Then it walks the steps backwards and resumes
after the furthest step whose postcondition holds *now*. Postconditions include
`{member_id}`, so a human who left the app on a different member does not count as
progress. If no checkpoint matches, a read-only flow restarts from the beginning;
flows with side effects fail rather than guess. The handoff summary is attached to the
`ReplayResult`: who resumed, the note, the duration, and the human's actions.

**Mocked:** the operator console. It is a CLI, plus a scripted operator for
reproducible evidence. In production the same endpoint would back a co-browsing
console (CDP screencast with input forwarding) and an intervention queue with
assignment, SLAs, and audit.

## 6. Safety

**Guardrail layers**

1. **Executor checks.** Every action is checked against the allowlist before it runs:
   action type, origin, path (`/app/admin`, `/__test`, and `/logout` are denied), and
   link targets resolved before clicking. A denial goes back to the model as a tool
   error; the model never has a path that bypasses it.
2. **Network route guard.** It applies to the whole browser context and aborts any
   document navigation outside the allowlist, whoever triggered it. During human
   control it only logs, because an operator needs to be able to recover.
3. **Irreversible actions.** Targets are classified by label patterns
   (post/transfer/close account/confirm…), and steps carry `risk`. I chose
   **require-approval** rather than block:
   - In discovery, such clicks are denied, so a recording can never commit a side
     effect on its own.
   - Replay refuses an `irreversible` capability unless it is `status: approved`
     **and** the invocation passes explicit approval.

   The reasoning: irreversible flows are exactly the valuable ones, but the decision
   to commit belongs to a reviewed artifact plus an accountable caller, not to a model
   mid-run.
4. **Secrets.** The model sees only placeholders, and artifacts store `{secret: name}`.
   Values come from a per-tenant vault (environment variables in this MVP).
5. **Redaction at the write boundary.** Every log or evidence write passes through a
   redactor. It masks registered sensitive values (PII and financial params and
   outputs, secrets) and patterns (amounts, SSNs, long account numbers). Screenshots
   black out inputs and any table cell containing digits. Tests assert that no member
   number, balance, or credential reaches disk or the artifact.

**Limits, stated plainly**

- During discovery the model sees real screen content, including PII. In production,
  discovery should run against test members or in a non-prod instance, under
  zero-data-retention terms.
- Pattern redaction does not catch names in free text. The model's notes are logged
  (truncated) and could contain one. Next step: an NER pass, or logging only
  structured fields.
- Page text could attempt prompt injection. The mitigation is structural, not
  detection: the model can only use the six tools, every action is checked
  independently of the model, and replay has no model at all.
- The irreversible classifier is label-based. Its real backstop is the artifact's
  reviewed `risk` field plus approval.

## 7. Cuts

**Cut, deliberately**

- Real operator console: CLI plus a scripted operator instead.
- Desktop and vision surfaces: interface only.
- Tenant overlays and version fingerprinting: designed in §4.
- Recording human steps as artifact steps: they are captured as evidence, and the
  artifact gets a note blocking approval.
- Vault-backed automatic re-authentication.
- The approval workflow itself: `status` exists, but there is no tooling to move it.
- Probing discovery: deliberately running bad inputs to *learn* new outcome screens.
  Today those screens are authored in the profile.

**Next, in order**

1. Tenant overlays, plus a second variant of the mock app, to demonstrate reuse.
2. Approval states driven by replay stability (N replays → a flakiness score).
3. Bounded single-step LLM recovery on replay failure. It would be policy-checked,
   and anything it learns becomes a proposed overlay, never a silent fix.
4. Expose capabilities as an agent tool catalog: name, typed args, and the
   result union.
5. A re-auth sub-flow in the AppProfile.

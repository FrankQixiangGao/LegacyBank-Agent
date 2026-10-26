# BankGPT — computer-use automation for legacy bank apps

An LLM **discovers** how to do a task in a legacy back-office UI once. The run is
recorded as a typed, versioned **capability artifact**. The artifact is then
**replayed deterministically**, with no model in the loop, and returns typed outputs,
business outcomes, or debuggable failures. When replay gets stuck it **hands the live
session to a human** and resumes afterwards.

Design rationale is in [REPORT.md](REPORT.md).

## Layout

```
cua/
  surface.py    perception + targeting (Playwright, frames, legacy-idiom locators)   ← the surface seam
  agent.py      LLM discovery loop (Claude, tool use) — records as it acts
  recorder.py   element -> verified multi-strategy locators, params, checkpoints
  schema.py     Capability / AppProfile / ReplayResult contracts (pydantic)
  replay.py     deterministic replay + screen classification + resync
  handoff.py    control lease, intervention requests, human-action capture
  policy.py     allowlist, irreversible-action gate, network guard, redaction
  evidence.py   run dir, redacted JSONL log, masked screenshots
  cli.py        cua discover | replay | operator | show
mock_bank/      "MemberLink 3.2": frameset, table forms, no ids, injectable faults
profiles/       AppProfile per vendor product (known screens and how to handle them)
policy.yaml     guardrail config
artifacts/      saved capabilities: <id>/v<N>.yaml
evidence/       demo outputs (discovery + replays)
```

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run playwright install chromium
cp .env.example .env    # then fill in ANTHROPIC_API_KEY
set -a; source .env; set +a
```

- `ANTHROPIC_API_KEY` is needed only for **discovery**. Replay, tests, and the mock app
  run without any live service.
- Credentials for the target app come from a "vault". In this MVP that's the
  `CUA_SECRET_<NAME>` environment variables. The mock app's demo credentials are in
  `.env.example`. They are fake, and the model never sees them.

## Run without live services

```bash
uv run pytest -q
```

This runs 18 tests and needs no API key. The end-to-end tests start the mock app and
drive the discovery loop with a *scripted* stand-in for the model, so the recorder
and replay paths run against a real browser. That stand-in is test-only and is not
used for any evidence.

## Demo path

One command runs everything and writes the results to `evidence/`:

```bash
./scripts/demo.sh
```

Or step by step:

```bash
# 1. start the target app
uv run uvicorn mock_bank.app:app --port 8000 &

# 2. discovery: a real LLM run against the live app -> artifacts/<id>/v1.yaml
uv run cua discover --target http://127.0.0.1:8000/login \
  --goal "Look up member 12345 and read their current share savings balance"

# 3. inspect the artifact
uv run cua show artifacts/memberlink.<capability_name>

# 4. replay with different inputs (no LLM)
A=artifacts/memberlink.<capability_name>
uv run cua replay $A --param member_id=23456 --show-outputs   # success + outputs
uv run cua replay $A --param member_id=99999                  # business_outcome: member_not_found
uv run cua replay $A --param member_id=34567                  # business_outcome: access_denied
uv run cua replay $A --param member_id=12A45                  # failure: invalid_input (before touching the UI)
uv run cua replay $A --param member_id=12345 --fault dialog   # recovered: system notice acknowledged
uv run cua replay $A --param member_id=12345 --fault slow     # recovered: waited out "processing"
uv run cua replay $A --param member_id=12345 --fault app_error  # failure + screenshot evidence
```

`--fault` asks the mock app (through its test-only endpoint) to produce that runtime
condition once.

### Human handoff, by hand

```bash
uv run cua replay $A --param member_id=12345 --fault session_expired --handoff --headed
```

1. The run pauses and prints an intervention request: the reason, the step, and the
   live CDP endpoint.
2. Sign back on in the open browser window. This is the same live session, not a new
   one. Your clicks and inputs are captured, with credentials redacted.
3. Hand control back:
   ```bash
   uv run cua operator resume <run_id> --by <your-name>
   ```
4. The engine re-reads the screen, finds where the flow now stands, and finishes.

`scripts/operator_relogin.py` does the same steps in a script; the demo uses it so the
handoff evidence is reproducible.

## Outputs

Each run writes to `runs/<run_id>/`, or `evidence/runs/` for the demo:

- `log.jsonl`: the structured, redacted event log
- `result.json`
- masked screenshots and redacted text snapshots, captured on failure or intervention
- `intervention-*.json` and `control/state.json`, written when a handoff happens

The CLI prints results redacted by default. `--show-outputs` prints the caller's
view, with real output values.

Built with AI assistance (Claude Code).

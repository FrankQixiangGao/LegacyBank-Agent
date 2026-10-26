# Evidence

`./scripts/demo.sh` writes everything in this directory:

| File | What it shows |
|---|---|
| `discovery_result.json` + `runs/discover-*/log.jsonl` | the real LLM discovery run: every tool call, the policy decisions, the locators recorded |
| `artifact.yaml` | the capability produced by that run |
| `replay_success_other_member.json` | replay with a different member: typed outputs (redacted on disk) |
| `replay_not_found.json` / `replay_access_denied.json` | business outcomes, not failures |
| `replay_invalid_input.json` | rejected before any UI interaction |
| `replay_recover_notice.json` / `replay_recover_slow.json` | recoverable screens handled; see `trace[].recoveries` |
| `replay_hard_failure.json` + `runs/replay-*/0*-*.png` | hard failure with step / expected / observed and masked screenshot |
| `replay_handoff.json` + `runs/replay-*/intervention-*.json` | session expiry → intervention request → operator re-signs on in the live session → resync → success; the human's actions are in `handoffs[].human_actions` |

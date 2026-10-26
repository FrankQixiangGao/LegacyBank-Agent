"""cua — discover, replay, and hand off.

  cua discover --goal "..." --target http://127.0.0.1:8000/login
  cua replay artifacts/<id> --param member_id=12345 [--fault dialog] [--handoff]
  cua operator resume <run_id> --by alice [--note ...]
  cua show artifacts/<id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .evidence import RunLog
from .handoff import ControlChannel, Owner, signal_resume
from .policy import Policy, Redactor
from .store import load_capability, load_profile, save_capability
from .surface import WebSurface

SECRET_PREFIX = "CUA_SECRET_"


def vault_from_env() -> dict[str, str]:
    """Per-tenant credential vault. Here: CUA_SECRET_<NAME> env vars. In production: a
    secrets manager keyed by tenant + app instance; values never reach artifacts or logs."""
    return {k[len(SECRET_PREFIX):].lower(): v for k, v in os.environ.items() if k.startswith(SECRET_PREFIX)}


def _origin(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def _session(args, log: RunLog, policy: Policy) -> tuple[WebSurface, ControlChannel | None]:
    surface = WebSurface(headless=not args.headed, cdp_port=args.cdp_port)
    control = ControlChannel(surface, log) if args.handoff else None
    policy.install_network_guard(
        surface.context,
        on_block=lambda url, why: log.event("network_blocked", url=url, reason=why),
        exempt=lambda: control is not None and control.owner == Owner.human,  # humans are logged, not blocked
    )
    return surface, control


def cmd_discover(args) -> int:
    from .agent import DiscoveryAgent

    policy = Policy.load(args.policy)
    profile = load_profile(args.profile)
    log = RunLog("discover", args.runs, Redactor())
    surface, control = _session(args, log, policy)
    try:
        agent = DiscoveryAgent(surface, policy, profile, log, base_url=_origin(args.target), vault=vault_from_env(),
                               control=control, max_steps=args.max_steps)
        res = agent.run(args.goal, args.target)
        if res.capability:
            path = save_capability(res.capability, args.artifacts)
            res.artifact_path = str(path)
            log.event("artifact_saved", path=str(path), capability=res.capability.id, version=res.capability.version)
        out = {"status": res.status, "run_id": res.run_id, "summary": log.redactor.text(res.summary),
               "artifact": res.artifact_path, "evidence": str(log.dir)}
        log.write_json("result.json", out)
        print(json.dumps(out, indent=2))
        return 0 if res.status == "success" else 1
    finally:
        log.close()
        surface.close()


def cmd_replay(args) -> int:
    from .replay import ReplayEngine

    cap = load_capability(args.artifact)
    profile = load_profile(cap.app.profile)
    policy = Policy.load(args.policy)
    params = dict(p.split("=", 1) for p in args.param)
    if args.fault:
        req = urllib.request.Request(f"{args.base_url}/__test/fault?mode={args.fault}&count={args.fault_count}",
                                     method="POST")
        urllib.request.urlopen(req).read()  # test harness, outside the agent's allowlist on purpose
    log = RunLog("replay", args.runs, Redactor())
    if args.fault:
        log.event("fault_injected", mode=args.fault, count=args.fault_count)
    surface, control = _session(args, log, policy)
    try:
        engine = ReplayEngine(cap, profile, surface, policy, log, base_url=args.base_url, params=params,
                              vault=vault_from_env(), control=control, approve_irreversible=args.approve_irreversible)
        rr = engine.run()
        # Two views: the caller gets real outputs; disk gets the redacted copy.
        log.write_json("result.json", rr.model_dump(mode="json"))
        print(json.dumps(rr.model_dump(mode="json") if args.show_outputs else log.redactor.obj(rr.model_dump(mode="json")),
                         indent=2))
        return {"success": 0, "business_outcome": 0}.get(rr.result.status, 1)
    finally:
        log.close()
        surface.close()


def cmd_operator(args) -> int:
    run_dir = Path(args.runs) / args.run_id
    if args.action == "show":
        for p in sorted(run_dir.glob("intervention-*.json")):
            print(p.read_text())
        state = run_dir / "control" / "state.json"
        print(state.read_text() if state.exists() else "no control channel")
        return 0
    p = signal_resume(run_dir, by=args.by, note=args.note, abort=(args.action == "abort"))
    print(f"signalled {args.action} -> {p}")
    return 0


def cmd_show(args) -> int:
    cap = load_capability(args.artifact)
    print(f"{cap.id} v{cap.version} [{cap.status}] risk={cap.max_risk.value}\n  {cap.summary}")
    print("  inputs : " + ", ".join(f"{p.name}:{p.type}({p.sensitivity.value})" for p in cap.inputs))
    print("  outputs: " + ", ".join(f"{o.name}:{o.type}({o.sensitivity.value})" for o in cap.outputs))
    print("  outcomes: " + ", ".join(o.code for o in cap.business_outcomes))
    for st in cap.steps:
        loc = " > ".join(l.strategy for l in st.target.locators) if st.target else st.url
        print(f"  {st.id:>3} {st.action:<7} {st.intent}  [{loc}]{'  ✓expect' if st.expect else ''}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cua")
    ap.add_argument("--policy", default="policy.yaml")
    ap.add_argument("--runs", default="runs")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def session_flags(p):
        p.add_argument("--headed", action="store_true", help="show the browser (needed for a human to take over)")
        p.add_argument("--handoff", action="store_true", help="escalate to a human instead of failing when stuck")
        p.add_argument("--cdp-port", type=int, default=9333)

    d = sub.add_parser("discover")
    d.add_argument("--goal", required=True)
    d.add_argument("--target", required=True)
    d.add_argument("--profile", default="memberlink-3")
    d.add_argument("--artifacts", default="artifacts")
    d.add_argument("--max-steps", type=int, default=25)
    session_flags(d)
    d.set_defaults(fn=cmd_discover)

    r = sub.add_parser("replay")
    r.add_argument("artifact")
    r.add_argument("--param", action="append", default=[], help="name=value")
    r.add_argument("--base-url", default="http://127.0.0.1:8000", help="tenant's app instance")
    r.add_argument("--fault", choices=["dialog", "slow", "forbidden", "session_expired", "app_error"])
    r.add_argument("--fault-count", type=int, default=1)
    r.add_argument("--approve-irreversible", action="store_true")
    r.add_argument("--show-outputs", action="store_true", help="print unredacted outputs (caller view)")
    session_flags(r)
    r.set_defaults(fn=cmd_replay)

    o = sub.add_parser("operator")
    o.add_argument("action", choices=["show", "resume", "abort"])
    o.add_argument("run_id")
    o.add_argument("--by", default=os.environ.get("USER", "operator"))
    o.add_argument("--note")
    o.set_defaults(fn=cmd_operator)

    s = sub.add_parser("show")
    s.add_argument("artifact")
    s.set_defaults(fn=cmd_show)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

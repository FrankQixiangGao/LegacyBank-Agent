"""Discovery (scripted model) -> artifact -> replay, against the live mock app."""

import json
import re
import subprocess
import sys
import textwrap
import threading

import pytest

from cua.agent import DiscoveryAgent
from cua.evidence import RunLog
from cua.handoff import ControlChannel
from cua.replay import ReplayEngine
from cua.store import load_capability, load_profile, save_capability
from cua.surface import WebSurface
from mock_bank import app as mock

from .conftest import VAULT, ScriptedClient


def ref(obs: str, pattern: str) -> str:
    m = re.search(r"\[((?:f\d+)?e\d+)\] " + pattern, obs)
    assert m, f"{pattern!r} not in observation:\n{obs}"
    return m.group(1)


def row_ref(obs: str, key: str, col: int) -> str:
    line = next(l for l in obs.splitlines() if key in l and l.startswith("row:"))
    return re.findall(r"\[((?:f\d+)?e\d+)\]", line)[col]


SCRIPT = [
    lambda o: ("fill", {"ref": row_ref(o, "Operator ID", 1), "intent": "Enter operator ID",
                        "value": "{{secret:operator_id}}"}),
    lambda o: ("fill", {"ref": ref(o, "password-box"), "intent": "Enter password",
                        "value": "{{secret:operator_password}}"}),
    lambda o: ("click", {"ref": ref(o, 'button "Sign On"'), "intent": "Sign on", "expect_text": "MEMBER SEARCH"}),
    lambda o: ("fill", {"ref": row_ref(o, "Member #:", 1), "intent": "Enter the member number", "value": "12345",
                        "param_name": "member_id", "param_description": "Member (account holder) number",
                        "param_sensitivity": "pii"}),
    lambda o: ("click", {"ref": ref(o, 'button "Search"'), "intent": "Run the search",
                         "expect_text": "SEARCH RESULTS"}),
    lambda o: ("click", {"ref": ref(o, 'link "View"'), "intent": "Open the member record",
                         "expect_text": "MEMBER DETAIL"}),
    lambda o: ("extract", {"ref": row_ref(o, "SHARE SAVINGS", 2), "name": "share_savings_balance",
                           "type": "decimal", "description": "Current balance of the SHARE SAVINGS account",
                           "sensitivity": "financial"}),
    lambda o: ("finish", {"status": "success", "summary": "read balance",
                          "capability_name": "lookup_share_savings_balance",
                          "capability_summary": "Look up a member by number and return the share savings balance."}),
]


@pytest.fixture(scope="module")
def artifact(tmp_path_factory, server):
    from .conftest import BASE
    from cua.policy import Policy, PolicyConfig
    policy = Policy(PolicyConfig(allowed_origins=[BASE], denied_paths=["^/app/admin", "^/__test"]))
    root = tmp_path_factory.mktemp("disc")
    log = RunLog("discover", root / "runs")
    s = WebSurface(headless=True, cdp_port=None)
    try:
        agent = DiscoveryAgent(s, policy, load_profile("memberlink-3"), log, base_url=server, vault=VAULT,
                               client=ScriptedClient(SCRIPT))
        res = agent.run("look up member 12345 and read their share savings balance", f"{server}/login")
    finally:
        log.close()
        s.close()
    assert res.status == "success", res.summary
    path = save_capability(res.capability, root / "artifacts")
    logtext = (log.dir / "log.jsonl").read_text()
    assert "demo-pass-123" not in logtext and "2,481.17" not in logtext and "12345" not in logtext
    return load_capability(path)


def test_artifact_shape(artifact):
    assert [i.name for i in artifact.inputs] == ["member_id"]
    assert artifact.inputs[0].pattern == r"^\d+$"
    assert {s.name for s in artifact.secrets} == {"operator_id", "operator_password"}
    assert artifact.outputs[0].name == "share_savings_balance"
    view = artifact.steps[6]
    # the parameterized row locator outranks the merely-unique 'View' link
    assert view.target.locators[0].strategy == "row_cell"
    assert view.target.locators[0].args["key"] == "{member_id}"
    extract = artifact.steps[7]
    assert extract.target.locators[0].args["key"] == "SHARE SAVINGS"
    assert all(l.strategy != "text" for l in extract.target.locators)
    # no record data (member name/branch) used as locator keys
    assert all("SAMPLE" not in str(l.args) for st in artifact.steps if st.target for l in st.target.locators)
    dump = artifact.model_dump_json()
    assert "12345" not in dump and "teller01" not in dump and "2,481.17" not in dump


def replay(artifact, server, policy, tmp_path, params, fault=None, control=False, cdp_port=None):
    if fault:
        mock.FAULT.update(mode=fault, remaining=1)
    log = RunLog("replay", tmp_path / "runs")
    s = WebSurface(headless=True, cdp_port=cdp_port)
    ch = ControlChannel(s, log, timeout_s=60) if control else None
    try:
        return ReplayEngine(artifact, load_profile("memberlink-3"), s, policy, log, base_url=server,
                            params=params, vault=VAULT, control=ch).run(), log
    finally:
        log.close()
        s.close()


def test_replay_success_other_member(artifact, server, policy, tmp_path):
    rr, log = replay(artifact, server, policy, tmp_path, {"member_id": "23456"})
    assert rr.result.status == "success"
    on_disk = "".join(p.read_text() for p in log.dir.glob("*.json*"))
    assert "23456" not in on_disk and "1,906.33" not in on_disk and "teller01" not in on_disk
    assert rr.result.outputs == {"share_savings_balance": "25.00"}
    assert not rr.drift


def test_replay_not_found_is_business_outcome(artifact, server, policy, tmp_path):
    rr, _ = replay(artifact, server, policy, tmp_path, {"member_id": "99999"})
    assert rr.result.status == "business_outcome" and rr.result.code == "member_not_found"


def test_replay_access_denied(artifact, server, policy, tmp_path):
    rr, _ = replay(artifact, server, policy, tmp_path, {"member_id": "34567"})
    assert rr.result.status == "business_outcome" and rr.result.code == "access_denied"


def test_invalid_input_never_touches_ui(artifact, server, policy, tmp_path):
    rr, _ = replay(artifact, server, policy, tmp_path, {"member_id": "12A45"})
    assert rr.result.status == "failure" and rr.result.code == "invalid_input" and not rr.trace


@pytest.mark.parametrize("fault,screen", [("dialog", "system_notice"), ("slow", "processing")])
def test_recoverable_screens(artifact, server, policy, tmp_path, fault, screen):
    rr, _ = replay(artifact, server, policy, tmp_path, {"member_id": "12345"}, fault=fault)
    assert rr.result.status == "success", rr.result
    assert rr.result.outputs["share_savings_balance"] == "2481.17"
    assert any(screen in t.recoveries for t in rr.trace)


def test_hard_failure_has_evidence(artifact, server, policy, tmp_path):
    rr, _ = replay(artifact, server, policy, tmp_path, {"member_id": "12345"}, fault="app_error")
    f = rr.result
    assert f.status == "failure" and f.code == "unrecoverable_screen" and f.screen == "host_abend"
    assert f.step and any(p.endswith(".png") for p in f.evidence)


def test_session_expired_without_handoff_fails_needing_human(artifact, server, policy, tmp_path):
    rr, _ = replay(artifact, server, policy, tmp_path, {"member_id": "12345"}, fault="session_expired")
    assert rr.result.status == "failure" and rr.result.code == "needs_human"


OPERATOR = textwrap.dedent("""
    import glob, json, sys, time
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    runs, cdp = sys.argv[1], sys.argv[2]
    while not glob.glob(runs + "/*/intervention-*.json"): time.sleep(0.2)
    req = json.loads(Path(glob.glob(runs + "/*/intervention-*.json")[0]).read_text())
    with sync_playwright() as p:
        pg = p.chromium.connect_over_cdp(cdp).contexts[0].pages[0]
        pg.frame(name="main").get_by_role("link", name="Return to Sign On").click(); pg.wait_for_url("**/login")
        pg.locator("input[name=u]").fill("teller01"); pg.locator("input[name=p]").fill("demo-pass-123")
        pg.get_by_role("button", name="Sign On").click(); pg.wait_for_timeout(1500)
    run_dir = Path(glob.glob(runs + "/*/control")[0]).parent
    (run_dir / "control" / "resume.json").write_text(json.dumps({"by": "test-operator", "note": "re-signed on"}))
""")


def test_handoff_on_live_session(artifact, server, policy, tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    op = subprocess.Popen([sys.executable, "-c", OPERATOR, str(runs), "http://127.0.0.1:9344"])
    try:
        rr, log = replay(artifact, server, policy, tmp_path, {"member_id": "12345"}, fault="session_expired",
                         control=True, cdp_port=9344)
    finally:
        op.wait(timeout=30)
    assert rr.result.status == "success", rr.result
    assert rr.result.outputs["share_savings_balance"] == "2481.17"
    h = rr.handoffs[0]
    assert h["outcome"] == "resumed" and h["resumed_by"] == "test-operator"
    assert any(a.get("type") == "click" for a in h["human_actions"])
    assert "demo-pass-123" not in json.dumps(h)
    assert any(t.status == "human" for t in rr.trace)

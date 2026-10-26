import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn

from cua.policy import Policy, PolicyConfig
from mock_bank import app as mock

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="session")
def server():
    cfg = uvicorn.Config(mock.app, port=PORT, log_level="warning")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                break
        time.sleep(0.1)
    yield BASE
    srv.should_exit = True


@pytest.fixture(autouse=True)
def clear_fault():
    mock.FAULT.update(mode=None, remaining=0)
    yield


@pytest.fixture
def policy():
    return Policy(PolicyConfig(
        allowed_origins=[BASE],
        denied_paths=["^/app/admin", "^/__test", "^/logout"],
        irreversible_patterns=[r"(?i)\b(post|transfer|delete|open account|close account|confirm)\b"],
    ))


VAULT = {"operator_id": "teller01", "operator_password": "demo-pass-123"}


class ScriptedClient:
    """Stands in for the Anthropic client in tests: picks the next tool call by
    pattern-matching the observation, the way the model would. Not used for evidence."""

    def __init__(self, script):
        self.script = list(script)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.n = 0

    def create(self, **kw):
        last = kw["messages"][-1]["content"]
        obs = last if isinstance(last, str) else next(b["text"] for b in last if b.get("type") == "text")
        name, inp = self.script.pop(0)(obs)
        self.n += 1
        block = SimpleNamespace(type="tool_use", id=f"tu{self.n}", name=name, input=inp)
        return SimpleNamespace(content=[block], stop_reason="tool_use")

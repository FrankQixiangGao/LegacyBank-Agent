import pytest

from cua.policy import Redactor
from cua.replay import parse_value
from cua.schema import Risk


def test_allowlist(policy):
    assert policy.check("goto", url="http://127.0.0.1:8765/app/search").allowed
    assert not policy.check("goto", url="https://evil.example/").allowed
    assert not policy.check("goto", url="http://127.0.0.1:8765/app/admin").allowed
    assert not policy.check("drag").allowed


def test_link_into_denied_path_is_blocked(policy):
    d = policy.check("click", element={"tag": "a", "text": "System Admin", "href": "/app/admin"},
                     base_url="http://127.0.0.1:8765/app/banner")
    assert not d.allowed and "denied" in d.reason


def test_irreversible_requires_approval(policy):
    d = policy.check("click", element={"tag": "input", "name": "Post Transfer", "text": "Post Transfer"})
    assert d.allowed and d.needs_approval and d.risk == Risk.irreversible
    assert not policy.check("click", element={"tag": "input", "name": "Search", "text": "Search"}).needs_approval


def test_redactor():
    r = Redactor()
    r.register("12345", "member_id")
    out = r.text("member 12345 has $2,481.17, ssn 123-45-6789, acct 4111111111111111")
    assert out == "member [member_id] has [AMOUNT], ssn [SSN], acct [ACCT]"


@pytest.mark.parametrize("raw,want", [("2,481.17", "2481.17"), ("-8,902.55", "-8902.55"),
                                      ("$25.00", "25.00"), ("1,000.00-", "-1000.00")])
def test_parse_decimal(raw, want):
    assert parse_value(raw, "decimal") == want

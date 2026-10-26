"""MemberLink 3.2 — a deliberately "legacy" core-banking servicing app used as the
proxy target.

Hostile on purpose: frameset layout, table-based forms whose labels live in sibling
<td>s (no <label for>), no ids / test ids, server-rendered pages, generic <title>s on
some screens. All data is fake.

Runtime faults can be injected through /__test/fault (test harness only, never on
the agent allowlist) so replay can be exercised against the exceptional states a
real back-office app produces.
"""

from __future__ import annotations

import html
import secrets
import time

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI(title="MemberLink mock")

USERS = {"teller01": "demo-pass-123"}

MEMBERS = {
    "12345": {
        "name": "SAMPLE, JANE Q",
        "branch": "001 - MAIN ST",
        "opened": "03/14/2011",
        "accounts": [
            ("S01", "SHARE SAVINGS", "2,481.17", "OPEN"),
            ("S10", "SHARE DRAFT CHECKING", "512.40", "OPEN"),
            ("S70", "HOLIDAY CLUB", "300.00", "OPEN"),
            ("L01", "AUTO LOAN 60MO", "-8,902.55", "OPEN"),
        ],
    },
    "23456": {
        "name": "EXAMPLE, ROBERT T",
        "branch": "004 - RIVERSIDE",
        "opened": "07/02/2019",
        "accounts": [
            ("S01", "SHARE SAVINGS", "25.00", "OPEN"),
            ("S10", "SHARE DRAFT CHECKING", "1,906.33", "OPEN"),
        ],
    },
    "34567": {
        "name": "PLACEHOLDER, ALEX",
        "branch": "001 - MAIN ST",
        "opened": "01/09/2024",
        "accounts": [("S10", "SHARE DRAFT CHECKING", "88.10", "OPEN")],
    },
}

SESSIONS: dict[str, str] = {}
# Members the signed-on teller profile is not authorized to view (permission denial).
RESTRICTED = {"34567"}

# Injected fault: {"mode": str, "remaining": int}
FAULT: dict = {"mode": None, "remaining": 0}
FAULT_MODES = {"dialog", "slow", "forbidden", "session_expired", "app_error"}


def _take_fault(*modes: str) -> str | None:
    if FAULT["mode"] in modes and FAULT["remaining"] > 0:
        FAULT["remaining"] -= 1
        mode = FAULT["mode"]
        if FAULT["remaining"] == 0:
            FAULT["mode"] = None
        return mode
    return None


STYLE = """
<style>
body { font-family: 'Courier New', monospace; font-size: 13px; background: #d4d0c8; margin: 6px; }
table.f { border: 2px outset #fff; background: #ece9d8; }
td.h { background: #000080; color: #fff; font-weight: bold; padding: 3px 6px; }
td { padding: 2px 6px; }
table.g td { border: 1px solid #808080; background: #fff; }
.msg { color: #a00000; font-weight: bold; }
</style>
"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<html><head><title>{html.escape(title)}</title>{STYLE}</head><body>{body}</body></html>"
    )


def _user(request: Request) -> str | None:
    return SESSIONS.get(request.cookies.get("ML_SESSION", ""))


def _expired() -> HTMLResponse:
    return page(
        "MemberLink",
        """<table class=f width=500><tr><td class=h>MESSAGE ML-0041</td></tr>
<tr><td class=msg>YOUR SESSION HAS EXPIRED. PLEASE SIGN ON AGAIN.</td></tr>
<tr><td><a href="/login" target="_top">Return to Sign On</a></td></tr></table>""",
    )


def _guard(request: Request) -> HTMLResponse | None:
    """Session check + session-expiry fault for every main-frame screen."""
    if _take_fault("session_expired"):
        SESSIONS.pop(request.cookies.get("ML_SESSION", ""), None)
        return _expired()
    if not _user(request):
        return _expired()
    if _take_fault("app_error"):
        return page(
            "MemberLink",
            """<table class=f width=500><tr><td class=h>SYSTEM ERROR</td></tr>
<tr><td class=msg>ABEND S0C7 IN MODULE MLINQ020. CONTACT HELP DESK.</td></tr></table>""",
        )
    return None


# ---------------------------------------------------------------- sign on


@app.get("/")
def root():
    return RedirectResponse("/login")


@app.get("/login")
def login_form(error: str = ""):
    msg = f"<tr><td colspan=2 class=msg>{html.escape(error)}</td></tr>" if error else ""
    return page(
        "MemberLink 3.2",
        f"""<center><br><br>
<form method=post action="/login">
<table class=f width=360>
<tr><td class=h colspan=2>MemberLink 3.2 &mdash; Sign On</td></tr>{msg}
<tr><td>Operator ID</td><td><input name=u size=16></td></tr>
<tr><td>Password</td><td><input name=p type=password size=16></td></tr>
<tr><td></td><td><input type=submit value="Sign On"></td></tr>
</table></form></center>""",
    )


@app.post("/login")
def login(u: str = Form(""), p: str = Form("")):
    if USERS.get(u) != p:
        return RedirectResponse("/login?error=INVALID+OPERATOR+ID+OR+PASSWORD", status_code=303)
    sid = secrets.token_hex(16)
    SESSIONS[sid] = u
    resp = RedirectResponse("/app", status_code=303)
    resp.set_cookie("ML_SESSION", sid, httponly=True)
    return resp


@app.get("/app")
def frameset(request: Request):
    if not _user(request):
        return RedirectResponse("/login")
    return HTMLResponse(
        """<html><head><title>MemberLink 3.2</title></head>
<frameset rows="56,*" border=0>
  <frame name="banner" src="/app/banner" scrolling=no>
  <frame name="main" src="/app/search">
</frameset></html>"""
    )


@app.get("/app/banner")
def banner(request: Request):
    user = _user(request) or "?"
    return page(
        "banner",
        f"""<table width=100% bgcolor=#000080><tr>
<td><font color=white><b>MemberLink 3.2</b> &nbsp; OPR: {html.escape(user)}</font></td>
<td align=right>
<a href="/app/search" target="main"><font color=yellow>Member Search</font></a> |
<a href="/app/admin" target="main"><font color=yellow>System Admin</font></a> |
<a href="/logout" target="_top"><font color=yellow>Sign Off</font></a></td></tr></table>""",
    )


@app.get("/logout")
def logout(request: Request):
    SESSIONS.pop(request.cookies.get("ML_SESSION", ""), None)
    return RedirectResponse("/login")


@app.get("/app/admin")
def admin(request: Request):
    if (r := _guard(request)) is not None:
        return r
    return page("MemberLink", "<b>SYSTEM ADMINISTRATION</b><br>Operator maintenance, GL posting, rate tables.")


# ---------------------------------------------------------------- inquiry flow


@app.get("/app/search")
def search_form(request: Request, error: str = ""):
    if (r := _guard(request)) is not None:
        return r
    msg = f"<tr><td colspan=2 class=msg>{html.escape(error)}</td></tr>" if error else ""
    return page(
        "MemberLink",
        f"""<form method=get action="/app/results">
<table class=f width=480>
<tr><td class=h colspan=2>MBR INQUIRY &mdash; MEMBER SEARCH</td></tr>{msg}
<tr><td width=140>Member #:</td><td><input name=mbr size=12></td></tr>
<tr><td>Last Name:</td><td><input name=ln size=20></td></tr>
<tr><td></td><td><input type=submit value="Search"> <input type=reset value="Clear"></td></tr>
</table></form>""",
    )


@app.get("/app/results")
def results(request: Request, mbr: str = "", ln: str = "", ack: str = ""):
    if (r := _guard(request)) is not None:
        return r
    mbr, ln = mbr.strip(), ln.strip().upper()
    if mbr and not mbr.isdigit():
        return RedirectResponse("/app/search?error=ML-0102+MEMBER+NUMBER+MUST+BE+NUMERIC", status_code=303)
    if not ack and _take_fault("dialog"):
        q = f"mbr={html.escape(mbr)}&ln={html.escape(ln)}&ack=1"
        return page(
            "MemberLink",
            f"""<table class=f width=480><tr><td class=h>SYSTEM NOTICE</td></tr>
<tr><td>Scheduled maintenance tonight 23:00-01:00 ET. Online posting will be unavailable.</td></tr>
<tr><td><form method=get action="/app/results">
<input type=hidden name=mbr value="{html.escape(mbr)}"><input type=hidden name=ln value="{html.escape(ln)}">
<input type=hidden name=ack value=1><input type=submit value="Acknowledge"></form></td></tr></table>""",
        )
    hits = [
        (num, m)
        for num, m in MEMBERS.items()
        if (mbr and num == mbr) or (ln and m["name"].startswith(ln))
    ]
    if not hits:
        return page(
            "MemberLink",
            """<table class=f width=480><tr><td class=h>MBR INQUIRY &mdash; SEARCH RESULTS</td></tr>
<tr><td class=msg>ML-0207 NO MEMBERS FOUND MATCHING SEARCH CRITERIA</td></tr>
<tr><td><a href="/app/search">New Search</a></td></tr></table>""",
        )
    rows = "".join(
        f"<tr><td>{num}</td><td>{html.escape(m['name'])}</td><td>{html.escape(m['branch'])}</td>"
        f'<td><a href="/app/member?m={num}">View</a></td></tr>'
        for num, m in hits
    )
    return page(
        "MemberLink",
        f"""<table class=f width=560><tr><td class=h>MBR INQUIRY &mdash; SEARCH RESULTS</td></tr>
<tr><td><table class=g width=100%><tr><td><b>MBR#</b></td><td><b>NAME</b></td><td><b>BRANCH</b></td><td></td></tr>
{rows}</table></td></tr><tr><td><a href="/app/search">New Search</a></td></tr></table>""",
    )


@app.get("/app/member")
def member(request: Request, m: str, waited: str = ""):
    if (r := _guard(request)) is not None:
        return r
    if not waited and _take_fault("slow"):
        return HTMLResponse(
            f"""<html><head><title>MemberLink</title><meta http-equiv="refresh" content="3;url=/app/member?m={html.escape(m)}&waited=1">{STYLE}</head>
<body><b>PROCESSING REQUEST &mdash; PLEASE WAIT...</b></body></html>"""
        )
    if m in RESTRICTED or _take_fault("forbidden"):
        return page(
            "MemberLink",
            """<table class=f width=480><tr><td class=h>ACCESS DENIED</td></tr>
<tr><td class=msg>ML-0403 OPERATOR PROFILE NOT AUTHORIZED FOR THIS MEMBER RECORD</td></tr>
<tr><td><a href="/app/search">New Search</a></td></tr></table>""",
        )
    mem = MEMBERS.get(m)
    if not mem:
        return page("MemberLink", "<span class=msg>ML-0207 MEMBER NOT ON FILE</span>")
    rows = "".join(
        f"<tr><td>{sfx}</td><td>{desc}</td><td align=right>{bal}</td><td>{st}</td></tr>"
        for sfx, desc, bal, st in mem["accounts"]
    )
    return page(
        "MemberLink",
        f"""<table class=f width=600><tr><td class=h colspan=2>MBR INQUIRY &mdash; MEMBER DETAIL</td></tr>
<tr><td width=120>Member:</td><td>{m} &nbsp; {html.escape(mem['name'])}</td></tr>
<tr><td>Branch:</td><td>{html.escape(mem['branch'])}</td></tr>
<tr><td>Opened:</td><td>{mem['opened']}</td></tr>
<tr><td colspan=2><table class=g width=100%>
<tr><td><b>SFX</b></td><td><b>DESCRIPTION</b></td><td align=right><b>BALANCE</b></td><td><b>STATUS</b></td></tr>
{rows}</table></td></tr>
<tr><td colspan=2><a href="/app/search">New Search</a></td></tr></table>""",
    )


# ---------------------------------------------------------------- test harness


@app.post("/__test/fault")
def set_fault(mode: str = "", count: int = 1):
    if mode and mode not in FAULT_MODES:
        return {"error": f"unknown mode; expected one of {sorted(FAULT_MODES)}"}
    FAULT.update(mode=mode or None, remaining=count if mode else 0)
    return {"fault": FAULT, "at": time.time()}


@app.post("/__test/expire_sessions")
def expire_sessions():
    SESSIONS.clear()
    return {"ok": True}

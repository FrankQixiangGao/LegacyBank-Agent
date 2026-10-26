"""Scripted stand-in for a human operator (used by the demo so the handoff evidence is
reproducible). It does exactly what a person would do in the browser window: waits
for an intervention request, attaches to the *live* session over CDP, signs back on,
and hands control back. A real person would use `--headed` and then run
`cua operator resume <run_id>`.

usage: python scripts/operator_relogin.py <runs_dir> [cdp_endpoint]
"""

import glob
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

runs = sys.argv[1]
cdp = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:9333"
start = time.time()
while True:
    reqs = [p for p in glob.glob(f"{runs}/*/intervention-*.json") if os.path.getmtime(p) > start - 1]
    if reqs:
        break
    if time.time() - start > 120:
        sys.exit("no intervention request within 120s")
    time.sleep(0.3)
req = json.loads(Path(reqs[0]).read_text())
print(f"[operator] took request {req['request_id']}: {req['reason']}")
with sync_playwright() as p:
    page = p.chromium.connect_over_cdp(req["live_session"]["cdp_endpoint"]).contexts[0].pages[0]
    main = page.frame(name="main")
    if main and main.get_by_role("link", name="Return to Sign On").count():
        main.get_by_role("link", name="Return to Sign On").click()
        page.wait_for_url("**/login")
    page.locator("input[name=u]").fill(os.environ["CUA_SECRET_OPERATOR_ID"])
    page.locator("input[name=p]").fill(os.environ["CUA_SECRET_OPERATOR_PASSWORD"])
    page.get_by_role("button", name="Sign On").click()
    page.wait_for_timeout(1500)
run_dir = Path(reqs[0]).parent
(run_dir / "control" / "resume.json").write_text(json.dumps({"by": "demo-operator", "note": "re-signed on after session expiry"}))
print("[operator] handed control back")

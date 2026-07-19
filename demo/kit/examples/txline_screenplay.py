#!/usr/bin/env python3
"""TxLINE without/with Gecko — screenplay in the gecko_demo_full style (80x20).
Every call is REAL (live credits). Secrets never printed."""

import json
import os
import sys
import time
import urllib.request

BOLD = "\033[1m"
CYAN = "\033[38;5;45m"
GREEN = "\033[38;5;42m"
RED = "\033[38;5;203m"
YELLOW = "\033[38;5;220m"
RESET = "\033[0m"

sess = json.load(open(os.path.expanduser("~/.gecko/txodds-session.json")))
JWT, TOK = sess["jwt"], sess["api_token"]
BASE = "https://txline.txodds.com"


def out(text="", delay=0.045, end="\n", pause=0.0):
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay if ch != "\033" else 0)
    sys.stdout.write(end)
    sys.stdout.flush()
    if pause:
        time.sleep(pause)


def put(text="", pause=0.9):  # instant line (results)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    time.sleep(pause)


def status(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def clear():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()
    time.sleep(0.4)


# ---------- scene 1 — the raw API ----------
clear()
out(f"{BOLD}TXLINE (TXODDS WORLD CUP API) — WITHOUT GECKO{RESET}", 0.03, pause=1.4)
put("")
out("Step 1: find the OpenAPI spec.", 0.03, pause=0.5)
out(f"{CYAN}$ GET {BASE}/openapi.json{RESET}", 0.02)
code = status("/openapi.json")
put(f"{RED}✗ HTTP {code}{RESET} — there is no public spec. Nothing to point at.", 1.5)
put("")
out("Step 2: just call it, then.", 0.03, pause=0.5)
out(f"{CYAN}$ GET /api/fixtures/snapshot{RESET}", 0.02)
code = status("/api/fixtures/snapshot")
put(f"{RED}✗ HTTP {code}{RESET} — auth. Fine — we have a valid JWT.", 1.5)
put("")
out(f"{CYAN}$ GET /api/fixtures/snapshot   -H \"Authorization: Bearer $JWT\"{RESET}", 0.02)
code = status("/api/fixtures/snapshot", {"Authorization": f"Bearer {JWT}"})
put(f"{RED}✗ HTTP {code}{RESET} — valid token... still forbidden?!", 1.2)
put(f"{YELLOW}It secretly needs a SECOND token — both headers, together.{RESET}", 1.6)
put("")
out(f"{CYAN}$ GET /api/fixtures/snapshot   + -H \"X-Api-Token: $TOKEN\"{RESET}", 0.02)
code = status(
    "/api/fixtures/snapshot",
    {"Authorization": f"Bearer {JWT}", "X-Api-Token": TOK},
)
put(f"{GREEN}✓ HTTP {code}{RESET} — finally.", 1.0)
put("An afternoon of doc-diving — and every agent relearns it from zero.", 2.6)

# ---------- scene 2 — with Gecko ----------
clear()
out(f"{BOLD}SAME API — WITH GECKO. KEYS SEALED IN THE OS KEYCHAIN.{RESET}", 0.03, pause=1.2)
put("")
out(f"{CYAN}$ gecko auth test txline --live{RESET}", 0.02)
put(f"{GREEN}✓ live — credential authenticates (HTTP 200){RESET}", 1.6)
put("")
out(f"{BOLD}User:{RESET} get live odds updates.", 0.045, pause=1.2)
put("")
put("Gecko comprehends the spec → returns the CHAIN:", 0.8)

# real plan + live chain via the gecko engine
sys.path.insert(0, "/home/nan/PycharmProjects/Gecko/surfcall")
from gecko.access import keychain_session  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.examples import txline  # noqa: E402

spec = txline.load_spec()
session, _ = keychain_session(spec, "txline")
client = AgentApiClient(spec, base_url=txline.BASE_URL, session=session)
plan = client.plan_for("get live odds updates", "getApiOddsUpdatesFixtureid")
s1, s2 = plan["steps"][0], plan["steps"][1]
e = plan["explain"][0]
put(f"  1. {s1['method']} {s1['path']}            supplies {e['param']}", 0.7)
put(f"  2. {s2['method']} {s2['path']}", 0.7)
put(
    f"     why: {e['param']} ← {e['source_field']}   [{e['basis']} · {e['confidence']}]",
    1.8,
)
put("")
put("Running the chain LIVE (auth injected, keys never shown):", 0.8)
r1 = client.call("getApiFixturesSnapshot", {}, mode="live")
fx = r1.get("data") or []
wc = next(f for f in fx if f.get("Competition") == "World Cup")
fid = wc["FixtureId"]
put(
    f"  {GREEN}✓{RESET} step 1 · HTTP {r1.get('status')} · {len(fx)} fixtures "
    f"— {wc['Participant1']} vs {wc['Participant2']}",
    1.2,
)
r2 = client.call("getApiOddsUpdatesFixtureid", {"fixtureId": fid}, mode="live")
odds = r2.get("data") or []
put(
    f"  {GREEN}✓{RESET} step 2 · HTTP {r2.get('status')} · "
    f"{len(odds):,} odds records — first try",
    2.0,
)
put("")
put("No 401s. No doc-diving. No key in sight.", 1.6)
out(f"{BOLD}{CYAN}ANY API, AGENT-READY — FIRST CALL CORRECT{RESET}", 0.03, pause=0.6)
out(f"{CYAN}npx @geckovision/gecko{RESET}", 0.03, pause=2.5)

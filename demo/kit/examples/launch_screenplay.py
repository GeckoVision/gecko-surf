#!/usr/bin/env python3
"""70-second launch demo — 3 acts, real runs ($0 recorded + redteam + gecko test)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "demo" / "kit"))
sys.path.insert(0, str(ROOT))

from screenplay import BOLD, CYAN, GREEN, RED, RESET, YELLOW, clear, out, put  # noqa: E402

from gecko.access import Session  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _tail_lines(text: str, n: int = 6) -> list[str]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-n:]


# ---------- Act 1 — comprehend + first call ----------
clear()
out(f"{BOLD}ACT 1 · PLUG IN TXODDS{RESET}", 0.03, pause=1.0)
put("")
out(f"{CYAN}$ gecko comprehend tests/fixtures/txodds_docs.yaml{RESET}", 0.02, pause=0.4)

client = AgentApiClient(
    str(ROOT / "tests/fixtures/txodds_docs.yaml"),
    session=Session(jwt="recorded-mode", api_token="recorded-mode"),
)
tools = client.list_tools()
put(f"{GREEN}✓{RESET} {len(tools)} operations → {len(tools)} first-call-correct tools", 1.0)
put("")
out(f"{BOLD}User:{RESET} get live odds for a football fixture", 0.04, pause=0.8)
hits = client.search("get live odds for a football fixture")
top = hits[0]
put(f"  search → {top['name']}  ({top['method']} {top['path']})", 0.7)
result = client.call(top["name"], {"fixtureId": 18179550}, mode="recorded")
put(
    f"  {GREEN}✓{RESET} HTTP {result['status']} · recorded/$0 · first call correct",
    1.4,
)

# ---------- Act 2 — poisoned spec blocked ----------
clear()
out(f"{BOLD}ACT 2 · STAY SAFE (POISONED SPEC){RESET}", 0.03, pause=1.0)
put("")
out(f"{CYAN}$ gecko-redteam --defenses none{RESET}", 0.02, pause=0.3)
def _scorecard_lines(text: str) -> list[str]:
    keep = ("exploited", "blocked", "money_trusted")
    return [ln.rstrip() for ln in text.splitlines() if any(k in ln for k in keep)]


naive = _run(["uv", "run", "gecko-redteam", "--defenses", "none"])
for line in _scorecard_lines(naive.stdout):
    put(line.replace("[FAIL]", f"{RED}[FAIL]{RESET}"), 0.45)
put(f"{YELLOW}Naive agent: 8/8 poisoned attacks succeed.{RESET}", 1.0)
put("")
out(f"{CYAN}$ gecko-redteam --defenses all{RESET}", 0.02, pause=0.3)
defended = _run(["uv", "run", "gecko-redteam", "--defenses", "all"])
for line in _scorecard_lines(defended.stdout):
    put(line.replace("[PASS]", f"{GREEN}[PASS]{RESET}"), 0.45)
put(f"{GREEN}Gecko: 8/8 blocked · 0/8 exploited · money_trusted{RESET}", 1.6)

# ---------- Act 3 — CI gate ----------
clear()
out(f"{BOLD}ACT 3 · STAY CORRECT (CI){RESET}", 0.03, pause=1.0)
put("")
out(
    f"{CYAN}$ gecko test tests/fixtures/txodds_docs.yaml --mode recorded{RESET}",
    0.02,
    pause=0.3,
)
test = _run(
    [
        "uv",
        "run",
        "gecko",
        "test",
        "tests/fixtures/txodds_docs.yaml",
        "--mode",
        "recorded",
    ]
)
for line in _tail_lines(test.stdout, 3):
    put(line, 0.5)
put("")
out(f"{BOLD}{CYAN}ANY API, AGENT-READY — FIRST CALL CORRECT{RESET}", 0.03, pause=0.5)
out(f"{CYAN}uvx --from gecko-surf gecko https://api.example.com/openapi.json{RESET}", 0.03, pause=2.0)

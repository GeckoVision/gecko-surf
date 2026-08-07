#!/usr/bin/env python3
"""Ten real intents through the LIVE hosted Orquestra surface. No fixtures, no fakes.

Every row below is one `tools/call find_start` against
https://mcp.geckovision.tech/orquestra/mcp — the same door a chat client uses. What it
writes is what the server returned during the run, including the refusals.

The refusals are the point, not an embarrassment. Four of these ten are written to be
un-routable on purpose: a tool that answers everything tells you nothing about when to
trust it. A sweep where all ten "pass" would be the manufactured version.

    uv run python demo/kit/orquestra_sweep.py            # human table
    uv run python demo/kit/orquestra_sweep.py --json     # feeds the screenplay

Writes demo/kit/orquestra_sweep.json so a video screenplay can render measured numbers
instead of typing them in by hand — the same rule the four-programs demo follows.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

HOST = "https://mcp.geckovision.tech"
SURFACE = "orquestra"
OUT = Path(__file__).with_name("orquestra_sweep.json")

#: (intent, what we expect). ``None`` means we expect a refusal — no start, only guesses.
#: Written before the run, not after, so a surprise stays a surprise.
INTENTS: tuple[tuple[str, str | None], ...] = (
    ("buy this token on pump.fun", "pumpfun/buy"),
    ("sell my pump.fun position", "pumpfun/sell"),
    ("swap wsol to usdc on meteora", "meteora/swap"),
    ("route a swap out of hyUSD", "jupiter/route"),
    ("claim my ore rewards", "ore/claimOre"),
    ("fund a metadao launch", "metadao_ico/fund"),
    # Deliberately un-routable: no program named, no two distinguishing terms.
    ("get me out of hyUSD into USDC", None),
    ("move my money somewhere safer", None),
    ("do the thing with the tokens", None),
    ("what is my balance", None),
)


@dataclass(frozen=True)
class Row:
    intent: str
    expected: str | None
    routed: str | None
    score: int | None
    accounts: int | None
    provenance: dict[str, int]
    matched: bool


def _post(payload: dict, session: str | None = None) -> tuple[dict, str | None]:
    request = urllib.request.Request(
        f"{HOST}/{SURFACE}/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **({"mcp-session-id": session} if session else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        sid = response.headers.get("mcp-session-id") or session
        body = response.read().decode()
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:]), sid
    return {}, sid


def _open_session() -> str:
    _, sid = _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "gecko-sweep", "version": "1"},
            },
        }
    )
    if not sid:
        raise RuntimeError("server returned no session id")
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    return sid


def _find_start(intent: str, session: str) -> dict:
    reply, _ = _post(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "find_start", "arguments": {"intent": intent}},
        },
        session,
    )
    content = (reply.get("result") or {}).get("content") or []
    return json.loads(content[0]["text"]) if content else {}


def _row(intent: str, expected: str | None, payload: dict) -> Row:
    starts = [s for s in payload.get("starts", []) if s.get("instruction")]
    # A card the server marked GUESS is not a start, whatever its score.
    real = [s for s in starts if "GUESS" not in (s.get("note") or "")]
    top = real[0] if real else None

    routed = f"{top['program']}/{top['instruction']}" if top else None
    plan = (top or {}).get("derive_plan") or []
    provenance: dict[str, int] = {}
    for step in plan:
        tag = step.get("provenance", "unknown")
        provenance[tag] = provenance.get(tag, 0) + 1

    return Row(
        intent=intent,
        expected=expected,
        routed=routed,
        score=(top or {}).get("score"),
        accounts=len(plan) or None,
        provenance=provenance,
        matched=routed == expected,
    )


def main(argv: list[str]) -> int:
    session = _open_session()
    rows: list[Row] = []
    for intent, expected in INTENTS:
        try:
            rows.append(_row(intent, expected, _find_start(intent, session)))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            # Recorded, never silently skipped — a sweep that hides its own failures is
            # exactly the manufactured demo this file exists to avoid.
            rows.append(
                Row(
                    intent,
                    expected,
                    f"ERROR: {type(exc).__name__}",
                    None,
                    None,
                    {},
                    False,
                )
            )

    OUT.write_text(json.dumps([asdict(r) for r in rows], indent=1), "utf-8")

    if "--json" in argv:
        print(json.dumps([asdict(r) for r in rows], indent=1))
        return 0

    routed = sum(1 for r in rows if r.routed and not str(r.routed).startswith("ERROR"))
    refused = sum(1 for r in rows if r.routed is None)
    correct = sum(1 for r in rows if r.matched)

    print(f"\n  {len(rows)} intents, live against {HOST}/{SURFACE}\n")
    for r in rows:
        verdict = "ok " if r.matched else "!! "
        shown = r.routed or "no start (guesses only)"
        score = f"  score {r.score}" if r.score is not None else ""
        print(f"  {verdict}{r.intent:<34} → {shown}{score}")
        if r.provenance:
            tags = " · ".join(f"{n} {k}" for k, n in sorted(r.provenance.items()))
            print(f"      {r.accounts} accounts: {tags}")
    print(
        f"\n  routed {routed} · refused {refused} · matched expectation {correct}/{len(rows)}"
    )
    print(f"  wrote {OUT.name}\n")
    return 0 if correct == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

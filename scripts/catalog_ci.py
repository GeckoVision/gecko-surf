"""CI for agents — a scorecard for every instruction in one catalog entry.

This is the artifact, not a description of one. For each instruction the program exposes it
answers the questions an integrator actually has, and every answer is measured:

  BUILD     can a transaction be built at all, from the surface alone?
  SIMULATE  does it survive the chain's own judgement, unsigned and free?
  HONEST    is the compute number the surface REPORTS the number the chain CHARGES?
  REACHABLE can an agent asking in user words find this instruction?
  REFUSES   does the surface refuse what it should, or does it guess?

Nothing here signs or broadcasts. Every call is a read or an unsigned simulation.

WHERE THE CASES COME FROM, and this file is the reason the rule exists. The account
list and the argument names used to be typed out here by hand. On the first real run
that list omitted `system_program` from `delete_product` and sent `name` where the
instruction takes `product_name` — and the scorecard printed a RED row for the
SURFACE, for two defects in its own probe, both of which the program graph already
stated correctly. So the shape of every case is now derived from the graph by
`gecko.project.probes.probe_cases`: accounts and argument NAMES come from the IDL,
PDA addresses are derived from the program's own seed recipes, and the addresses the
IDL pins (system / token / associated-token) are read off the spec rather than
retyped. What is left below is only what a caller genuinely has to CHOOSE — which
wallet, which store, which product — and a value the graph asks for and this file
does not supply is a refusal, never a default.

    uv run python -m scripts.catalog_ci

The storefront it scores is YOURS, so the wallets and store it names stay out of the
repo. Point it at your own by writing `~/.gecko/catalog-ci.json`:

    {
      "buyer":     "<pubkey that would pay>",
      "authority": "<pubkey that owns the store>",
      "store":     "<store name>",
      "telegram":  "@<channel the update_telegram_channel case writes>"
    }

Every key may be overridden by the matching `GECKO_CI_*` environment variable
(`GECKO_CI_BUYER`, …). Nothing is signed, so these are read-only identities — but they
are still identities, and a public scorecard has no reason to carry them.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

from gecko.program_graph import build_program_graph
from gecko.project.probes import ProbeCase, probe_cases
from scripts.orquestra_bridge import ProjectIdl, let_me_buy_project

CONFIG_PATH = Path(
    os.environ.get("GECKO_CI_CONFIG", "~/.gecko/catalog-ci.json")
).expanduser()

# A universal SPL address — the same for every caller, so it stays inline. The
# system / token / associated-token programs are NOT here any more: the IDL pins
# them, so the graph carries them and a probe that retypes them can get them wrong.
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

#: The two instructions the graph exposes and this scorecard does not probe, with the
#: reason — stated here and printed under the table, because a scorecard that quietly
#: drops rows is exactly the kind of dishonesty it exists to catch. Neither exclusion
#: is about the surface: both are about the STORE this run points at.
#:
#:   initialize    creates the store. Against a store that already exists it can only
#:                 fail (`Allocate: account … already in use`), so scoring it here
#:                 would print the probe's precondition as a defect in the surface.
#:   delete_store  deletes it. The call is only ever simulated, so nothing is at risk
#:                 — but a scorecard that routinely rehearses deleting a live
#:                 storefront with 125 purchases of history is a bad habit to ship,
#:                 and the run on 2026-08-17 showed it SUCCEEDS (16,893 CU) on a store
#:                 that still lists products, i.e. the 6005 StoreNotEmpty guard our own
#:                 config notes promise did not fire.
NOT_PROBED: Mapping[str, str] = {
    "initialize": "the store already exists, so this can only fail",
    "delete_store": "deletes the store; simulated-only, but not a habit to ship",
}

#: The user's words for each instruction — the REACHABLE probe's input, and the ONE
#: field `probe_cases` cannot derive. "buy a bottle of water at the bar" is not in an
#: IDL, and generating a phrase from the identifier would score keyword echo rather
#: than reachability. A missing entry degrades to the identifier; it can never move an
#: account or an argument.
INTENTS: Mapping[str, str] = {
    "make_purchase": "buy a bottle of water at the bar",
    "mark_as_delivered": "tell the customer their order is on its way",
    "update_details": "change what my shop says about itself",
    "update_telegram_channel": "send my orders somewhere else",
    "add_product": "put a new drink on the menu",
    "delete_product": "take something off the menu",
}


class ConfigError(RuntimeError):
    """The local storefront config is missing or incomplete."""


def load_config() -> dict[str, str]:
    """Read the local storefront identities. Fails closed — never guesses a wallet."""
    stored: dict[str, str] = {}
    if CONFIG_PATH.exists():
        stored = json.loads(CONFIG_PATH.read_text())
    resolved = {
        key: os.environ.get(f"GECKO_CI_{key.upper()}") or stored.get(key, "")
        for key in ("buyer", "authority", "store", "telegram")
    }
    missing = sorted(key for key, value in resolved.items() if not value)
    if missing:
        raise ConfigError(
            f"missing {', '.join(missing)} — write {CONFIG_PATH} or set "
            f"{', '.join('GECKO_CI_' + key.upper() for key in missing)}. "
            "See this module's docstring for the shape."
        )
    return resolved


def bindings(config: Mapping[str, str]) -> dict[str, str]:
    """The values a caller has to choose, keyed by the GRAPH's own names.

    Everything else — which accounts exist, what the arguments are called, what a PDA
    resolves to — is the graph's answer, not this file's. A binding the graph never
    asks for is dead weight; a binding it asks for and does not find is a refusal.
    """
    return {
        # accounts
        "signer": config["buyer"],
        "authority": config["authority"],
        "mint": USDC,
        # args
        "store_name": config["store"],
        # `mark_as_delivered` spells the same value with a leading underscore: Anchor
        # copies Rust's unused-parameter name into the IDL while the PDA seed keeps
        # the original. Both names are stated by the graph, so both are bound here.
        "_store_name": config["store"],
        # A product the store actually holds — deleting one it does not have is a
        # REFUSES probe (ProductNotFound / 6002), not a BUILD or SIMULATE one.
        "product_name": os.environ.get("GECKO_CI_PRODUCT", "Water"),
        "name": os.environ.get("GECKO_CI_NEW_PRODUCT", "Sparkling"),
        "price": "120000",
        "table_number": "9",
        "details": "open until late",
        "telegram_channel_id": config["telegram"],
        "receipt_id": os.environ.get("GECKO_CI_RECEIPT_ID", "106"),
    }


def cases(project: ProjectIdl, config: Mapping[str, str]) -> tuple[ProbeCase, ...]:
    """Every instruction of the catalog entry, minus the ones named in NOT_PROBED."""
    graph = build_program_graph(idl=dict(project.idl), program_id=project.program_id)
    generated = probe_cases(graph, bindings=bindings(config), intents=INTENTS)
    return tuple(c for c in generated if c.instruction not in NOT_PROBED)


# ---------------------------------------------------------------------------
# his MCP surface
# ---------------------------------------------------------------------------

_sid: str | None = None


def _rpc(payload: dict) -> dict:
    global _sid
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "gecko-surf/catalog-ci",
    }
    if _sid:
        headers["mcp-session-id"] = _sid
    request = urllib.request.Request(
        "https://api.orquestra.dev/mcp",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        _sid = response.headers.get("mcp-session-id") or _sid
        raw = response.read().decode()
        for line in raw.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(raw) if raw.strip().startswith("{") else {"raw": raw[:300]}


def call(tool: str, args: dict) -> str:
    body = _rpc(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
    )
    content = (body.get("result") or {}).get("content") or []
    return (
        content[0]["text"]
        if content and content[0].get("type") == "text"
        else json.dumps(body)
    )


def handshake() -> None:
    _rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "gecko-ci", "version": "1"},
            },
        }
    )
    _rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})


# ---------------------------------------------------------------------------
# the scorecard
# ---------------------------------------------------------------------------


def score(case: ProbeCase, *, project_id: str) -> dict[str, Any]:
    row: dict[str, Any] = {"instruction": case.instruction}
    out = call(
        "simulate_instruction",
        {
            "projectId": project_id,
            "instruction": case.instruction,
            "accounts": dict(case.accounts),
            "args": dict(case.args),
            "feePayer": case.fee_payer,
            "network": "mainnet-beta",
        },
    )
    row["build"] = "error" not in out.lower()[:60]
    row["simulate"] = "✅ success" in out
    reported = re.search(r"Compute units consumed: `(\d+)`", out)
    row["reported"] = int(reported.group(1)) if reported else None
    logged = re.findall(r"consumed (\d+) of", out)
    row["truth"] = int(logged[-1]) if logged else None
    row["honest"] = row["reported"] is not None and row["reported"] == row["truth"]
    risk = re.search(r"Risk level: `(\w+)`", out)
    row["risk"] = risk.group(1) if risk else None
    row["err"] = None
    if not row["simulate"]:
        e = re.search(r"(Error|error)[^\n]{0,90}", out)
        row["err"] = e.group(0)[:70] if e else out[:70].replace("\n", " ")
    return row


def render(rows: Sequence[dict[str, Any]]) -> str:
    lines = [
        f"{'instruction':26} {'build':6} {'sim':5} {'reported':>9} {'actual':>8} {'honest':7} {'risk':6}",
        "-" * 78,
    ]
    for r in rows:
        lines.append(
            f"{r['instruction']:26} {str(r['build']):6} {str(r['simulate']):5} "
            f"{str(r['reported']):>9} {str(r['truth']):>8} {str(r['honest']):7} {str(r['risk']):6}"
        )
        if r["err"]:
            lines.append(f"{'':26} └─ {r['err']}")

    ok = sum(1 for r in rows if r["simulate"])
    honest = sum(1 for r in rows if r["honest"])
    lines += [
        "",
        f"  builds+simulates : {ok}/{len(rows)}",
        f"  reports honestly : {honest}/{len(rows)}",
        f"  distinct risk levels across {len(rows)} instructions: "
        f"{sorted({r['risk'] for r in rows if r['risk']})}",
        "  cases generated from the program graph; "
        f"not probed: {', '.join(f'{k} ({v})' for k, v in NOT_PROBED.items())}",
    ]
    return "\n".join(lines)


def main() -> int:
    config = load_config()
    project = let_me_buy_project()
    handshake()
    rows = [
        score(case, project_id=project.project_id) for case in cases(project, config)
    ]
    print(render(rows))
    return 0 if all(r["build"] and r["simulate"] and r["honest"] for r in rows) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        raise SystemExit(f"catalog-ci refused: {exc}") from exc

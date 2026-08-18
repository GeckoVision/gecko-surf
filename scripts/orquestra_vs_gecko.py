"""One goal, two paths: Orquestra alone, and Gecko composed on top of it.

    uv run python -m scripts.orquestra_vs_gecko

THE GOAL, held identical on both sides: *contribute 0.10 USDC to the DEATON launch.*
Both paths read the same catalogue, the same IDL and the same chain. The only difference
is who derives the accounts.

PATH A IS NOT A STRAWMAN. It is the sequence a competent agent runs with only the
catalogue's own MCP: find the program, list its instructions, read the PDA seeds, derive
them, build. Each step is the documented next one, and the run stops where the tools
actually stop it — not where it would be convenient for us.

Nothing here signs or broadcasts. Path B ends at unsigned bytes plus a simulation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field

from gecko.mcp_client import McpClient, McpToolError
from gecko.orquestra_build import orquestra_seams
from gecko.prepare_instruction import prepare_instruction_result
from gecko.rpc import default_rpc_call

PROGRAM_ID = "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm"
LIVE_LAUNCH = "9nFKKFBEVW4njBtyJkcvngEkmT3qVXm4fgGGRBLbqH65"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
GOAL = "contribute 0.10 USDC to the DEATON launch"


@dataclass
class Path:
    name: str
    steps: list[tuple[str, str]] = field(default_factory=list)
    transaction: str | None = None
    compute_units: int | None = None
    stopped_because: str | None = None

    def step(self, call: str, outcome: str) -> None:
        self.steps.append((call, outcome))


def path_a(client: McpClient) -> Path:
    """Only the catalogue's MCP — the tools an agent has without Gecko."""
    p = Path("A — Orquestra alone")

    text = client.call_tool("search_programs", {"programId": PROGRAM_ID})
    project_id = text.split("projectId: `")[1].split("`")[0]
    p.step("search_programs", f"projectId {project_id[:8]}…")

    listing = client.call_tool("list_instructions", {"projectId": project_id})
    p.step(
        "list_instructions", f"{listing.count('contribute')} mention(s) of contribute"
    )

    pdas = client.call_tool("list_pda_accounts", {"projectId": project_id})
    launch_lines = [
        line.strip()
        for line in pdas.splitlines()
        if "launch." in line or "params.launch_id" in line
    ]
    p.step("list_pda_accounts", f"{len(launch_lines)} seed line(s) for the launch PDA")

    # The step everything hangs on. `contribute` needs `launch`, and the caller has
    # neither its admin nor its id — they live inside the account being derived.
    try:
        derived = client.call_tool(
            "derive_pda",
            {
                "projectId": project_id,
                "instruction": "contribute",
                "account": "launch",
                "seedValues": {},
            },
        )
        address = next(
            (
                ln.split("`")[1]
                for ln in derived.splitlines()
                if ln.startswith("Address:")
            ),
            None,
        )
        p.step("derive_pda(launch)", f"{address}")
        if address != LIVE_LAUNCH:
            p.stopped_because = (
                f"derived {address}, which is not the live launch — a valid address "
                "for an account that does not exist"
            )
    except McpToolError as exc:
        first = str(exc).splitlines()[0]
        p.step("derive_pda(launch)", f"REFUSED — {first[:70]}")
        p.stopped_because = (
            "the launch PDA cannot be derived: its seeds are fields of the account "
            "being addressed, and the caller does not hold it"
        )
    return p


def path_b(buyer: str, rpc: str) -> Path:
    """Gecko composed ON TOP of the same catalogue — one call from the agent's side."""
    p = Path("B — Gecko + Orquestra")

    launch = default_rpc_call(
        rpc, "getAccountInfo", [LIVE_LAUNCH, {"encoding": "base64"}]
    )
    import base64
    import struct

    from gecko.pda import b58_encode

    data = base64.b64decode(launch["result"]["value"]["data"][0])
    launch_id = struct.unpack_from("<Q", data, 8)[0]
    admin = b58_encode(data[16:48])
    p.step("getAccountInfo(launch)", f"launch_id={launch_id}, admin {admin[:8]}…")

    idl_fetch, build_call = orquestra_seams()
    from scripts.jurassic_contribute import associated_token_account

    result = prepare_instruction_result(
        {
            "program_id": PROGRAM_ID,
            "instruction": "contribute",
            "payer": buyer,
            "values": {
                "contributor": buyer,
                "admin": admin,
                "launch_id": launch_id,
                "params.launch_id": launch_id,
                "payment_mint": USDC,
                "contributor_payment_account": associated_token_account(buyer, USDC),
                "payment_vault": associated_token_account(LIVE_LAUNCH, USDC),
                "requested_amount": 100_000,
                "min_accepted_amount": 100_000,
            },
        },
        idl_fetch=idl_fetch,
        build_call=build_call,
        rpc_call=default_rpc_call,
        rpc_url=rpc,
    )
    if result["refused"]:
        p.step("prepare_instruction", f"REFUSED — {result['code']}")
        p.stopped_because = result["reason"]
        return p

    derived = [
        o["account"] for o in result["account_origins"] if o["origin"] == "derived"
    ]
    p.step(
        "prepare_instruction",
        f"{len(derived)} account(s) derived ({', '.join(derived)}), "
        "bytes built by Orquestra's own builder",
    )
    p.transaction = result["transaction_base64"]
    p.compute_units = result["simulation"]["compute_units"]
    p.step("simulateTransaction", f"err=None, {p.compute_units:,} CU")
    return p


def render(p: Path) -> None:
    print(f"\n{p.name}")
    print("-" * len(p.name))
    for i, (call, outcome) in enumerate(p.steps, 1):
        print(f"  {i}. {call:26} {outcome}")
    if p.transaction:
        print(f"  => UNSIGNED transaction, {len(p.transaction)} base64 chars")
        print(f"     simulates on mainnet at {p.compute_units:,} CU")
    if p.stopped_because:
        print(f"  => STOPPED: {p.stopped_because}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default=DEFAULT_RPC)
    parser.add_argument("--mcp-url", default="https://api.orquestra.dev/mcp")
    args = parser.parse_args()

    buyer = json.load(open(os.path.expanduser("~/.gecko/catalog-ci.json")))["buyer"]
    print(f'GOAL: "{GOAL}"')
    print(f"both paths, same catalogue and same chain, buyer {buyer[:8]}…")

    a = path_a(McpClient(args.mcp_url))
    render(a)
    time.sleep(1)
    b = path_b(buyer, args.rpc)
    render(b)

    print("\n" + "=" * 66)
    print(f"{'':26} {'path A':>16} {'path B':>16}")
    print(f"{'tool calls':26} {len(a.steps):>16} {len(b.steps):>16}")
    print(
        f"{'reached a transaction':26} {'no' if not a.transaction else 'yes':>16} "
        f"{'yes' if b.transaction else 'no':>16}"
    )
    print(
        f"{'verified against chain':26} {'—':>16} "
        f"{(str(b.compute_units) + ' CU') if b.compute_units else '—':>16}"
    )
    print(
        "\nPath B calls Orquestra for the catalogue, the IDL and the BYTES. The only "
        "thing\nit does not delegate is deriving the accounts — which is the only thing "
        "path A\ncould not do."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

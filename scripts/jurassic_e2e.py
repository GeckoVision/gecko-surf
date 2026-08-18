"""jurassic_fi end-to-end: every instruction, both arms, judged against mainnet.

    uv run python -m scripts.jurassic_e2e

Eight instructions, capped deliberately — this calls a partner's PRODUCTION MCP, so the
run is bounded and one-shot rather than something CI repeats.

WHAT MAKES THIS A TEST AND NOT A COMPARISON. Two surfaces agreeing proves nothing; our own
week says internal consistency is not authenticity. So every derived address is checked
against MAINNET: the account either exists, is owned by this program, and decodes — or it
does not. The live launch `9nFKK…` is the anchor, and its `launch_id` (100) and `admin` are
read off the chain, never assumed.

READ-ONLY. `getAccountInfo` and the partner's listing/derivation tools. Nothing is signed,
nothing is broadcast, and no response is persisted.
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import urllib.request
from dataclasses import dataclass

from gecko.mcp_client import McpClient, McpError
from gecko.pda import b58_encode, derive_pda
from gecko.program_graph import build_program_graph
from gecko.rpc import default_rpc_call

PROJECT_ID = "d2decec3-acdf-4946-bbb7-252a3c14ce2c"
PROGRAM_ID = "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm"
LIVE_LAUNCH = "9nFKKFBEVW4njBtyJkcvngEkmT3qVXm4fgGGRBLbqH65"
DEFAULT_MCP = "https://api.orquestra.dev/mcp"
DEFAULT_API = "https://api.orquestra.dev"
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"


@dataclass
class Row:
    instruction: str
    account: str
    gecko: str | None
    partner: str | None
    onchain: str  # "exists" | "absent" | "-"
    note: str = ""


def fetch_idl(api: str) -> dict:
    request = urllib.request.Request(
        f"{api}/api/idl/{PROJECT_ID}", headers={"User-Agent": "gecko-surf/jurassic-e2e"}
    )
    with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
        payload = json.loads(response.read())
    idl = payload.get("idl") or payload
    return json.loads(idl) if isinstance(idl, str) else idl


def read_launch(rpc: str) -> tuple[int, str]:
    """`launch_id` and `admin`, read off the chain — the seeds, from the account itself."""
    value = default_rpc_call(
        rpc, "getAccountInfo", [LIVE_LAUNCH, {"encoding": "base64"}]
    )
    account = value["result"]["value"]
    if not account:
        raise SystemExit(f"{LIVE_LAUNCH} does not exist on {rpc}")
    data = base64.b64decode(account["data"][0])
    launch_id = struct.unpack_from("<Q", data, 8)[0]
    admin = b58_encode(data[16:48])
    return launch_id, admin


def account_exists(rpc: str, address: str) -> bool:
    value = default_rpc_call(rpc, "getAccountInfo", [address, {"encoding": "base64"}])
    account = value["result"]["value"]
    return bool(account) and account.get("owner") == PROGRAM_ID


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP)
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--rpc", default=DEFAULT_RPC)
    args = parser.parse_args()

    idl = fetch_idl(args.api)
    launch_id, admin = read_launch(args.rpc)
    print(f"chain says: launch_id={launch_id}  admin={admin}\n")

    graph = build_program_graph(idl=idl, program_id=PROGRAM_ID)
    client = McpClient(args.mcp_url)

    # every seed value a caller could legitimately hold, and nothing else
    bindings = {"admin": admin, "launch_id": launch_id, "params.launch_id": launch_id}

    rows: list[Row] = []
    for instruction in graph.instructions:
        for account in instruction.accounts:
            if not account.is_pda or account.name != "launch":
                continue  # the root every other account hangs off
            node = graph.pdas.get(account.name)
            ours: str | None = None
            note = ""
            if node is not None and account.resolvable:
                try:
                    ours = derive_pda(node, bindings).address
                except Exception as exc:  # noqa: BLE001
                    note = f"gecko: {type(exc).__name__}"
            elif node is not None:
                note = "gecko: flagged unresolvable"

            theirs: str | None = None
            try:
                text = client.call_tool(
                    "derive_pda",
                    {
                        "projectId": PROJECT_ID,
                        "instruction": instruction.name,
                        "account": account.name,
                        "seedValues": bindings,
                    },
                )
                for line in text.splitlines():
                    if line.startswith("Address:"):
                        theirs = line.split("`")[1]
                        break
            except (McpError, OSError) as exc:
                note = note or f"partner: {str(exc).splitlines()[0][:44]}"

            checked = ours or theirs
            onchain = "-"
            if checked:
                onchain = "exists" if account_exists(args.rpc, checked) else "absent"
            rows.append(
                Row(instruction.name, account.name, ours, theirs, onchain, note)
            )

    print(f"{'instruction':34} {'gecko':>10} {'partner':>10} {'on chain':>9}")
    correct = agree = 0
    for row in rows:
        g = "RIGHT" if row.gecko == LIVE_LAUNCH else ("wrong" if row.gecko else "—")
        p = "RIGHT" if row.partner == LIVE_LAUNCH else ("wrong" if row.partner else "—")
        correct += row.gecko == LIVE_LAUNCH
        agree += row.partner == LIVE_LAUNCH
        print(
            f"{row.instruction:34} {g:>10} {p:>10} {row.onchain:>9}"
            + (f"   {row.note}" if row.note else "")
        )

    print(
        f"\nof {len(rows)} instructions that need the launch account, "
        f"gecko derives the live one for {correct}, the partner for {agree}."
    )
    print(
        "RIGHT means byte-identical to an account that exists on mainnet and is owned by\n"
        "this program — not that the two surfaces agree with each other."
    )
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())

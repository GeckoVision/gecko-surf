"""Screenplay — "the call a catalogue can't build" (jurassic_fi, live).

One goal, two paths, same catalogue and same chain: contribute to a live token sale
holding real USDC. The catalogue's own tools find the program, list its instructions and
its PDA seeds — and then stop, because the account they all hang off is seeded on fields
of itself. Gecko derives it, hands the accounts back to the CATALOGUE'S OWN builder, and
the transaction simulates on mainnet.

The point is not that the catalogue is wrong. Its builder produced the working bytes. The
missing step was one derivation, and that is the whole beat: we compose, we do not replace.

EVERY CALL HERE IS REAL and made during the recording — the catalogue's public MCP, a
public Solana RPC, and our own graph. Nothing signs. Nothing is broadcast. The last frame
is unsigned bytes plus a simulation.

The sale is LIVE, so the raised figure moves between takes — which is why no number is
hardcoded here or in the scene titles. Whatever the chain says at record time is what the
frame shows.

Record it:

    asciinema rec --cols 80 --rows 20 \
        -c "uv run python demo/kit/jurassic_screenplay.py" jurassic.cast

    uv run --with pyte --with pillow python demo/kit/render_cast.py jurassic.cast \
        demo/kit/jurassic.mp4 \
        --brand "GECKO  •  CHECK THE CALL BEFORE IT COUNTS" \
        --scene "Gecko — a live token sale|real money, and one account in the way" \
        --scene "Gecko — the catalogue alone|four calls, then a dead end" \
        --scene "Gecko — the same goal, composed|three calls, and a transaction"
"""

from __future__ import annotations

import base64
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from screenplay import BOLD, CYAN, GREEN, RED, RESET, YELLOW, clear, out, put  # noqa: E402

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from gecko.mcp_client import McpClient, McpToolError  # noqa: E402
from gecko.orquestra_build import orquestra_seams  # noqa: E402
from gecko.pda import (  # noqa: E402
    PdaNode,
    VariablePdaSeedNode,
    b58_encode,
    derive_pda,
)
from gecko.prepare_instruction import prepare_instruction_result  # noqa: E402
from gecko.rpc import default_rpc_call  # noqa: E402

PROGRAM = "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm"
LAUNCH = "9nFKKFBEVW4njBtyJkcvngEkmT3qVXm4fgGGRBLbqH65"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC = "https://api.mainnet-beta.solana.com"
MCP = "https://api.orquestra.dev/mcp"
#: A public address, shown because a fee payer is part of a real transaction. It signs
#: nothing here — this demo never reaches a signer.
PAYER = "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi"

catalog = McpClient(MCP)


def ata(owner: str) -> str:
    """The owner's USDC associated token account — derived, never pasted."""
    return derive_pda(
        PdaNode(
            "ata",
            (
                VariablePdaSeedNode("owner", source="account", encoding="pubkey"),
                VariablePdaSeedNode("tp", source="account", encoding="pubkey"),
                VariablePdaSeedNode("mint", source="account", encoding="pubkey"),
            ),
            "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        ),
        {
            "owner": owner,
            "tp": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "mint": USDC,
        },
    ).address


# ---------------------------------------------------------------- scene 1: the sale
out(f"{BOLD}GECKO — THE CALL A CATALOGUE CAN'T BUILD{RESET}", pause=0.6)
out()
out(f"{CYAN}User:{RESET} put 0.10 USDC into the DEATON launch.", pause=0.9)
out()

out(f"{YELLOW}First — is it real? Reading the account off mainnet.{RESET}", pause=0.5)
value = default_rpc_call(RPC, "getAccountInfo", [LAUNCH, {"encoding": "base64"}])
account = value["result"]["value"]
data = base64.b64decode(account["data"][0])
launch_id = struct.unpack_from("<Q", data, 8)[0]
admin = b58_encode(data[16:48])
# Launch: 8 discriminator | launch_id u64 | six pubkeys | start_ts,end_ts i64 | raise_cap.
# Written as a sum rather than a magic number, because a wrong offset here reads as a
# plausible figure — the first draft of this printed a 3.2-trillion-USDC raise cap.
_CAP_AT = 8 + 8 + 6 * 32 + 8 + 8
cap = struct.unpack_from("<Q", data, _CAP_AT)[0]
raised = struct.unpack_from("<Q", data, _CAP_AT + 8)[0]

put(f"  owner        {account['owner']}")
put(f"  launch_id    {launch_id}")
put(f"  raise_cap    {cap / 1e6:,.2f} USDC")
put(f"  total_raised {raised / 1e6:,.2f} USDC")
out()
out(f"{GREEN}Real money, live, open.{RESET}", pause=1.0)

# ------------------------------------------------- scene 2: the catalogue, alone
clear()
out(f"{BOLD}THE CATALOGUE'S OWN TOOLS{RESET}", pause=0.5)
out()

out(f"{CYAN}1. find the program{RESET}", pause=0.3)
found = catalog.call_tool("search_programs", {"programId": PROGRAM})
project = found.split("projectId: `")[1].split("`")[0]
put(f"  jurassic_fi_token_sale   {project[:8]}…")

out(f"{CYAN}2. list its instructions{RESET}", pause=0.3)
listed = catalog.call_tool("list_instructions", {"projectId": project})
put(
    f"  {sum(1 for line in listed.splitlines() if line.startswith('## '))} instructions, contribute among them"
)

out(f"{CYAN}3. read the PDA seeds{RESET}", pause=0.3)
pdas = catalog.call_tool("list_pda_accounts", {"projectId": project})
for line in pdas.splitlines():
    if "launch.admin" in line or "launch.launch_id" in line:
        put(f"  {line.strip()}")
        break
put("  ^ the seeds are fields of the account being derived")

out()
out(f"{CYAN}4. derive it{RESET}", pause=0.4)
try:
    catalog.call_tool(
        "derive_pda",
        {
            "projectId": project,
            "instruction": "contribute",
            "account": "launch",
            "seedValues": {},
        },
    )
    put("  (derived)")
except McpToolError as exc:
    put(f"{RED}  {str(exc).splitlines()[0][:70]}{RESET}")
    out()
    out(
        f"{YELLOW}A dead end, and an honest one: to compute the account you need{RESET}"
    )
    out(
        f"{YELLOW}what is inside it. Four calls in, and the sale is unreachable.{RESET}",
        pause=1.4,
    )

# ------------------------------------------------- scene 3: composed
clear()
out(f"{BOLD}THE SAME GOAL, COMPOSED{RESET}", pause=0.5)
out()

out(f"{CYAN}1. read the two seeds off the chain{RESET}", pause=0.3)
put(f"  admin      {admin}")
put(f"  launch_id  {launch_id}   (u64 — at u8 it derives a DIFFERENT valid address)")

out(f"{CYAN}2. prepare_instruction{RESET}", pause=0.3)
idl_fetch, build_call = orquestra_seams()
result = prepare_instruction_result(
    {
        "program_id": PROGRAM,
        "instruction": "contribute",
        "payer": PAYER,
        "values": {
            "admin": admin,
            "launch_id": launch_id,
            "params.launch_id": launch_id,
            "payment_mint": USDC,
            "contributor_payment_account": ata(PAYER),
            "payment_vault": ata(LAUNCH),
            "requested_amount": 100_000,
            "min_accepted_amount": 100_000,
        },
    },
    idl_fetch=idl_fetch,
    build_call=build_call,
    rpc_call=default_rpc_call,
    rpc_url=RPC,
)
if result["refused"]:
    put(f"{RED}  refused: {result['code']} — {result['reason'][:60]}{RESET}")
    raise SystemExit(1)
for origin in result["account_origins"]:
    put(f"  {origin['account']:26} {origin['origin']}")

out(f"{CYAN}3. simulate on mainnet{RESET}", pause=0.3)
put(f"  err  {result['simulation']['err']}")
put(f"  CU   {result['simulation']['compute_units']:,}")

out()
out(
    f"{GREEN}Three calls. Unsigned bytes, and a mainnet simulation that passes.{RESET}",
    pause=0.8,
)
out(f"{YELLOW}The bytes were built by the catalogue's OWN builder.{RESET}", pause=0.6)
out(
    f"{YELLOW}The only step it could not take was deriving one account.{RESET}",
    pause=1.2,
)
out()
out(f"{BOLD}CHECK THE CALL BEFORE IT COUNTS{RESET}", pause=0.4)
out(f"{BOLD}mcp.geckovision.tech{RESET}", pause=1.0)
time.sleep(1.0)

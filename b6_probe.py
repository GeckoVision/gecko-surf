"""Two questions the fork can settle for free, at tick granularity and at size.

(1) WHERE IS THE REAL sqrt_price CEILING? MAX vs MAX-1 was not a controlled comparison —
near the top of the range one tick spans ~4e24, so both are the SAME TICK and any bound
enforced at tick granularity rejects them identically. Probe at tick granularity instead.

(2) DOES MULTI-ARRAY TRAVERSAL WORK? Traversal correctness is a CONSTRUCTION property —
are the right arrays derived and consumed in the right order — so it needs no real money.
Fund the fork buyer arbitrarily and push size until the swap has to leave array 0.
"""

import os

from gecko.pda import derive_pda
from gecko.pda_testkit import SurfpoolError, SurfpoolFork
from gecko.prepare_instruction import prepare_instruction_result
from gecko.provider_config import load_packaged_provider
from gecko.providers.catalog_surface import orquestra_seams
from gecko.rpc import default_rpc_call
from gecko.sandbox import ephemeral_signer, fund_sol, fund_token, prove_surfnet
from gecko.store_accounts import derive_ata

USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
POOL = "9RqDTfwCx2SgxsvKpspQHc38HUo3B6hRd3oR9JR966Ps"
VAULT_A = "6j9UtMmzmWuLu45XXmdUXN3NJBdiicxxoBEex8jUs3j6"
VAULT_B = "5Sokmb48nt8aH8TnnkrAcVea4SdRqGU3qTxhRFvTHJyn"
LIVE = 18447632430077141684
PORT = 8951

_, apis = load_packaged_provider("orquestra")
recipe = dict(apis["whirlpool"].program.pdas)["tick_array"]
UP = [
    derive_pda(recipe, {"whirlpool": POOL, "start_tick_index": str(s)}).address
    for s in (0, 88, 176)
]

PROBES = [
    ("MAX (tick 443636)", 79226673515401279992447579055),
    ("A: tick 443635", 79222712478800779441888593669),
    ("B: tick 400000", 8940773544377188876727933131),
    ("live x 2", LIVE * 2),
]
SIZES = [
    ("0.1 USDC", 100_000),
    ("10k USDC", 10_000_000_000),
    ("1M USDC", 1_000_000_000_000),
    ("50M USDC", 50_000_000_000_000),
]


def run(url, buyer, ata_a, ata_b, seams, *, amount, limit):
    idl_fetch, build_call = seams
    return prepare_instruction_result(
        {
            "program_id": PROGRAM,
            "instruction": "swap_v2",
            "payer": buyer,
            "values": {
                "whirlpool": POOL,
                "token_mint_a": USDG,
                "token_mint_b": USDC,
                "token_program_a": TOKEN_2022,
                "token_program_b": CLASSIC,
                "token_owner_account_a": ata_a,
                "token_owner_account_b": ata_b,
                "token_vault_a": VAULT_A,
                "token_vault_b": VAULT_B,
                "tick_array_0": UP[0],
                "tick_array_1": UP[1],
                "tick_array_2": UP[2],
                "amount": amount,
                "other_amount_threshold": 0,
                "sqrt_price_limit": limit,
                "amount_specified_is_input": True,
                "a_to_b": False,
                "remaining_accounts_info": None,
            },
        },
        idl_fetch=idl_fetch,
        build_call=build_call,
        rpc_call=default_rpc_call,
        rpc_url=url,
    )


def show(label, result):
    if result.get("refused"):
        code = None
        for line in (result.get("logs") or []):
            if "Error Code:" in line:
                code = line.split("Error Code:")[1].split(".")[0].strip()
        print(f"  {label:22} REFUSED  {code or result.get('error')}")
    else:
        sim = result.get("simulation") or {}
        print(f"  {label:22} CLEAN    {sim.get('compute_units')} CU")


mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
try:
    with SurfpoolFork(mainnet, port=PORT, ws_port=PORT + 1, ready_timeout=240) as _f:
        url = f"http://127.0.0.1:{PORT}"
        proof = prove_surfnet(url)
        buyer = ephemeral_signer(proof).pubkey
        fund_sol(proof, buyer, 10_000_000_000)
        fund_token(proof, buyer, USDC, 100_000_000_000_000, token_program=CLASSIC)
        fund_token(proof, buyer, USDG, 0, token_program=TOKEN_2022)
        ata_a = derive_ata(buyer, USDG, token_program=TOKEN_2022)
        ata_b = derive_ata(buyer, USDC, token_program=CLASSIC)
        seams = orquestra_seams()

        print("1) WHERE IS THE CEILING? (0.1 USDC, b->a, upward arrays)")
        for label, limit in PROBES:
            show(label, run(url, buyer, ata_a, ata_b, seams, amount=100_000, limit=limit))

        print("\n2) DOES TRAVERSAL WORK? (limit = live x 2, upward arrays)")
        for label, amount in SIZES:
            show(label, run(url, buyer, ata_a, ata_b, seams, amount=amount, limit=LIVE * 2))
except SurfpoolError as exc:
    print(f"THE FORK DID NOT RUN — nothing was measured: {exc}")

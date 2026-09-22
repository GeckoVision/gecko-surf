"""Plan an Orca Whirlpool liquidity position: open it, then fund it by token amounts.

Two instructions, two transactions, because the second needs the first's account to exist:

1. ``open_position`` (classic SPL position mint) creates the position PDA, its one-token
   mint and the owner's token account. The mint is a FRESH keypair that signs once and is
   never kept: it has no authority afterwards (the position PDA owns the mint). Classic,
   not ``open_position_with_token_extensions``: a Token-2022 mint that does not exist yet
   has no extension set anyone could read, and the spend gate refuses a Token-2022
   movement whose extensions were never read. A classic mint has none to read.
2. ``increase_liquidity_by_token_amounts_v2`` deposits by token MAXIMA and lets the
   program size the liquidity, so no liquidity arithmetic of ours is on the money path.
   The sqrt-price bounds are the slippage guard: outside them the program reverts.

The range is the smallest in-range window the pool's tick spacing allows: one spacing
below the current tick's floor to two above it. Both tick arrays it touches must already
exist on chain; initialising one costs rent and a separate instruction, and a plan that
needs that says so instead of discovering it as a revert.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

from ..provider_config import load_packaged_provider
from ..rpc import RpcCall, default_rpc_call
from ..spend_policy import AllowedInstruction, SpendPolicy, TokenCap, TokenCaps
from ..store_accounts import derive_ata
from ..whirlpool_venue import decode_whirlpool, tick_array_start, whirlpool_layout
from ..pda import derive_pda
from ..pda_resolve import read_account_owner
from .whirlpool import WHIRLPOOL_PROGRAM, WhirlpoolPlanError

__all__ = [
    "INCREASE_BY_AMOUNTS",
    "complete_increase_data",
    "increase_data",
    "MEMO_PROGRAM",
    "OPEN_POSITION",
    "plan_position",
    "position_spend_policies",
]

OPEN_POSITION = "open_position"
INCREASE_BY_AMOUNTS = "increase_liquidity_by_token_amounts_v2"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"

#: Rent the open transaction creates, bounded from above: the position account, the
#: position mint and the owner's token account are about 0.006 SOL together, refunded on
#: close. The cap is what the gate compares the simulated SOL outflow against.
OPEN_LAMPORTS_CAP = 10_000_000


def _discriminator(idl: Mapping[str, Any], name: str) -> bytes:
    for instruction in idl.get("instructions", []):
        if instruction.get("name") == name and instruction.get("discriminator"):
            return bytes(instruction["discriminator"])
    raise WhirlpoolPlanError(f"the Whirlpool IDL declares no discriminator for {name}")


def plan_position(
    args: Mapping[str, Any],
    *,
    rpc_url: str,
    rpc_call: RpcCall | None = None,
    idl_fetch: Any = None,
) -> dict[str, Any]:
    """The values for both instructions, the range, and the accounts the gate may write.

    ``args``: ``pool``, ``owner``, ``position_mint`` (the fresh key's public half),
    ``position_bump``, ``token_max_a`` and ``token_max_b`` (raw units of each mint), and
    optional ``slippage_bps`` for the sqrt-price bounds (default 100).
    """
    call: RpcCall = rpc_call or default_rpc_call
    pool = str(args["pool"])
    owner = str(args["owner"])
    position_mint = str(args["position_mint"])
    token_max_a = int(args["token_max_a"])
    token_max_b = int(args["token_max_b"])
    slippage_bps = int(args.get("slippage_bps", 100))
    if token_max_a <= 0 or token_max_b <= 0:
        raise WhirlpoolPlanError("both token maxima must be positive")
    if not 0 < slippage_bps < 10_000:
        raise WhirlpoolPlanError(f"slippage_bps {slippage_bps} is not in (0, 10000)")

    if idl_fetch is None:
        from .catalog_surface import orquestra_seams

        idl_fetch, _build = orquestra_seams()
    idl = idl_fetch(WHIRLPOOL_PROGRAM)
    layout = whirlpool_layout(idl)
    _, apis = load_packaged_provider("orquestra")
    program = apis["whirlpool"].program
    if program is None:  # pragma: no cover - the packaged config always carries it
        raise WhirlpoolPlanError("the packaged whirlpool config declares no program")
    tick_recipe = dict(program.pdas)["tick_array"]

    value = (
        call(rpc_url, "getAccountInfo", [pool, {"encoding": "base64"}]).get("result")
        or {}
    ).get("value")
    if not value:
        raise WhirlpoolPlanError(f"no account exists at {pool} on this node")
    if value.get("owner") != WHIRLPOOL_PROGRAM:
        raise WhirlpoolPlanError(f"{pool} is not owned by the Whirlpool program")
    account = decode_whirlpool(base64.b64decode(value["data"][0]), layout)
    if account.tick_current_index is None or account.token_vault_a is None:
        raise WhirlpoolPlanError(
            "this IDL does not declare the pool fields a position needs"
        )

    spacing = int(account.tick_spacing)
    floor = (int(account.tick_current_index) // spacing) * spacing
    tick_lower, tick_upper = floor - spacing, floor + 2 * spacing

    def tick_array_for(tick: int) -> str:
        start = tick_array_start(tick, tick_spacing=spacing)
        return derive_pda(
            tick_recipe, {"whirlpool": pool, "start_tick_index": str(start)}
        ).address

    array_lower, array_upper = tick_array_for(tick_lower), tick_array_for(tick_upper)
    rows = (
        call(
            rpc_url,
            "getMultipleAccounts",
            [
                [array_lower, array_upper],
                {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}},
            ],
        ).get("result")
        or {}
    ).get("value") or [None, None]
    missing = [a for a, row in zip((array_lower, array_upper), rows) if not row]
    if missing:
        raise WhirlpoolPlanError(
            f"tick array(s) {', '.join(missing)} are not initialised; this plan does not "
            "pay to initialise them, so the range is refused rather than reverted"
        )

    mint_a, mint_b = str(account.token_mint_a), str(account.token_mint_b)
    program_a = read_account_owner(mint_a, rpc_url=rpc_url, rpc_call=call)
    program_b = read_account_owner(mint_b, rpc_url=rpc_url, rpc_call=call)
    ata_a = derive_ata(owner, mint_a, token_program=program_a)
    ata_b = derive_ata(owner, mint_b, token_program=program_b)

    live = int(account.sqrt_price)
    min_sqrt = live * (10_000 - slippage_bps) // 10_000
    max_sqrt = live * (10_000 + slippage_bps) // 10_000

    open_values = {
        "whirlpool": pool,
        "position_mint": position_mint,
        "owner": owner,
        "funder": owner,
        "tick_lower_index": tick_lower,
        "tick_upper_index": tick_upper,
        "bumps": {"position_bump": int(args["position_bump"])},
    }
    increase_values = {
        "whirlpool": pool,
        "token_program_a": program_a,
        "token_program_b": program_b,
        "memo_program": MEMO_PROGRAM,
        "position_authority": owner,
        "token_mint_a": mint_a,
        "token_mint_b": mint_b,
        "token_owner_account_a": ata_a,
        "token_owner_account_b": ata_b,
        "token_vault_a": str(account.token_vault_a),
        "token_vault_b": str(account.token_vault_b),
        "tick_array_lower": array_lower,
        "tick_array_upper": array_upper,
        "method": {
            "ByTokenAmounts": {
                "token_max_a": token_max_a,
                "token_max_b": token_max_b,
                "min_sqrt_price": min_sqrt,
                "max_sqrt_price": max_sqrt,
            }
        },
        "remaining_accounts_info": None,
    }
    return {
        "pool": pool,
        "tick_spacing": spacing,
        "tick_current": int(account.tick_current_index),
        "tick_lower": tick_lower,
        "tick_upper": tick_upper,
        "sqrt_price": live,
        "sqrt_bounds": [min_sqrt, max_sqrt],
        "mints": {"a": mint_a, "b": mint_b},
        "token_programs": {"a": program_a, "b": program_b},
        "open_values": open_values,
        "increase_values": increase_values,
        "discriminators": {
            OPEN_POSITION: _discriminator(idl, OPEN_POSITION).hex(),
            INCREASE_BY_AMOUNTS: _discriminator(idl, INCREASE_BY_AMOUNTS).hex(),
        },
    }


def position_spend_policies(
    plan: Mapping[str, Any],
    *,
    open_destinations: frozenset[str],
    increase_destinations: frozenset[str],
    decimals: Mapping[str, int],
    accepted_mints: tuple[Any, ...] = (),
) -> tuple[SpendPolicy, SpendPolicy]:
    """One policy per transaction, each authorising its own instruction and nothing else.

    The open leg moves rent and no token out, so its token caps are authored as NONE and
    its lamport cap is the rent bound. The deposit leg moves the two pool mints out,
    capped at exactly the maxima the human chose, and carries the operator's acceptance
    of any Token-2022 mint among them.
    """
    discriminators = plan["discriminators"]
    compute = AllowedInstruction(
        program_id=COMPUTE_BUDGET_PROGRAM, discriminator=b"\x02"
    )
    compute_price = AllowedInstruction(
        program_id=COMPUTE_BUDGET_PROGRAM, discriminator=b"\x03"
    )
    open_policy = SpendPolicy(
        authorized=True,
        per_transaction_cap_lamports=OPEN_LAMPORTS_CAP,
        hourly_cap_lamports=OPEN_LAMPORTS_CAP,
        daily_cap_lamports=OPEN_LAMPORTS_CAP,
        max_transactions_per_day=2,
        allowed_instructions=frozenset(
            {
                AllowedInstruction(
                    program_id=WHIRLPOOL_PROGRAM,
                    discriminator=bytes.fromhex(discriminators[OPEN_POSITION]),
                ),
                compute,
                compute_price,
            }
        ),
        allowed_destinations=open_destinations,
        token_caps=TokenCaps.none(),
    )
    increase = plan["increase_values"]["method"]["ByTokenAmounts"]
    mints = plan["mints"]
    increase_policy = SpendPolicy(
        authorized=True,
        per_transaction_cap_lamports=100_000,
        hourly_cap_lamports=200_000,
        daily_cap_lamports=200_000,
        max_transactions_per_day=2,
        allowed_instructions=frozenset(
            {
                AllowedInstruction(
                    program_id=WHIRLPOOL_PROGRAM,
                    discriminator=bytes.fromhex(discriminators[INCREASE_BY_AMOUNTS]),
                ),
                compute,
                compute_price,
            }
        ),
        allowed_destinations=increase_destinations,
        token_caps=TokenCaps.of(
            [
                TokenCap(
                    mint=mints[side],
                    decimals=int(decimals[mints[side]]),
                    per_transaction_raw=int(increase[f"token_max_{side}"]),
                    hourly_raw=int(increase[f"token_max_{side}"]),
                    daily_raw=int(increase[f"token_max_{side}"]),
                )
                for side in ("a", "b")
            ]
        ),
        accepted_mints=frozenset(accepted_mints),
    )
    return open_policy, increase_policy


def increase_data(plan: Mapping[str, Any]) -> bytes:
    """The Borsh bytes of ``increase_liquidity_by_token_amounts_v2`` for this plan.

    discriminator, then the ``IncreaseLiquidityMethod`` enum (variant 0 ``ByTokenAmounts``:
    two u64 maxima, two u128 sqrt-price bounds, little-endian), then the ``Option`` of
    ``remaining_accounts_info`` (0 = None). 58 bytes.
    """
    method = plan["increase_values"]["method"]["ByTokenAmounts"]
    return (
        bytes.fromhex(plan["discriminators"][INCREASE_BY_AMOUNTS])
        + b"\x00"
        + int(method["token_max_a"]).to_bytes(8, "little")
        + int(method["token_max_b"]).to_bytes(8, "little")
        + int(method["min_sqrt_price"]).to_bytes(16, "little")
        + int(method["max_sqrt_price"]).to_bytes(16, "little")
        + b"\x00"
    )


def complete_increase_data(transaction_base64: str, plan: Mapping[str, Any]) -> str:
    """Replace a TRUNCATED deposit instruction's data with the full encoding, or refuse.

    Measured 2026-09-22 on mainnet: the remote builder encoded the enum's variant tag and
    dropped its four fields (9 bytes instead of 58), and the program answered BorshIoError.
    This keeps every account the builder placed (the gate checks those against our own
    derivation) and replaces the data ONLY when it is exactly the prefix of what the IDL
    says it must be. Any other difference is not a truncation and is refused.
    """
    from solders.instruction import CompiledInstruction
    from solders.message import Message
    from solders.transaction import Transaction

    expected = increase_data(plan)
    tx = Transaction.from_bytes(base64.b64decode(transaction_base64))
    message = tx.message
    keys = list(message.account_keys)
    rebuilt = []
    touched = 0
    for compiled in message.instructions:
        data = bytes(compiled.data)
        if str(keys[compiled.program_id_index]) == WHIRLPOOL_PROGRAM:
            if data != expected and not (
                len(data) < len(expected) and expected.startswith(data)
            ):
                raise WhirlpoolPlanError(
                    "the deposit instruction's data is neither the IDL's encoding nor a "
                    "truncation of it; refusing to guess which the builder meant"
                )
            data = expected
            touched += 1
        rebuilt.append(
            CompiledInstruction(
                compiled.program_id_index, data, bytes(compiled.accounts)
            )
        )
    if touched != 1:
        raise WhirlpoolPlanError(f"expected one deposit instruction, found {touched}")
    header = message.header
    fixed = Message.new_with_compiled_instructions(
        header.num_required_signatures,
        header.num_readonly_signed_accounts,
        header.num_readonly_unsigned_accounts,
        keys,
        message.recent_blockhash,
        rebuilt,
    )
    if list(fixed.account_keys) != keys or bytes(fixed.header) != bytes(header):
        raise WhirlpoolPlanError(
            "rebuilding the deposit changed its accounts or header"
        )
    return base64.b64encode(bytes(Transaction.new_unsigned(fixed))).decode()

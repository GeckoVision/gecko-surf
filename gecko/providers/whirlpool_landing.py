"""The Orca Whirlpool ``swap_v2`` landing orchestrator — the program that already RAN.

Whirlpool is the one endpoint in this repo with real mainnet swaps behind it, and until
now it was the one endpoint nobody could PROVE: ``ingest_gate`` R5 refused it for having
no dispatch key, which meant ``gecko prove`` could not run it and ``gecko watch`` could
not notice the day it broke. The thing we demo most was the thing we watched least.

WHAT A DERIVE-ONLY ``swap_v2`` CANNOT DO, and therefore what this closes:

  1. ``swap_v2`` creates NO token accounts. Both sides must already exist — including the
     one you are only RECEIVING into. Anchor reports the miss as 3012
     ``AccountNotInitialized`` against a slot name, which is accurate and tells you
     nothing about how to fix it. :func:`gecko.providers.whirlpool.plan_swap` names the
     gap in ``missing_atas``; this module turns each one into a ``CreateIdempotent``
     prelude so the bundle lands instead of teaching you Anchor error codes.
  2. A native-SOL leg needs the wrap/unwrap pair around the swap.
  3. ``oracle`` is an account ``plan_swap`` does not return, because it is derived rather
     than supplied. It is derived HERE, from the packaged recipe, never guessed.

WHAT THIS MODULE REFUSES TO DO, and the reason is the interesting part. The authoritative
account ORDER for ``swap_v2`` lives in Whirlpool's IDL, and that IDL is fetched live — it
is deliberately not packaged in this repo (see ``tests/test_ingest_gate.PACKAGED_IDLS``).
So this module does not reconstruct the account list. It completes the set it can NAME
from the packaged config and hands that to the build seam, whose lane the IDL-hard part
is. Anything the packaged config cannot name is left to the caller through
``extra_accounts`` and surfaces as a build error rather than a fabricated slot. A
well-formed wrong account is the failure mode this whole repo exists to prevent; a
missing one that says so is strictly better.

The compose boundary is the one :mod:`gecko.providers.meteora_landing` already holds:
**Gecko assembles the standard instructions only to prove the bundle lands ($0,
``sigVerify:false``, ``replaceRecentBlockhash:true``). It never signs, never sends, never
broadcasts.** Orquestra builds the real signable transaction; a separate signer signs it.

Control-plane invariant #1: the pool-state read and the built instruction are public
metadata held in memory; the assembled bundle and the Receipt are returned, never stored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..landing import (
    NATIVE_SOL_MINT,
    close_account_ix,
    create_idempotent_ata_ix,
    orquestra_instruction_to_solders,
    wrap_sol_ixs,
)
from ..landing import simulate_landing_bundle as _simulate_landing_bundle
from ..pda import derive_pda
from ..pda_testkit import LOCAL_RPC, RpcCall
from ..provider_config import load_packaged_provider, load_packaged_provider_base_url
from ..rpc import _http_post_json, validate_rpc_url
from ..simulate import Receipt
from .landing_record import record_landing_outcome
from .whirlpool import plan_swap

__all__ = [
    "FetchSwapV2Instruction",
    "WhirlpoolLandingError",
    "WhirlpoolLandingResult",
    "build_url",
    "simulate_swap_v2_landing",
]

#: ``plan_swap`` returns ONE ``values`` map carrying both accounts and args, because that
#: is the shape ``prepare_instruction`` takes. ``/build`` wants them apart, so the split
#: is declared here by name rather than guessed by type — ``a_to_b`` is a bool and
#: ``sqrt_price_limit`` is an int, and no type rule separates those from an account.
_ARG_NAMES = frozenset(
    {
        "amount",
        "other_amount_threshold",
        "sqrt_price_limit",
        "amount_specified_is_input",
        "a_to_b",
        "remaining_accounts_info",
    }
)

#: (accounts, args, feePayer) -> Orquestra's built ``swap_v2`` instruction object.
#: Injectable so the orchestrator is falsifiable offline with a canned instruction.
FetchSwapV2Instruction = Callable[
    [Mapping[str, Any], Mapping[str, Any], str], Mapping[str, Any]
]


class WhirlpoolLandingError(Exception):
    """A swap_v2 landing-orchestration failure — bad bindings, or a build response with
    no ``instruction``. Messages carry only public data, never a secret or a raw body."""


@dataclass(frozen=True)
class WhirlpoolLandingResult:
    """The side-by-side deliverable: the Gecko-complete landing Receipt next to the
    derive-only one, plus the quote, the ATAs this bundle had to create, and the CU
    limit the sim settled on."""

    landing_receipt: Receipt
    derive_only_receipt: Receipt | None
    pool: str
    direction: str
    expected_out_spot: int
    min_amount_out: int
    created_atas: list[str]
    unit_limit: int


def _program_spec() -> Any:
    _, apis = load_packaged_provider("orquestra")
    program = apis["whirlpool"].program
    if program is None:  # pragma: no cover - the packaged config always carries it
        raise WhirlpoolLandingError("the packaged whirlpool config declares no program")
    return program


def build_url(instruction: str = "swap_v2") -> str:
    """Orquestra's build endpoint for one Whirlpool instruction.

    Composed from the packaged provider base URL and the project slug the config already
    carries, rather than written out as a literal. A hardcoded URL is a second place the
    project id can be wrong, and the config is the first.
    """
    program = _program_spec()
    project = getattr(program, "orquestra_project", None)
    if not project:
        raise WhirlpoolLandingError(
            "the packaged whirlpool config names no orquestra_project, so its build "
            "endpoint cannot be composed"
        )
    base = load_packaged_provider_base_url("orquestra").rstrip("/")
    return f"{base}/{project}/instructions/{instruction}/build"


def _fetch_swap_v2_instruction_default(
    accounts: Mapping[str, Any],
    args: Mapping[str, Any],
    fee_payer: str,
    *,
    url: str,
) -> Mapping[str, Any]:
    """POST the plan to Orquestra ``/build`` and return its ``instruction`` object.

    ``/build`` is single-instruction: it returns the built ``swap_v2`` and ignores
    prelude parameters, which is exactly why this module assembles those itself for the
    simulation. Raises :class:`WhirlpoolLandingError` on transport failure or a missing
    ``instruction``, and never echoes the response body.
    """
    validate_rpc_url(url)
    body = json.dumps(
        {"accounts": dict(accounts), "args": dict(args), "feePayer": fee_payer}
    ).encode()
    try:
        payload = _http_post_json(url, body)
    except Exception as exc:  # noqa: BLE001 - redacted to a class at the transport edge
        raise WhirlpoolLandingError(
            f"the whirlpool build endpoint failed: {type(exc).__name__}"
        ) from exc
    instruction = (payload or {}).get("instruction")
    if not isinstance(instruction, Mapping):
        raise WhirlpoolLandingError(
            "the whirlpool build response carried no `instruction` object"
        )
    return instruction


def _derive_oracle(pool: str) -> str:
    """``oracle`` from the packaged recipe — seeded on the pool, never guessed.

    ``plan_swap`` does not return it because it is derived rather than caller-supplied,
    and the build needs it by name.
    """
    recipe = dict(_program_spec().pdas).get("oracle")
    if recipe is None:  # pragma: no cover - the packaged config always carries it
        raise WhirlpoolLandingError(
            "the packaged whirlpool config declares no `oracle`"
        )
    return derive_pda(recipe, {"whirlpool": pool}).address


def simulate_swap_v2_landing(
    bindings: Mapping[str, Any],
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
    unit_price_microlamports: int = 0,
    include_derive_only: bool = True,
    fetch_instruction: FetchSwapV2Instruction | None = None,
    idl_fetch: Any = None,
    extra_accounts: Mapping[str, str] | None = None,
    network_label: str | None = None,
    record_to: str | Path | None = None,
) -> WhirlpoolLandingResult:
    """Assemble the Whirlpool ``swap_v2`` landing bundle and simulate it.

    ``bindings`` needs ``input_mint``, ``output_mint``, ``user`` and ``amount_in``;
    ``slippage_bps`` and ``pool`` are optional and documented on
    :func:`gecko.providers.whirlpool.plan_swap`. Reads are control-plane only and the
    unsigned bundle is simulated, never sent. Both the RPC and the build are injectable,
    so the whole path is falsifiable offline.

    ``idl_fetch`` is forwarded to the planner, which needs the Whirlpool account
    LAYOUT to decode the pool. Left unset it is fetched live; injected, the whole
    orchestrator runs offline.

    ``extra_accounts`` fills any slot the packaged config cannot name — see the module
    docstring on why that is a parameter rather than a guess.

    ``record_to`` is the corpus opt-in, OFF by default. When set, ONE categorical row for
    the landing Receipt is appended to the path's segregated ``simulated.jsonl`` sibling:
    status, revert family, units, slot, network category, and a values-free structural
    hash — never a pubkey, an amount, or a log line.
    """
    required = ("input_mint", "output_mint", "user", "amount_in")
    missing = [key for key in required if key not in bindings]
    if missing:
        raise WhirlpoolLandingError(
            f"simulate_swap_v2_landing needs bindings {missing}"
        )

    user = str(bindings["user"])

    # (1) ONE pool read: the pool, its vaults, the three tick arrays, the quote, and the
    # ATAs that do not exist yet.
    plan = plan_swap(bindings, rpc_url=rpc_url, rpc_call=rpc_call, idl_fetch=idl_fetch)
    values: dict[str, Any] = dict(plan["values"])
    accounts = {k: v for k, v in values.items() if k not in _ARG_NAMES}
    args = {k: v for k, v in values.items() if k in _ARG_NAMES}

    # (2) the two slots `plan_swap` does not supply: one derived, one the signer.
    accounts["oracle"] = _derive_oracle(str(plan["pool"]))
    accounts["token_authority"] = user
    accounts.update(dict(extra_accounts or {}))

    # (3) Orquestra builds swap_v2 — its lane, because the account ORDER is the IDL's.
    url = build_url()
    fetch = fetch_instruction or (
        lambda a, ar, fp: _fetch_swap_v2_instruction_default(a, ar, fp, url=url)
    )
    swap_ix = orquestra_instruction_to_solders(fetch(accounts, args, user))

    # (4) the standard preludes AROUND it. Only the ATAs `plan_swap` measured as absent
    # are created — CreateIdempotent on an account that exists is a no-op, but paying
    # for one anyway would make the CU figure a worse prediction of the real thing.
    token_program_of = {
        str(values["token_mint_a"]): str(values["token_program_a"]),
        str(values["token_mint_b"]): str(values["token_program_b"]),
    }
    created_atas: list[str] = []
    prelude_ixs = []
    for gap in plan["missing_atas"]:
        mint, ata = str(gap["mint"]), str(gap["ata"])
        prelude_ixs.append(
            create_idempotent_ata_ix(
                payer=user,
                owner=user,
                mint=mint,
                ata=ata,
                token_program=token_program_of[mint],
            )
        )
        created_atas.append(ata)

    a_to_b = bool(values["a_to_b"])
    input_mint = str(values["token_mint_a"] if a_to_b else values["token_mint_b"])
    output_mint = str(values["token_mint_b"] if a_to_b else values["token_mint_a"])
    input_ata = str(
        values["token_owner_account_a" if a_to_b else "token_owner_account_b"]
    )
    output_ata = str(
        values["token_owner_account_b" if a_to_b else "token_owner_account_a"]
    )

    if input_mint == NATIVE_SOL_MINT:
        prelude_ixs.extend(wrap_sol_ixs(user, input_ata, int(values["amount"])))
    postlude_ixs = []
    if NATIVE_SOL_MINT in (input_mint, output_mint):
        wsol_ata = input_ata if input_mint == NATIVE_SOL_MINT else output_ata
        postlude_ixs.append(close_account_ix(wsol_ata, user, user))

    landing_receipt, unit_limit = _simulate_landing_bundle(
        swap_ix,
        prelude_ixs,
        user,
        rpc_url=rpc_url,
        rpc_call=rpc_call,
        unit_price_microlamports=unit_price_microlamports,
        track=[user],
        network_label=network_label,
        postlude_ixs=postlude_ixs,
        program="whirlpool",
        instruction="swap_v2",
    )

    derive_only_receipt: Receipt | None = None
    if include_derive_only:
        # The RAW swap — what an IDL-driven client produces: no ATA creation, no wrap.
        # It reverts at the first gap, which is the comparison worth showing.
        derive_only_receipt, _ = _simulate_landing_bundle(
            swap_ix,
            [],
            user,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            unit_price_microlamports=unit_price_microlamports,
            track=[user],
            network_label=network_label,
        )

    if record_to is not None:
        record_landing_outcome(
            landing_receipt,
            program_id=str(plan["program_id"]),
            instruction="swap_v2",
            account_names=accounts,
            arg_names=args,
            pdas=dict(_program_spec().pdas),
            network_label=network_label,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            record_to=record_to,
        )

    return WhirlpoolLandingResult(
        landing_receipt=landing_receipt,
        derive_only_receipt=derive_only_receipt,
        pool=str(plan["pool"]),
        direction=str(plan["direction"]),
        expected_out_spot=int(plan["quote"]["expected_out_spot"]),
        min_amount_out=int(plan["quote"]["min_amount_out"]),
        created_atas=created_atas,
        unit_limit=unit_limit,
    )

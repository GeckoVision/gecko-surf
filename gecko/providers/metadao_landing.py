"""The MetaDAO "fund-that-passes" orchestrator — the third program that RUNS.

The simplest of the runnable flows on purpose (the gap-map call): a ``fund`` is a fixed
USDC contribution — no price, no slippage, no oracle, no hidden remaining accounts.
The whole gap is COMPREHENSION: the IDL carries ZERO PDA seeds for this program, the
USDC vault exists only as a field inside the launch account, and the fund window is
runtime state. This module closes the loop for the $0 SIMULATION ONLY, mirroring
:mod:`gecko.providers.pumpfun_landing` / :mod:`gecko.providers.meteora_landing`:

  1. :func:`gecko.providers.metadao.plan_fund` reads the launch ONCE and declares
     everything — the 10-account set (camelCase, the project's wire names), the
     has_one-read vault, the fund-window verdict, the ordered landing plan;
  2. Orquestra ``/build`` builds the ``fund`` instruction (its lane — the IDL-hard
     part);
  3. the STANDARD prelude is assembled AROUND it — createIdempotentATA for the
     funder's USDC account + ComputeBudget — into an UNSIGNED bundle that is
     ``simulateTransaction``-ed (:mod:`gecko.landing`). ``funding_record`` needs NO
     prelude: the program init_if_needed's it (source-verified);
  4. the side-by-side Receipt comes back: derive-only (the raw built fund, no
     prelude) next to the Gecko-complete landing bundle.

The compose boundary holds exactly: **Gecko assembles the standard instructions only
to prove the bundle lands ($0, sigVerify:false, replaceRecentBlockhash:true), and
DECLARES the ordered plan. It never signs, never sends, never broadcasts.** Orquestra
builds the real signable tx; 1claw signs the bound receipt.

Control-plane invariant #1: the launch-state read + the built instruction are public
metadata held in memory; the assembled bundle and the Receipt are returned, never
stored.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..landing import (
    create_idempotent_ata_ix,
    orquestra_instruction_to_solders,
)
from ..landing import simulate_landing_bundle as _simulate_landing_bundle
from ..pda_testkit import LOCAL_RPC, RpcCall
from ..rpc import _http_post_json, validate_rpc_url
from ..simulate import Receipt
from .landing_record import record_landing_outcome
from .metadao import LAUNCHPAD_PROGRAM_ID, _load_metadao_pdas, plan_fund

__all__ = [
    "FetchFundInstruction",
    "FundLandingError",
    "FundLandingResult",
    "simulate_fund_landing",
]

# (accounts, args, feePayer) -> Orquestra's built `fund` instruction object.
# Injectable so the orchestrator is falsifiable offline with a canned instruction.
FetchFundInstruction = Callable[
    [Mapping[str, str], Mapping[str, Any], str], Mapping[str, Any]
]


class FundLandingError(Exception):
    """A fund-landing orchestration failure — bad bindings or a build response with
    no ``instruction``. Messages carry only public data, never a secret or raw body."""


@dataclass(frozen=True)
class FundLandingResult:
    """The side-by-side deliverable: the Gecko-complete landing Receipt next to the
    derive-only Receipt, the launch's fund-window verdict (declared at plan time,
    not discovered at revert time), the chosen CU limit, and the DECLARED ordered
    plan Orquestra/1claw assemble."""

    landing_receipt: Receipt
    derive_only_receipt: Receipt | None
    launch_state: str
    fund_window_open: bool
    fund_window_note: str
    amount: int
    unit_limit: int
    landing_plan: list[dict[str, Any]]


def _fetch_fund_instruction_default(
    accounts: Mapping[str, str],
    args: Mapping[str, Any],
    fee_payer: str,
    *,
    build_url: str,
) -> Mapping[str, Any]:
    """POST the plan to Orquestra ``/build`` and return its ``instruction`` object.

    ``/build`` is single-instruction: it returns the built ``fund`` as an
    ``instruction`` object and ignores prelude/compute-budget params — exactly why
    Gecko assembles those itself for the sim. Account keys must be the project's
    exact (camelCase) names. Raises :class:`FundLandingError` on a transport failure
    or a missing ``instruction`` (never echoes the body).
    """
    validate_rpc_url(build_url)
    body = json.dumps(
        {"accounts": dict(accounts), "args": dict(args), "feePayer": fee_payer}
    ).encode()
    try:
        resp = _http_post_json(build_url, body)
    except urllib.error.HTTPError as exc:
        raise FundLandingError(
            f"build POST to {build_url} failed: HTTP {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise FundLandingError(
            f"build POST to {build_url} failed: {exc.reason}"
        ) from exc
    instruction = resp.get("instruction")
    if not isinstance(instruction, Mapping):
        raise FundLandingError(
            f"build response from {build_url} carried no `instruction` object"
        )
    return instruction


def simulate_fund_landing(
    bindings: Mapping[str, Any],
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
    unit_price_microlamports: int = 0,
    include_derive_only: bool = True,
    fetch_fund_instruction: FetchFundInstruction | None = None,
    network_label: str | None = None,
    record_to: str | Path | None = None,
) -> FundLandingResult:
    """Assemble the MetaDAO fund landing bundle and simulate it → a
    :class:`FundLandingResult`.

    ``bindings`` needs ``base_mint``, ``funder``, ``amount`` (**USDC base units, 6
    decimals**; optional ``payer`` defaults to the funder). Reads are control-plane
    only; the unsigned bundle is simulated, never sent. Both the RPC and the
    Orquestra build are injectable, so the whole path is falsifiable offline.

    ``record_to`` is the D2 corpus opt-in — OFF by default (None = today's behavior,
    nothing persisted). When set, ONE categorical ``SimulatedOutcome`` row for the
    landing Receipt is appended to the path's segregated ``simulated.jsonl`` sibling
    (:func:`gecko.providers.landing_record.record_landing_outcome`): status / revert
    family / units / slot / network category + a values-free structural
    ``recipe_hash`` — never a pubkey, amount, or log line.
    """
    missing = [k for k in ("base_mint", "funder", "amount") if k not in bindings]
    if missing:
        raise FundLandingError(f"simulate_fund_landing needs bindings {missing}")

    funder = str(bindings["funder"])
    amount = int(bindings["amount"])

    # (1) the full declared plan: ONE launch read → accounts, vault, window verdict.
    plan = plan_fund(bindings, rpc_url=rpc_url, rpc_call=rpc_call)
    accounts: dict[str, str] = dict(plan["accounts"])
    payer = accounts["payer"]

    # (2) Orquestra builds the `fund` instruction (its lane).
    fetch = fetch_fund_instruction or (
        lambda a, ar, fp: _fetch_fund_instruction_default(
            a, ar, fp, build_url=plan["build_url"]
        )
    )
    built = fetch(accounts, plan["args"], payer)
    fund_ix = orquestra_instruction_to_solders(built)

    # (3) the STANDARD prelude around it, in the declared order: the funder's USDC
    # ATA, idempotent (funding_record needs nothing — the program init_if_needed's
    # it; the no-drift guard in the tests compares this assembly to landing_plan).
    ata_ix = create_idempotent_ata_ix(
        payer=payer,
        owner=funder,
        mint=plan["launch_state"]["quote_mint"],
        ata=accounts["funderQuoteAccount"],
        token_program=accounts["tokenProgram"],
    )
    landing_receipt, unit_limit = _simulate_landing_bundle(
        fund_ix,
        [ata_ix],
        payer,
        rpc_url=rpc_url,
        rpc_call=rpc_call,
        unit_price_microlamports=unit_price_microlamports,
        track=[payer],
        network_label=network_label,
    )

    derive_only_receipt: Receipt | None = None
    if include_derive_only:
        # The RAW Orquestra fund — no ATA prelude. NOTE the honest asymmetry vs
        # pump/meteora: once Gecko has filled the accounts, a funder who already
        # holds the USDC ATA lands even derive-only — the Class-1 gap for THIS
        # program is upstream, in deriving/reading the account set at all (the IDL
        # carries zero seeds and the vault is a state field).
        derive_only_receipt, _ = _simulate_landing_bundle(
            fund_ix,
            [],
            payer,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            unit_price_microlamports=unit_price_microlamports,
            track=[payer],
            network_label=network_label,
        )

    # (4) the D2 corpus opt-in: categorical outcome → segregated simulated.jsonl.
    # The fingerprint reads the plan's NAMES + the packaged seed-KIND graph, never
    # the resolved accounts above. Explicit opt-in only — record_to=None persists
    # nothing.
    if record_to is not None:
        record_landing_outcome(
            landing_receipt,
            program_id=LAUNCHPAD_PROGRAM_ID,
            instruction="fund",
            account_names=accounts,
            arg_names=plan["args"],
            pdas=_load_metadao_pdas(),
            network_label=network_label,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            record_to=record_to,
        )

    return FundLandingResult(
        landing_receipt=landing_receipt,
        derive_only_receipt=derive_only_receipt,
        launch_state=plan["launch_state"]["state"],
        fund_window_open=plan["launch_state"]["fund_window_open"],
        fund_window_note=plan["launch_state"]["note"],
        amount=amount,
        unit_limit=unit_limit,
        landing_plan=plan["landing_plan"],
    )

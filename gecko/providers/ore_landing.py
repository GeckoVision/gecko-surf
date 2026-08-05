"""The ORE "claim-that-passes" orchestrator — the fourth program that RUNS.

The narrowest prelude of the four flows and the sharpest gap: ``claimOre`` needs no ATA
prelude at all (``claim_ore`` creates the recipient itself) and no amount — yet the
instruction the builder returns **does not land**. Its surface marks ``board``
``isMut: false``; the deployed program hands the board to a closing self-CPI as a
writable signer, so the naive bundle transfers the tokens and *then* fails with
``"writable privilege escalated"``. That, plus three of the eleven accounts being
unresolvable from the surface (the pinned mint, the signer's ORE ATA, the treasury PDA's
own ATA), a payout only knowable by decoding a ``Miner`` whose shipped IDL layout is 208
bytes short, and a ``bps`` argument that exists in source but not on the builder, is the
whole ORE comprehension gap. This module closes the loop for the $0 SIMULATION ONLY,
mirroring :mod:`gecko.providers.pumpfun_landing` /
:mod:`gecko.providers.meteora_landing` / :mod:`gecko.providers.metadao_landing`:

  1. :func:`gecko.providers.ore.plan_claim` reads the miner + treasury ONCE each and
     declares everything — the 11-account set (camelCase, the project's wire names),
     the claimable floor, the checkpoint verdict, the ordered landing plan;
  2. Orquestra ``/build`` builds the ``claimOre`` instruction (its lane);
  3. Gecko RECONCILES the account metas against source — widening ``board`` to writable
     (:func:`gecko.landing.with_writable_accounts`, which only ever widens writability,
     never signer-ness) — and assembles the STANDARD ComputeBudget prelude around it
     into an UNSIGNED bundle that is ``simulateTransaction``-ed (:mod:`gecko.landing`);
  4. the side-by-side Receipt comes back: the NAIVE bundle (the builder's instruction
     verbatim, board read-only) next to the Gecko-complete landing bundle.

**The semantics guard.** Before simulating, the orchestrator compares the data of the
built instruction against the SOURCE-TRUE bytes :func:`gecko.providers.ore.
claim_instruction_data` declares. At ``bps=10000`` they agree (``"04"``). Below that they
do not — the builder returns ``"04"`` regardless — and the orchestrator RAISES rather
than simulate a bundle that would claim 100% of a wallet that asked for a fraction. A
silent semantic swap is worse than a loud refusal.

The compose boundary holds exactly: **Gecko assembles the standard instructions only to
prove the bundle lands ($0, sigVerify:false, replaceRecentBlockhash:true), and DECLARES
the ordered plan. It never signs, never sends, never broadcasts.** Orquestra builds the
real signable tx; 1claw signs the bound receipt.

Control-plane invariant #1: the miner/treasury reads + the built instruction are public
metadata held in memory; the assembled bundle and the Receipt are returned, never stored.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..landing import orquestra_instruction_to_solders, with_writable_accounts
from ..landing import simulate_landing_bundle as _simulate_landing_bundle
from ..pda_testkit import LOCAL_RPC, RpcCall
from ..rpc import _http_post_json, validate_rpc_url
from ..simulate import Receipt
from .landing_record import record_landing_outcome
from .ore import ORE_PROGRAM_ID, _load_ore_pdas, plan_claim
from .ore import BUILD_URL as _DEFAULT_BUILD_URL

__all__ = [
    "FetchClaimInstruction",
    "ClaimLandingError",
    "ClaimLandingResult",
    "simulate_claim_landing",
]

# (accounts, args, feePayer) -> Orquestra's built `claimOre` instruction object.
# Injectable so the orchestrator is falsifiable offline with a canned instruction.
FetchClaimInstruction = Callable[
    [Mapping[str, str], Mapping[str, Any], str], Mapping[str, Any]
]


class ClaimLandingError(Exception):
    """A claim-landing orchestration failure — bad bindings, a build response with no
    ``instruction``, or a built instruction whose data does not carry the requested
    ``bps``. Messages carry only public data, never a secret or raw body."""


@dataclass(frozen=True)
class ClaimLandingResult:
    """The side-by-side deliverable: the Gecko-complete landing Receipt next to the
    derive-only Receipt, the miner's claim verdict (declared at plan time, not
    discovered at revert time), the chosen CU limit, and the DECLARED ordered plan
    Orquestra/1claw assemble."""

    landing_receipt: Receipt
    derive_only_receipt: Receipt | None
    authority: str
    claimable_ore: int
    claim_amount_at_least: int
    refining_fee: int
    decimals: int
    checkpoint_pending: bool
    requested_bps: int
    unit_limit: int
    landing_plan: list[dict[str, Any]]
    # The accounts whose writability Gecko had to correct from source before the
    # builder's instruction could land — empty when the builder already agreed.
    corrected_metas: tuple[str, ...] = ()


def _fetch_claim_instruction_default(
    accounts: Mapping[str, str],
    args: Mapping[str, Any],
    fee_payer: str,
    *,
    build_url: str,
) -> Mapping[str, Any]:
    """POST the plan to Orquestra ``/build`` and return its ``instruction`` object.

    ``/build`` is single-instruction: it returns the built ``claimOre`` as an
    ``instruction`` object and ignores prelude/compute-budget params — exactly why
    Gecko assembles those itself for the sim. Account keys must be the project's exact
    (camelCase) names, and so must the instruction path (``/instructions/claimOre/``).
    Raises :class:`ClaimLandingError` on a transport failure or a missing
    ``instruction`` (never echoes the body).
    """
    validate_rpc_url(build_url)
    body = json.dumps(
        {"accounts": dict(accounts), "args": dict(args), "feePayer": fee_payer}
    ).encode()
    try:
        resp = _http_post_json(build_url, body)
    except urllib.error.HTTPError as exc:
        raise ClaimLandingError(
            f"build POST to {build_url} failed: HTTP {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ClaimLandingError(
            f"build POST to {build_url} failed: {exc.reason}"
        ) from exc
    instruction = resp.get("instruction")
    if not isinstance(instruction, Mapping):
        raise ClaimLandingError(
            f"build response from {build_url} carried no `instruction` object"
        )
    return instruction


def _assert_semantics(built: Mapping[str, Any], declared_data: str, bps: int) -> None:
    """Refuse a built instruction whose data does not encode the requested ``bps``.

    The builder emits ``"04"`` unconditionally (wire-verified), which the program reads
    as a 100% claim. For ``bps == 10000`` that IS the ask. For anything less it is a
    silent semantic swap — the caller asked for a fraction and would get everything —
    so we raise instead of simulating a lie.
    """
    got = built.get("data")
    if not isinstance(got, str) or got.lower() != declared_data.lower():
        raise ClaimLandingError(
            f"the built claimOre carries data {got!r} but a {bps} bps claim needs "
            f"{declared_data!r}: the builder drops the optional `bps` arg (its surface "
            "declares args: []), so this bundle would claim 100%, not "
            f"{bps / 100:g}%. Refusing to simulate the wrong semantics — claim in full "
            "(bps=10000) or use a builder that carries the argument."
        )


def simulate_claim_landing(
    bindings: Mapping[str, Any],
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
    unit_price_microlamports: int = 0,
    include_derive_only: bool = True,
    fetch_claim_instruction: FetchClaimInstruction | None = None,
    network_label: str | None = None,
    record_to: str | Path | None = None,
) -> ClaimLandingResult:
    """Assemble the ORE claim landing bundle and simulate it → a
    :class:`ClaimLandingResult`.

    ``bindings`` needs ``signer`` (the miner's own authority); optional ``bps``
    (basis points, default 10000 = 100%). Reads are control-plane only; the unsigned
    bundle is simulated, never sent. Both the RPC and the Orquestra build are
    injectable, so the whole path is falsifiable offline.

    ``record_to`` is the D2 corpus opt-in — OFF by default (None = today's behavior,
    nothing persisted). When set, ONE categorical ``SimulatedOutcome`` row for the
    landing Receipt is appended to the path's segregated ``simulated.jsonl`` sibling
    (:func:`gecko.providers.landing_record.record_landing_outcome`): status / revert
    family / units / slot / network category + a values-free structural
    ``recipe_hash`` — never a pubkey, amount, or log line.
    """
    if "signer" not in bindings:
        raise ClaimLandingError("simulate_claim_landing needs bindings ['signer']")

    # (1) the full declared plan: miner + treasury reads → accounts, claim verdict.
    plan = plan_claim(bindings, rpc_url=rpc_url, rpc_call=rpc_call)
    accounts: dict[str, str] = dict(plan["accounts"])
    signer = accounts["signer"]

    # (2) Orquestra builds the `claimOre` instruction (its lane).
    fetch = fetch_claim_instruction or (
        lambda a, ar, fp: _fetch_claim_instruction_default(
            a, ar, fp, build_url=plan.get("build_url", _DEFAULT_BUILD_URL)
        )
    )
    built = fetch(accounts, plan["args"], signer)
    # (3) the semantics guard BEFORE any simulation — a wrong-bps bundle never runs.
    _assert_semantics(built, plan["instruction_data"], plan["requested_bps"])
    naive_ix = orquestra_instruction_to_solders(built)

    # (4) THE meta reconciliation. The builder's surface marks `board` read-only; the
    # deployed program hands it to a self-CPI as a writable signer, so the naive
    # instruction dies on "writable privilege escalated" AFTER the transfer already
    # succeeded. Gecko widens only what SOURCE says is writable — never signer-ness.
    writable = [
        accounts[name]
        for name, meta in plan["account_metas"].items()
        if meta["is_writable"] and name in accounts
    ]
    claim_ix, corrected_metas = with_writable_accounts(naive_ix, writable)

    # (5) the STANDARD prelude around it: compute budget only. NO idempotent-ATA step —
    # claim_ore creates the recipient itself when empty (source-verified); the no-drift
    # guard in the tests compares this assembly to landing_plan.
    landing_receipt, unit_limit = _simulate_landing_bundle(
        claim_ix,
        [],
        signer,
        rpc_url=rpc_url,
        rpc_call=rpc_call,
        unit_price_microlamports=unit_price_microlamports,
        track=[signer],
        network_label=network_label,
        program="ore",
        instruction="claim",
    )

    derive_only_receipt: Receipt | None = None
    if include_derive_only:
        # The NAIVE bundle: the builder's instruction VERBATIM — the account metas its
        # surface declares, board read-only included. This is what a client that trusts
        # the IDL sends, and on ORE it is not a near-miss: it executes the transfer and
        # then fails at the closing self-CPI.
        derive_only_receipt, _ = _simulate_landing_bundle(
            naive_ix,
            [],
            signer,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            unit_price_microlamports=0,
            track=[signer],
            network_label=network_label,
        )

    # (5) the D2 corpus opt-in: categorical outcome → segregated simulated.jsonl.
    # The fingerprint reads the plan's NAMES + the packaged seed-KIND graph, never the
    # resolved accounts above. Explicit opt-in only — record_to=None persists nothing.
    if record_to is not None:
        record_landing_outcome(
            landing_receipt,
            program_id=ORE_PROGRAM_ID,
            instruction="claimOre",
            account_names=accounts,
            arg_names=plan["args"],
            pdas=_load_ore_pdas(),
            network_label=network_label,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            record_to=record_to,
        )

    state = plan["miner_state"]
    return ClaimLandingResult(
        landing_receipt=landing_receipt,
        derive_only_receipt=derive_only_receipt,
        authority=state["authority"],
        claimable_ore=state["claimable_ore"],
        claim_amount_at_least=state["claim_amount_at_least"],
        refining_fee=state["refining_fee"],
        decimals=state["decimals"],
        checkpoint_pending=state["checkpoint_pending"],
        requested_bps=plan["requested_bps"],
        unit_limit=unit_limit,
        landing_plan=plan["landing_plan"],
        corrected_metas=corrected_metas,
    )

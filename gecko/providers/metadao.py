"""MetaDAO launchpad_v7 — the third Orquestra program made runnable end-to-end.

The capstone comprehension gap: Orquestra's PDA Finder reports **"No PDA accounts
found in this IDL"** for this program — its Anchor-IDL generator drops EVERY seed
(none are declared in IDL ``pda.seeds``; they live only as ``#[account(seeds=...)]``
in v07 source). ``plan_fund`` assembles the full 10-account ``fund`` set an IDL-driven
client cannot: the source-recovered ``launch``/``funding_record`` PDAs, the Anchor
``#[event_cpi]`` ``event_authority`` (macro-generated — on no machine surface at all),
the funder's USDC ATA, and the launch's USDC vault — which is NOT derivable, only
READABLE (``has_one = launch_quote_vault``: the field inside the launch account is the
single source of truth, the same dotted-path class as pump's ``bonding_curve.creator``).

Source-verified facts (metaDAOproject/programs ``programs/v07_launchpad/src/
instructions/fund.rs`` — the DEPLOYED program is v07; Orquestra's "IDL v8" label is its
own upload counter, the version trap):

  * ``funding_record`` is ``init_if_needed`` inside ``fund`` itself — the program
    initializes it on a funder's first fund and ``payer`` pays the rent. NO separate
    init call exists (unlike pump's ``associated_user`` precondition).
  * ``amount`` is a plain ``u64`` in the QUOTE mint's base units — USDC, 6 decimals
    (10 USDC = 10_000_000). No defined-type wrapper (no OptionBool-class trap here;
    verified against the live Orquestra instruction surface).
  * ``token_program`` is pinned to classic SPL Token by source
    (``Program<'info, Token>``) — never Token-2022, so no mint-owner read is needed.
  * ``fund`` validates ``state == Live`` and ``now < started + seconds_for_launch``;
    Gecko reads the launch state at plan time and declares the window honestly.

Naming gotcha (wire-verified): this Orquestra project speaks the IDL's **camelCase**
account names (``fundingRecord``, ``launchQuoteVault``, …) — unlike the pump/meteora
projects' snake_case — and ``/build`` matches names exactly.

Run it (once its slug is served):
    claude mcp add orquestra-metadao -- \
        uvx --from "gecko-surf[serve,solana]" gecko-orquestra --program metadao_ico --stdio
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from ..find_start import PreludeSpec, StartSpec
from ..landing import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    COMPUTE_BUDGET_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from ..metadao_state import LaunchAccountState, read_launch_state
from ..pda import derive_pda
from ..pda_testkit import LOCAL_RPC, RpcCall
from ..provider_config import load_packaged_provider
from .orquestra import Intent, OrquestraProgramSurface

__all__ = [
    "LAUNCHPAD_PROGRAM_ID",
    "BUILD_URL",
    "METADAO_INTENTS",
    "METADAO_STARTS",
    "plan_fund",
    "build_metadao_surface",
]

# Display constant. The authoritative program id + PDA recipes live in the packaged
# config (gecko/providers/configs/orquestra/metadao_ico.json) — data, not code.
LAUNCHPAD_PROGRAM_ID = "moontUzsdepotRGe5xsfip7vLPTJnVuafqdUWexVnPM"
BUILD_URL = (
    "https://api.orquestra.dev/api/krhmrxpy2fgwn3q0whic7/instructions/fund/build"
)

# Why the vault is a read, never a derivation — kept as the single canonical note
# (plan_fund and the StartSpec both reuse it; refactor here, not in two places).
_QUOTE_VAULT_NOTE = (
    "launchQuoteVault is NOT derivable client-side: the program's `has_one = "
    "launch_quote_vault` constraint makes the field INSIDE the launch account the "
    "single source of truth. Gecko reads it from the launch state (one control-plane "
    "getAccountInfo — the same dotted-path class as pump's bonding_curve.creator)."
)

_FUNDING_RECORD_NOTE = (
    "per-funder PDA ['funding_record', launch, funder], recovered from v07 source. "
    "fund is `init_if_needed`: the PROGRAM initializes it on the funder's first fund "
    "and `payer` pays the rent — no separate init instruction exists, so this is NOT "
    "a precondition (unlike pump's associated_user)."
)


def _load_metadao_pdas() -> dict[str, Any]:
    _, apis = load_packaged_provider("orquestra")
    program = apis["metadao_ico"].program
    if program is None:
        raise ValueError("metadao_ico config carries no program spec")
    return dict(program.pdas)


def _landing_plan(accounts: Mapping[str, str], quote_mint: str) -> list[dict[str, Any]]:
    """The DECLARED ordered instruction plan a real builder (Orquestra) assembles +
    1claw signs — pure structured data (no bytes, no signing):
    ``[SetComputeUnitLimit, SetComputeUnitPrice, createIdempotentATA(funder USDC),
    fund]``. The ATA prelude covers the ACCOUNT-exists gap only — it cannot conjure
    the USDC balance (see ``preconditions``). Compute-budget values are filled by the
    simulate step. See :func:`gecko.providers.metadao_landing.simulate_fund_landing`.
    """
    return [
        {
            "kind": "compute_budget",
            "program": COMPUTE_BUDGET_PROGRAM_ID,
            "instruction": "SetComputeUnitLimit",
            "note": "units from the Receipt's units_consumed × 1.2 (Gecko simulates to measure)",
        },
        {
            "kind": "compute_budget",
            "program": COMPUTE_BUDGET_PROGRAM_ID,
            "instruction": "SetComputeUnitPrice",
            "note": "micro-lamports from getRecentPrioritizationFees (operator's RPC)",
        },
        {
            "kind": "create_idempotent_ata",
            "program": ASSOCIATED_TOKEN_PROGRAM_ID,
            "accounts": {
                "payer": accounts["payer"],
                "ata": accounts["funderQuoteAccount"],
                "owner": accounts["funder"],
                "mint": quote_mint,
                "system_program": SYSTEM_PROGRAM_ID,
                "token_program": accounts["tokenProgram"],
            },
            "note": (
                "initializes the funder's USDC ATA if missing (no-op if held) — it "
                "cannot conjure the USDC balance the fund transfers"
            ),
        },
        {
            "kind": "fund",
            "program": LAUNCHPAD_PROGRAM_ID,
            "accounts": dict(accounts),
            "note": (
                "Orquestra builds this instruction; amount is USDC base units "
                "(6 decimals) and funding_record self-initializes (init_if_needed)"
            ),
        },
    ]


def plan_fund(
    bindings: Mapping[str, Any],
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
) -> dict[str, Any]:
    """Assemble the full account set for a MetaDAO launchpad ``fund`` → an Orquestra
    ``/build`` payload plus the ordered landing plan.

    ``bindings`` needs ``base_mint`` (the token being launched), ``funder``, and
    ``amount`` (**USDC base units, 6 decimals** — 10 USDC = 10_000_000; sending 10
    would commit 0.00001 USDC). Optional ``payer`` defaults to the funder (it signs
    and pays the funding_record rent on a first fund).

    ONE control-plane read happens (the launch account — public metadata, never
    stored): it confirms the launch exists, yields ``quote_mint`` +
    ``launchQuoteVault`` (the non-derivable ``has_one`` field), and declares the fund
    window (``state == Live``, ``now < started + seconds_for_launch``) under
    ``launch_state`` — planning against a closed launch is declared, not discovered
    at revert time. Account keys use the Orquestra project's exact (camelCase) names.
    """
    missing = [k for k in ("base_mint", "funder", "amount") if k not in bindings]
    if missing:
        raise ValueError(f"plan_fund needs bindings {missing}")

    base_mint = str(bindings["base_mint"])
    funder = str(bindings["funder"])
    payer = str(bindings.get("payer") or funder)
    amount = int(bindings["amount"])
    pdas = _load_metadao_pdas()

    # (1) derive the root the IDL carries ZERO seeds for, then the per-funder record.
    launch = derive_pda(pdas["launch"], {"base_mint": base_mint}).address
    funding_record = derive_pda(
        pdas["funding_record"], {"launch": launch, "funder": funder}
    ).address

    # (2) the ONE state read: existence + quote_mint + the has_one vault + the window.
    state: LaunchAccountState = read_launch_state(
        launch, rpc_url=rpc_url, rpc_call=rpc_call
    )
    window_open, window_note = state.fund_window(int(time.time()))

    # (3) the funder's USDC ATA. Classic SPL Token by source (Program<'info, Token>) —
    # no mint-owner read; a Token-2022 quote mint would be rejected by the program.
    funder_quote_account = derive_pda(
        pdas["funder_quote_account"],
        {"owner": funder, "token_program": TOKEN_PROGRAM_ID, "mint": state.quote_mint},
    ).address
    event_authority = derive_pda(pdas["event_authority"], {}).address

    # Keys are the Orquestra project's EXACT account names — camelCase here
    # (wire-verified), unlike the pump/meteora projects' snake_case.
    accounts: dict[str, str] = {
        "launch": launch,
        "fundingRecord": funding_record,
        "launchQuoteVault": state.launch_quote_vault,
        "funder": funder,
        "payer": payer,
        "funderQuoteAccount": funder_quote_account,
        "tokenProgram": TOKEN_PROGRAM_ID,
        "systemProgram": SYSTEM_PROGRAM_ID,
        "eventAuthority": event_authority,
        "program": LAUNCHPAD_PROGRAM_ID,
    }

    return {
        "instruction": "fund",
        "accounts": accounts,
        # amount unit honesty lives next to the value, not only in prose.
        "args": {"amount": amount},
        "args_note": (
            "amount is in USDC base units (6 decimals): 10 USDC = 10_000_000. "
            "The program requires amount > 0 and the funder's USDC balance >= amount."
        ),
        "feePayer": payer,
        "build_url": BUILD_URL,
        # The declared fund-window verdict from the state read — fund's own
        # validate() gates, checked at plan time instead of revert time.
        "launch_state": {
            "state": state.state_name,
            "fund_window_open": window_open,
            "note": window_note,
            "quote_mint": state.quote_mint,
            "total_committed_amount": state.total_committed_amount,
            "minimum_raise_amount": state.minimum_raise_amount,
        },
        "preconditions": {
            "funderQuoteAccount": (
                "must be the funder's ASSOCIATED USDC token account holding >= amount "
                "(source: associated_token::mint = launch.quote_mint, authority = "
                "funder; balance short reverts LaunchpadError::InsufficientFunds). "
                "The landing plan's idempotent-ATA prelude creates the ACCOUNT if "
                "missing; the USDC balance is the funder's own"
            ),
            "launch_state": (
                "fund requires state == Live inside the window (started + "
                "seconds_for_launch) — see `launch_state` for this launch's verdict"
            ),
        },
        "landing_plan": _landing_plan(accounts, state.quote_mint),
        "simulate": {
            "after": "POST build_url to get the built fund instruction/tx",
            "rpc_method": "simulateTransaction",
            "params_note": (
                "the tx in the encoding /build reports + {sigVerify:false, "
                "replaceRecentBlockhash:true, commitment:'processed'}"
            ),
            "gecko_tool": (
                "simulate  # Path A: hand this plan to Gecko's simulate tool"
            ),
        },
    }


def _fund_plan(
    surface: OrquestraProgramSurface, args: Mapping[str, Any]
) -> dict[str, str]:
    """Intent adapter for the MCP surface: run plan_fund and hand back the resolved
    accounts (the surface wraps them with the /build execute URL)."""
    plan = plan_fund(args)
    return plan["accounts"]


_FUND = Intent(
    name="plan_fund",
    instruction="fund",
    description=(
        "Plan a MetaDAO launchpad fund: contribute USDC to a Live token launch "
        "(ICO). Give base_mint (the token being launched), funder, and amount in "
        "USDC BASE UNITS (6 decimals — 10 USDC = 10_000_000, NOT 10). Gecko derives "
        "every account of the 10-account fund the IDL carries ZERO seeds for — the "
        "launch root, the per-funder funding_record (init_if_needed: the program "
        "creates it on first fund, no separate init), the macro-generated "
        "event_authority, the funder's USDC ATA — and READS the launch's USDC vault "
        "plus the fund window (state must be Live) from the launch account, so a "
        "closed launch is declared at plan time, not at revert time."
    ),
    inputs=("base_mint", "funder", "amount"),
    plan=_fund_plan,
)

# The code half of the config: the plan callables this program exposes, keyed by
# intent name. The config lists the intent NAMES; here we supply their derivation.
METADAO_INTENTS: dict[str, Intent] = {_FUND.name: _FUND}

# What find_start declares about plan_fund: the derive set (config PDAs + the read
# vault kept in place as a non-PDA slot), the recovered facts, and the DECLARED
# landing preludes. Pure data.
METADAO_STARTS: dict[str, StartSpec] = {
    "plan_fund": StartSpec(
        accounts=(
            "launch",
            "funding_record",
            "launch_quote_vault",
            "funder_quote_account",
            "event_authority",
        ),
        recovered={
            "launch": (
                "the root PDA (['launch', base_mint]) recovered from v07 source — "
                "Orquestra's PDA Finder returns ZERO PDAs for this program (no seeds "
                "declared in IDL pda.seeds; the deployed program is v07_launchpad, "
                "despite the catalog's 'IDL v8' upload-counter label)"
            ),
            "funding_record": _FUNDING_RECORD_NOTE,
            "launch_quote_vault": _QUOTE_VAULT_NOTE,
            "event_authority": (
                "generated by Anchor's #[event_cpi] macro (seed "
                "['__event_authority']) — implicit in source, on no machine surface"
            ),
        },
        preludes=(
            PreludeSpec(
                kind="compute_budget",
                program=COMPUTE_BUDGET_PROGRAM_ID,
                note=(
                    "SetComputeUnitLimit (units from the simulate Receipt) + "
                    "SetComputeUnitPrice (getRecentPrioritizationFees)"
                ),
            ),
            PreludeSpec(
                kind="create_idempotent_ata",
                program=ASSOCIATED_TOKEN_PROGRAM_ID,
                note=(
                    "createAssociatedTokenAccountIdempotent for the funder's USDC "
                    "ATA (classic SPL Token — pinned by source, never Token-2022). "
                    "Creates the ACCOUNT only; the USDC balance (>= amount, 6-decimal "
                    "base units) is the funder's own"
                ),
            ),
        ),
    )
}


def build_metadao_surface() -> OrquestraProgramSurface:
    """Build the MetaDAO surface from packaged config (identity + PDA recipes) + the
    local intent registry (the plan callables)."""
    from .cli import build_surface_from_config

    return build_surface_from_config("orquestra", "metadao_ico", METADAO_INTENTS)

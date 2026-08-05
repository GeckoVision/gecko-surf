"""ORE V3 ``claimOre`` — the fourth Orquestra program made runnable end-to-end.

Why ``claim`` and not ``mine``: mining needs an off-chain drillx proof-of-work solution
that neither Gecko nor a builder computes — recommending it would be scope creep into
building a miner. ``claim`` (harvest accrued rewards) is the realistic agent flow. The
decision is recorded in ``docs/specs/2026-08-04-orquestra-program-flow-gap-map.md``.

ORE is a **Steel** program, so there is no Anchor IDL. It ships a hand-maintained
``api/idl.json`` (``metadata.origin: "steel"``) that Orquestra serves; where that
disagrees with the deployed source, SOURCE WINS. Verified against
``regolith-labs/ore@master`` (``program/src/claim_ore.rs``, ``api/src/sdk.rs``,
``api/src/state/miner.rs``, ``api/src/consts.rs``) AND real mainnet instructions:

  * **The read-only ``board`` (Class-1, LIVE-PROVEN).** The shipped IDL, the live
    Orquestra surface and ``/build`` all mark ``board`` ``isMut: false``. ``claim_ore``
    ends with ``program_log``, a self-CPI whose inner ``log`` instruction takes the
    board as a WRITABLE signer — so the builder's instruction runs, transfers the
    tokens, and *then* dies on ``"BrcSxdp…'s writable privilege escalated / Cross-
    program invocation with unauthorized signer or writable account"``. ``sdk::claim_ore``
    marks it ``AccountMeta::new(board, false)`` (writable) and every real mainnet
    claimOre carries it writable. This is a failure no static surface reveals and no
    account derivation fixes; :mod:`gecko.providers.ore_landing` reconciles the meta
    from source, which is the difference between the naive bundle failing and the
    Gecko bundle landing.
  * **The dropped ``bps`` arg (Class-1, live).** ``claim_ore`` parses
    ``ClaimORE { bps: [u8; 8] }`` — the percentage to claim, in basis points, defaulting
    to 100% when the data is absent. The shipped IDL and the live Orquestra surface both
    declare ``args: []``, and ``POST /instructions/claimOre/build`` returns ``data:
    "04"`` (discriminator only) **even when ``bps`` is supplied** — so "claim half"
    silently becomes "claim everything". 6 of the last 7 real mainnet ``claimOre``
    instructions carried a 9-byte payload (disc + bps); the surface says that argument
    does not exist. :func:`plan_claim` declares the source-true bytes and
    :mod:`gecko.providers.ore_landing` REFUSES to simulate a plan the builder cannot
    carry, instead of landing the wrong semantics.
  * **authority is NOT always the signer — and for claim it MUST be.**
    ``claim_ore`` binds ``miner`` with ``has_seeds(&[MINER, signer.key])`` *and*
    asserts ``miner.authority == signer.key``. So a claim signed by anyone other than
    the miner's own authority cannot be made correct by choosing a different seed — it
    is simply not claimable. (The authority/signer split is real in ``deploy`` and
    ``checkpoint``, which take a SEPARATE ``authority`` account and seed ``miner`` by
    IT; ``ore-cli`` PR #150 added a ``proof_authority`` arg for that confusion.)
  * **No ``round``.** ``claim_ore`` never touches the ``round`` PDA, so the flagged
    runtime-data seed (``board.round_id`` / ``miner.round_id``) is not on this path.
    It IS on ``checkpoint``, claim's precondition — declared, not resolved here.
  * **``recipient`` needs no prelude.** ``claim_ore`` calls
    ``create_associated_token_account`` itself when ``recipient`` is empty (payer =
    signer), so the landing plan carries compute budget only — the same class as
    MetaDAO's ``init_if_needed`` ``funding_record``.
  * **11 decimals** (``TOKEN_DECIMALS: u8 = 11``), not 9.

Naming gotcha (wire-verified): this Orquestra project speaks camelCase for BOTH the
instruction path (``/instructions/claimOre/build``) and the account names
(``treasuryTokens``, ``systemProgram``, ``oreProgram``) — like metadao, unlike the
pump/meteora projects' snake_case.

Run it:
    claude mcp add orquestra-ore -- \\
        uvx --from "gecko-surf[serve,solana]" gecko-orquestra --program ore --stdio
"""

from __future__ import annotations

from typing import Any, Mapping

from ..find_start import GapSpec, PreludeSpec, StartSpec
from ..landing import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    COMPUTE_BUDGET_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from ..ore_state import (
    DENOMINATOR_BPS,
    TOKEN_DECIMALS,
    MinerAccountState,
    TreasuryAccountState,
    read_miner_state,
    read_treasury_state,
)
from ..pda import derive_pda
from ..pda_testkit import LOCAL_RPC, RpcCall
from ..provider_config import load_packaged_provider
from .orquestra import Intent, OrquestraProgramSurface

__all__ = [
    "ORE_PROGRAM_ID",
    "ORE_MINT",
    "BUILD_URL",
    "CLAIM_DISCRIMINATOR",
    "ACCOUNT_METAS",
    "ORE_INTENTS",
    "ORE_STARTS",
    "OrePlanError",
    "claim_instruction_data",
    "plan_claim",
    "build_ore_program_surface",
]

# Display constants. The authoritative program id + PDA recipes live in the packaged
# config (gecko/providers/configs/orquestra/ore.json) — data, not code.
ORE_PROGRAM_ID = "oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv"
# api/src/consts.rs MINT_ADDRESS — a pinned constant, NOT a PDA. The shipped idl.json
# carries it as an `address`; the live Orquestra instruction surface drops that field,
# leaving `mint` with neither a pda recipe nor an address for an agent to resolve.
ORE_MINT = "oreoU2P8bN6jkk3jbaiVxYnG1dCXcYxwhwyK9jSybcp"
# api/src/instruction.rs — OreInstruction::ClaimORE = 4 (Steel: a single leading byte).
CLAIM_DISCRIMINATOR = 4
BUILD_URL = (
    "https://api.orquestra.dev/api/6alwvs9936laepljczqumb/instructions/claimOre/build"
)

# The SOURCE-TRUE account metas of `claimOre` — (is_signer, is_writable) in the source's
# account order (api/src/sdk.rs::claim_ore), cross-checked against real mainnet claimOre
# instructions. `board` is the one the builder's surface gets WRONG (it declares
# isMut: false); see _BOARD_META_GAP_NOTE. Data, not logic.
ACCOUNT_METAS: dict[str, dict[str, bool]] = {
    "signer": {"is_signer": True, "is_writable": True},
    "board": {"is_signer": False, "is_writable": True},
    "miner": {"is_signer": False, "is_writable": True},
    "mint": {"is_signer": False, "is_writable": False},
    "recipient": {"is_signer": False, "is_writable": True},
    "treasury": {"is_signer": False, "is_writable": True},
    "treasuryTokens": {"is_signer": False, "is_writable": True},
    "systemProgram": {"is_signer": False, "is_writable": False},
    "tokenProgram": {"is_signer": False, "is_writable": False},
    "associatedTokenProgram": {"is_signer": False, "is_writable": False},
    "oreProgram": {"is_signer": False, "is_writable": False},
}

# The one canonical note about the dropped arg — plan_claim, the StartSpec and the
# orchestrator all reuse it (refactor here, never in three places).
_BPS_GAP_NOTE = (
    "claimOre takes an OPTIONAL `bps` arg in source (ClaimORE { bps: [u8; 8] }, "
    "api/src/instruction.rs; omitted data = DENOMINATOR_BPS = 100%), but the shipped "
    "idl.json and the live Orquestra surface both declare args: []. POST "
    "/instructions/claimOre/build returns data '04' even when bps is supplied "
    "(wire-verified) — so a partial-claim ask silently becomes a full claim. Gecko "
    "declares the source-true bytes; the landing orchestrator refuses to simulate a "
    "bps the builder cannot carry rather than land the wrong semantics."
)

_CHECKPOINT_GAP_NOTE = (
    "checkpoint — the instruction that MOVES a round's winnings into miner.rewards_ore, "
    "i.e. claim's precondition — takes 8 accounts on mainnet (193/193 sampled), "
    "including a SEPARATE `authority` (which seeds the miner PDA, not the signer) and "
    "`automation`. The live surface declares 6 and documents the miner seed as the "
    "signer. Building checkpoint from that surface fails with NotEnoughAccountKeys. "
    "Not wired here — declared so a claim plan does not pretend the precondition is free."
)

_BOARD_META_GAP_NOTE = (
    "`board` must be WRITABLE. The shipped idl.json and the live Orquestra surface both "
    "declare isMut: false, and /build emits it read-only — but claim_ore ends with "
    "program_log, a self-CPI whose inner `log` instruction takes the board as a WRITABLE "
    "signer (api/src/sdk.rs: log() = AccountMeta::new(signer, true), invoked via "
    "invoke_signed with the BOARD seed). Simulating the builder's version fails with "
    "\"BrcSxdp…'s writable privilege escalated / Cross-program invocation with "
    'unauthorized signer or writable account" AFTER the token transfer has already '
    "succeeded — a failure no static surface reveals. sdk::claim_ore marks it "
    "AccountMeta::new(board, false) (writable) and every real mainnet claimOre carries "
    "it writable. Gecko reconciles the meta from source before simulating."
)

_RECIPIENT_NOTE = (
    "the signer's ORE associated-token account (mint oreoU2P8…, classic SPL Token). "
    "NO prelude is needed: claim_ore calls create_associated_token_account itself when "
    "recipient is empty, with the signer as payer (source-verified) — the same class as "
    "MetaDAO's init_if_needed funding_record. The live surface gives this account "
    "neither a pda recipe nor an address."
)

_TREASURY_TOKENS_NOTE = (
    "the treasury PDA's own ORE ATA — a TWO-STEP derivation (treasury = PDA(['treasury'], "
    "ORE), then ATA(treasury, mint)) that no IDL/llms.txt surface expresses; the live "
    "Orquestra surface reports pda: null for it."
)

_MINT_NOTE = (
    "a pinned CONSTANT (api/src/consts.rs MINT_ADDRESS), not a PDA. The shipped "
    "idl.json carries it as an `address`; the live Orquestra instruction surface drops "
    "that field, so an agent reading the surface alone has nothing to resolve."
)


class OrePlanError(Exception):
    """A claim-planning failure — missing bindings, or a signer that is not the miner's
    authority. Messages carry only public data (addresses, names), never a secret."""


def _load_ore_pdas() -> dict[str, Any]:
    _, apis = load_packaged_provider("orquestra")
    program = apis["ore"].program
    if program is None:
        raise OrePlanError("ore config carries no program spec")
    return dict(program.pdas)


def claim_instruction_data(bps: int) -> str:
    """The SOURCE-TRUE ``claimOre`` instruction data, hex.

    ``bps == DENOMINATOR_BPS`` (100%) is emitted as the bare discriminator ``"04"`` —
    the canonical omit form every full-claim client sends, which ``process_claim_ore``
    reads as 100% because ``ClaimORE::try_from_bytes`` fails on empty data. Anything
    less appends the ``u64`` little-endian ``bps``, exactly like ``sdk::claim_ore``.
    """
    head = bytes([CLAIM_DISCRIMINATOR])
    if bps >= DENOMINATOR_BPS:
        return head.hex()
    import struct

    return (head + struct.pack("<Q", bps)).hex()


def _landing_plan(accounts: Mapping[str, str], bps: int) -> list[dict[str, Any]]:
    """The DECLARED ordered instruction plan a real builder (Orquestra) assembles +
    1claw signs — pure structured data (no bytes signed, nothing sent):
    ``[SetComputeUnitLimit, SetComputeUnitPrice, claimOre]``.

    There is deliberately **no** ``create_idempotent_ata`` prelude: ``claim_ore``
    creates the recipient ATA itself when it is empty (source-verified). Compute-budget
    values are filled by the simulate step. See
    :func:`gecko.providers.ore_landing.simulate_claim_landing`.
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
            "kind": "claimOre",
            "program": ORE_PROGRAM_ID,
            "accounts": dict(accounts),
            "data": claim_instruction_data(bps),
            "metas": ACCOUNT_METAS,
            "note": (
                "Orquestra builds this instruction; Gecko reconciles two things the "
                "builder gets wrong before it can land — `board` must be WRITABLE (the "
                "surface says read-only; the program's closing self-CPI needs it), and "
                "the data must carry `bps` for anything but a full claim (the builder "
                "emits '04' unconditionally). The recipient ATA is created by the "
                "program itself when missing, so no ATA prelude is declared."
            ),
        },
    ]


def plan_claim(
    bindings: Mapping[str, Any],
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
) -> dict[str, Any]:
    """Assemble the full 11-account set for an ORE ``claimOre`` → an Orquestra
    ``/build`` payload plus the ordered landing plan.

    ``bindings`` needs ``signer`` (the miner's own authority — see below). Optional
    ``bps`` is the percentage to claim in basis points (default ``10000`` = 100%);
    optional ``authority`` is accepted only when it EQUALS the signer, so the
    authority/signer confusion fails loud instead of deriving a miner the program
    rejects.

    TWO control-plane reads happen (the miner + treasury accounts — public metadata,
    never stored): they prove the miner exists, yield the claimable balances (``at
    least`` — see :meth:`~gecko.ore_state.MinerAccountState.claim_preview`), confirm
    the signer really is ``miner.authority``, and declare whether a ``checkpoint`` is
    still pending. Account keys use the Orquestra project's exact (camelCase) names.
    """
    if "signer" not in bindings:
        raise OrePlanError("plan_claim needs bindings ['signer']")
    signer = str(bindings["signer"])
    bps = int(bindings.get("bps", DENOMINATOR_BPS))
    if not 0 < bps <= DENOMINATOR_BPS:
        raise OrePlanError(
            f"bps must be in 1..{DENOMINATOR_BPS} (basis points; {DENOMINATOR_BPS} = "
            f"100%) — got {bps}"
        )
    declared_authority = bindings.get("authority")
    if declared_authority is not None and str(declared_authority) != signer:
        # THE Class-1 guard. For claimOre the program seeds miner by the SIGNER and
        # asserts miner.authority == signer — so a claim on someone else's behalf is
        # not a different derivation, it is impossible. Never silently derive it.
        raise OrePlanError(
            "claimOre cannot claim on another authority's behalf: source binds miner "
            "with has_seeds([MINER, signer]) AND asserts miner.authority == signer "
            "(program/src/claim_ore.rs). Pass authority == signer, or omit it. (The "
            "authority/signer split IS real in deploy/checkpoint, which take a "
            "separate authority account — not here.)"
        )

    pdas = _load_ore_pdas()

    # (1) derive: the const singletons, the per-authority miner, and the two ATAs the
    # live surface reports `pda: null` for (recipient, treasuryTokens).
    board = derive_pda(pdas["board"], {}).address
    treasury = derive_pda(pdas["treasury"], {}).address
    miner = derive_pda(pdas["miner"], {"authority": signer}).address
    recipient = derive_pda(
        pdas["recipient"],
        {"signer": signer, "token_program": TOKEN_PROGRAM_ID, "mint": ORE_MINT},
    ).address
    treasury_tokens = derive_pda(
        pdas["treasury_tokens"],
        {"treasury": treasury, "token_program": TOKEN_PROGRAM_ID, "mint": ORE_MINT},
    ).address

    # (2) the state reads: existence + the authority assertion + the claimable floor.
    miner_state: MinerAccountState = read_miner_state(
        miner, rpc_url=rpc_url, rpc_call=rpc_call
    )
    if miner_state.authority != signer:
        raise OrePlanError(
            f"miner {miner} is owned by a different authority — claim_ore asserts "
            "miner.authority == signer and would revert"
        )
    treasury_state: TreasuryAccountState = read_treasury_state(
        treasury, rpc_url=rpc_url, rpc_call=rpc_call
    )
    amount, fee = miner_state.claim_preview(treasury_state, bps)

    # Keys are the Orquestra project's EXACT account names — camelCase here
    # (wire-verified), in the source's account order.
    accounts: dict[str, str] = {
        "signer": signer,
        "board": board,
        "miner": miner,
        "mint": ORE_MINT,
        "recipient": recipient,
        "treasury": treasury,
        "treasuryTokens": treasury_tokens,
        "systemProgram": SYSTEM_PROGRAM_ID,
        "tokenProgram": TOKEN_PROGRAM_ID,
        "associatedTokenProgram": ASSOCIATED_TOKEN_PROGRAM_ID,
        "oreProgram": ORE_PROGRAM_ID,
    }

    return {
        "instruction": "claimOre",
        "accounts": accounts,
        # What /build can actually carry today: nothing. The source-true bytes ride
        # alongside so a builder that WANTS to honour bps has them.
        "args": {},
        "requested_bps": bps,
        "instruction_data": claim_instruction_data(bps),
        # The SOURCE-TRUE metas (api/src/sdk.rs::claim_ore), cross-checked against real
        # mainnet claimOre instructions. `board` is where the builder's surface and the
        # deployed program disagree — see gaps.board_meta.
        "account_metas": ACCOUNT_METAS,
        "args_note": (
            f"claimOre takes NO amount — it harvests the miner account. The optional "
            f"`bps` (basis points, {DENOMINATOR_BPS} = 100%) selects a FRACTION; the "
            f"builder drops it (see gaps.bps). Payout is in ORE base units at "
            f"{TOKEN_DECIMALS} decimals: 1 ORE = 10^{TOKEN_DECIMALS}."
        ),
        "feePayer": signer,
        "build_url": BUILD_URL,
        # The declared claim verdict from the state reads — the program's own gates,
        # checked at plan time instead of revert time.
        "miner_state": {
            "authority": miner_state.authority,
            "refined_ore": miner_state.refined_ore,
            "rewards_ore": miner_state.rewards_ore,
            "claimable_ore": miner_state.claimable_ore,
            "claim_amount_at_least": amount,
            "refining_fee": fee,
            "decimals": TOKEN_DECIMALS,
            "checkpoint_pending": miner_state.checkpoint_pending,
            "round_id": miner_state.round_id,
            "checkpoint_id": miner_state.checkpoint_id,
            "note": (
                "claim_amount_at_least is a FLOOR: claim_ore runs update_rewards first, "
                "which can credit further refining rewards (fixed-point Numeric math "
                "Gecko does not guess at). The 10% refining fee applies to the "
                "unrefined (rewards_ore) portion only."
            ),
        },
        "preconditions": {
            "signer": (
                "must BE the miner's authority — claim_ore seeds miner by the signer "
                "and asserts miner.authority == signer (program/src/claim_ore.rs)"
            ),
            "checkpoint": _CHECKPOINT_GAP_NOTE,
            "recipient": _RECIPIENT_NOTE,
        },
        "gaps": {
            "bps": _BPS_GAP_NOTE,
            "board_meta": _BOARD_META_GAP_NOTE,
            "checkpoint": _CHECKPOINT_GAP_NOTE,
        },
        "landing_plan": _landing_plan(accounts, bps),
        "simulate": {
            "after": "POST build_url to get the built claimOre instruction/tx",
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


def _claim_plan(
    surface: OrquestraProgramSurface, args: Mapping[str, Any]
) -> dict[str, str]:
    """Intent adapter for the MCP surface: run plan_claim and hand back the resolved
    accounts (the surface wraps them with the /build execute URL)."""
    plan = plan_claim(args)
    return plan["accounts"]


_CLAIM = Intent(
    name="plan_claim",
    instruction="claimOre",
    description=(
        "Plan an ORE claim: harvest the ORE token mining rewards accrued in a miner "
        "account into the miner's own wallet. Give signer (the wallet that mined — it "
        "MUST be the miner's authority) and optionally bps (basis points to claim, "
        "10000 = 100%). Gecko derives all 11 accounts, including the three the "
        "instruction surface leaves unresolvable — the pinned ORE mint, the signer's "
        "ORE associated-token account, and the treasury PDA's own ATA (a two-step "
        "derivation) — reads the miner account for the claimable balance (ORE has "
        "ELEVEN decimals), and declares the two honest gaps: the optional bps argument "
        "the builder silently drops, and the checkpoint precondition whose surface "
        "account set is short by two."
    ),
    inputs=("signer", "bps"),
    plan=_claim_plan,
)

# The code half of the config: the plan callables this program exposes, keyed by
# intent name. The config lists the intent NAMES; here we supply their derivation.
ORE_INTENTS: dict[str, Intent] = {_CLAIM.name: _CLAIM}

# What find_start declares about plan_claim: the derive set (config PDAs; `mint` is a
# pinned constant kept in place as a non-PDA slot), the recovered facts, the honest
# FLAGGED gaps, and the DECLARED landing preludes. Pure data.
ORE_STARTS: dict[str, StartSpec] = {
    "plan_claim": StartSpec(
        accounts=("board", "treasury", "miner", "mint", "recipient", "treasury_tokens"),
        recovered={
            "miner": (
                "['miner', authority] recovered from Steel source (api/src/state/mod.rs "
                "miner_pda) — for claimOre the authority IS the signer, because "
                "claim_ore both seeds by the signer and asserts miner.authority == "
                "signer. deploy/checkpoint are the opposite: a separate authority "
                "account seeds the miner (ore-cli PR #150's proof_authority confusion)"
            ),
            "mint": _MINT_NOTE,
            "recipient": _RECIPIENT_NOTE,
            "treasury_tokens": _TREASURY_TOKENS_NOTE,
            "board": (
                "['board'] — a const-seeded singleton (source api/src/consts.rs "
                "BOARD_ADDRESS pins the same address the seed derives)"
            ),
            "treasury": (
                "['treasury'] — a const-seeded singleton and the ATA owner the "
                "treasuryTokens derivation chains off"
            ),
        },
        gaps=(
            GapSpec("bps", _BPS_GAP_NOTE),
            GapSpec("board_meta", _BOARD_META_GAP_NOTE),
            GapSpec("checkpoint", _CHECKPOINT_GAP_NOTE),
        ),
        preludes=(
            PreludeSpec(
                kind="compute_budget",
                program=COMPUTE_BUDGET_PROGRAM_ID,
                note=(
                    "SetComputeUnitLimit (units from the simulate Receipt) + "
                    "SetComputeUnitPrice (getRecentPrioritizationFees). This is the "
                    "WHOLE prelude — no idempotent-ATA step is declared because "
                    "claim_ore creates the recipient ATA itself when it is empty"
                ),
            ),
        ),
    )
}


def build_ore_program_surface() -> OrquestraProgramSurface:
    """Build the ORE surface from packaged config (identity + PDA recipes) + the local
    intent registry (the plan callables)."""
    from .cli import build_surface_from_config

    return build_surface_from_config("orquestra", "ore", ORE_INTENTS)

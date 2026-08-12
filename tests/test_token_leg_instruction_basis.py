"""The ``instruction-trace`` token-leg basis — F1.

Every test is offline. Nothing signs, nothing broadcasts, no RPC is reached: the whole
point of the basis under test is that it reads a response the caller already holds.

THE ONE THAT MATTERS is ``test_a_delegate_draining_the_payer_inside_a_cpi_never_authorizes``.
The rest support it. F1 was specified as safe because it is fallback-only and can
therefore "only move refuse → (authorize | refuse)" — which is true, and which quietly
includes authorizing an unbounded drain. An authority-FILTERED movement produces a report
with no refusals, a report with no refusals is ``measured``, a measured report whose
outflows do not include the payer returns ``()``, and an empty tuple skips every
per-outflow check in the gate. Zero is an amount that passes every cap.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.simulate import (
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    TokenDeltaReport,
    TokenDeltaUnmeasurable,
    TokenMovement,
    TokenOutflow,
    parse_token_deltas_from_instructions,
)

PAYER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
DELEGATE = "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _checked(
    *,
    kind: str = "transferChecked",
    authority: str = PAYER,
    mint: str = MINT,
    amount: str = "25000000",
    decimals: int = 6,
    program: str = "spl-token",
) -> dict[str, Any]:
    return {
        "program": program,
        "programId": TOKEN_PROGRAM_ID,
        "parsed": {
            "type": kind,
            "info": {
                "source": "SourceAta1111111111111111111111111111111111",
                "destination": "DestAta111111111111111111111111111111111111",
                "mint": mint,
                "authority": authority,
                "tokenAmount": {
                    "amount": amount,
                    "decimals": decimals,
                    "uiAmount": 25.0,
                    "uiAmountString": "25",
                },
            },
        },
    }


def _value(*instructions: Any) -> dict[str, Any]:
    return {"innerInstructions": [{"index": 0, "instructions": list(instructions)}]}


# ----------------------------------------------------------------------------------------
# THE FALSIFIER
# ----------------------------------------------------------------------------------------


def test_a_delegate_draining_the_payer_inside_a_cpi_never_authorizes() -> None:
    """The hazard F1 would have shipped: a drain that sums to zero and passes every cap.

    Reachable without a second signer. The payer signed ``approve(delegate = PDA_X)`` in
    some earlier transaction; today's transaction is one allowlisted top-level call, and
    program X CPIs ``transferChecked`` out of the payer's ATA under ``PDA_X``. The
    transfer exists ONLY in ``innerInstructions`` and its authority is not the payer.

    Filtering it out yields a measured, refusal-free, empty report — an OBSERVED ZERO for
    a real drain. This asserts the report is unmeasurable instead, and that reading an
    amount off it RAISES rather than returning nothing.
    """
    report = parse_token_deltas_from_instructions(
        _value(_checked(authority=DELEGATE)), fee_payer=PAYER
    )
    assert report is not None
    assert report.status == "unmeasurable", (
        "a movement authorised by a delegate must refuse, never filter to an observed zero"
    )
    assert [refusal.reason for refusal in report.refusals] == ["authority-not-payer"]

    with pytest.raises(TokenDeltaUnmeasurable):
        report.outflows()


def test_the_drain_is_not_rescued_by_a_clean_movement_beside_it() -> None:
    """Fails closed as a WHOLE, like the arrays path.

    The tempting shape is "sum what we could attribute and refuse the rest", which reports
    the payer's own small transfer and stays silent about the delegate's large one.
    Nothing proves the refused movement is not the drain.
    """
    report = parse_token_deltas_from_instructions(
        _value(_checked(amount="1"), _checked(authority=DELEGATE, amount="999999999")),
        fee_payer=PAYER,
    )
    assert report is not None
    assert report.status == "unmeasurable"
    assert report.instruction_outflows == ()


# ----------------------------------------------------------------------------------------
# what it CAN price
# ----------------------------------------------------------------------------------------


def test_a_checked_transfer_the_payer_signed_is_priced() -> None:
    report = parse_token_deltas_from_instructions(_value(_checked()), fee_payer=PAYER)
    assert report is not None
    assert report.status == "measured"
    assert report.basis == "instruction-trace"
    assert report.outflows() == (
        TokenOutflow(
            mint=MINT, owner=PAYER, decimals=6, raw=25_000_000, ui="25.000000"
        ),
    )


def test_two_movements_of_one_mint_are_summed() -> None:
    report = parse_token_deltas_from_instructions(
        _value(_checked(amount="1000000"), _checked(amount="500000")), fee_payer=PAYER
    )
    assert report is not None
    assert report.outflows()[0].raw == 1_500_000


def test_a_burn_checked_is_an_outflow_too() -> None:
    """Burning is leaving. Pricing only transfers would let a burn read as zero."""
    report = parse_token_deltas_from_instructions(
        _value(_checked(kind="burnChecked", amount="7")), fee_payer=PAYER
    )
    assert report is not None
    assert report.status == "measured"
    assert report.outflows()[0].raw == 7


def test_token_2022_is_recognised_by_program_id_as_well_as_name() -> None:
    instruction = _checked(program="spl-token-2022")
    instruction["programId"] = TOKEN_2022_PROGRAM_ID
    report = parse_token_deltas_from_instructions(_value(instruction), fee_payer=PAYER)
    assert report is not None
    assert report.status == "measured"


def test_a_non_token_program_is_ignored_rather_than_refused() -> None:
    """A System transfer is not a token movement, and its absence here is a fact.

    Refusing on it would make the basis useless — every real transaction contains
    instructions that are not token instructions.
    """
    report = parse_token_deltas_from_instructions(
        _value({"program": "system", "programId": "11111111111111111111111111111111"}),
        fee_payer=PAYER,
    )
    assert report is not None
    assert report.status == "measured"
    assert report.outflows() == ()


# ----------------------------------------------------------------------------------------
# what it refuses, and WHY each one is not a skip
# ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    ["transfer", "burn", "closeAccount", "mintTo", "approve", "syncNative"],
)
def test_a_known_but_unpriceable_instruction_refuses(kind: str) -> None:
    """Not skipped. A skipped token instruction sums to zero and zero passes every cap.

    ``transfer`` is the important member: it is the most common SPL instruction there is,
    and ``jsonParsed`` gives it no mint and no decimals. Production decoders recover the
    mint from a map built out of ``preTokenBalances`` — which is precisely what is absent
    whenever this basis runs, so there is nothing to recover it from.
    """
    report = parse_token_deltas_from_instructions(
        _value(
            {
                "program": "spl-token",
                "programId": TOKEN_PROGRAM_ID,
                "parsed": {
                    "type": kind,
                    "info": {"authority": PAYER, "amount": "25000000"},
                },
            }
        ),
        fee_payer=PAYER,
    )
    assert report is not None
    assert report.status == "unmeasurable"
    assert report.refusals[0].reason == "instruction-not-priceable"


def test_an_instruction_outside_the_closed_allowlist_refuses() -> None:
    """The allowlist is CLOSED. "We have not heard of it" is not "it moved nothing"."""
    report = parse_token_deltas_from_instructions(
        _value(
            {
                "program": "spl-token",
                "programId": TOKEN_PROGRAM_ID,
                "parsed": {
                    "type": "someFutureInstruction",
                    "info": {"authority": PAYER},
                },
            }
        ),
        fee_payer=PAYER,
    )
    assert report is not None
    assert report.refusals[0].reason == "instruction-unrecognised"


def test_an_unparsed_token_instruction_refuses() -> None:
    """An opaque token instruction is not an inert one."""
    report = parse_token_deltas_from_instructions(
        _value({"program": "spl-token", "programId": TOKEN_PROGRAM_ID, "data": "abc"}),
        fee_payer=PAYER,
    )
    assert report is not None
    assert report.refusals[0].reason == "instruction-unrecognised"


def test_a_multisig_authority_refuses_rather_than_being_resolved() -> None:
    """A multisig states ``multisigAuthority`` + ``signers``, not ``authority``.

    Whether the payer is meaningfully the authority of an m-of-n set is a judgement, and
    the token leg of a simulation is not where it gets made.
    """
    instruction = _checked()
    del instruction["parsed"]["info"]["authority"]
    instruction["parsed"]["info"]["multisigAuthority"] = "Ms11111111111111111111111111"
    instruction["parsed"]["info"]["signers"] = [PAYER]
    report = parse_token_deltas_from_instructions(_value(instruction), fee_payer=PAYER)
    assert report is not None
    assert report.refusals[0].reason == "authority-not-payer"


def test_a_top_level_token_instruction_refuses_the_whole_report() -> None:
    """Constraint 4. ``innerInstructions`` carries only CPIs.

    A top-level SPL transfer is invisible here and would read as zero. Its amount cannot
    be recovered either: the local decoder keeps the selector and discards the argument
    bytes on purpose. Refusing is the only answer that neither holds payload nor reads a
    real transfer as nothing.
    """
    report = parse_token_deltas_from_instructions(
        _value(_checked()), fee_payer=PAYER, top_level_token_instructions=1
    )
    assert report is not None
    assert report.status == "unmeasurable"
    assert report.refusals[0].reason == "instruction-not-priceable"


def test_an_amount_is_read_as_a_string_and_never_through_a_float() -> None:
    """A u64 past 2^53 does not survive a JSON float — silently, and DOWNWARD.

    ``uiAmount`` is a JSON number. Reading it would lose precision in the direction a cap
    does not catch, so the decimal string is the only source.
    """
    big = str(2**63 - 1)
    instruction = _checked(amount=big)
    instruction["parsed"]["info"]["tokenAmount"]["uiAmount"] = 1.0
    instruction["parsed"]["info"]["tokenAmount"]["uiAmountString"] = "1"
    report = parse_token_deltas_from_instructions(_value(instruction), fee_payer=PAYER)
    assert report is not None
    assert report.outflows()[0].raw == 2**63 - 1


def test_a_non_numeric_amount_refuses() -> None:
    report = parse_token_deltas_from_instructions(
        _value(_checked(amount="not-a-number")), fee_payer=PAYER
    )
    assert report is not None
    assert report.refusals[0].reason == "malformed-balance"


# ----------------------------------------------------------------------------------------
# tracked / not-tracked / declined — the three states kept apart
# ----------------------------------------------------------------------------------------


def test_no_inner_instructions_key_is_NOT_TRACKED_and_not_zero() -> None:
    assert parse_token_deltas_from_instructions({}, fee_payer=PAYER) is None


def test_a_null_inner_instructions_is_a_declined_read_not_an_empty_one() -> None:
    report = parse_token_deltas_from_instructions(
        {"innerInstructions": None}, fee_payer=PAYER
    )
    assert report is not None
    assert report.status == "unmeasurable"
    assert report.refusals[0].reason == "balances-null"


def test_no_cpis_at_all_is_an_observed_zero() -> None:
    """Empty is a real answer here: the node said there were no inner instructions."""
    report = parse_token_deltas_from_instructions(
        {"innerInstructions": []}, fee_payer=PAYER
    )
    assert report is not None
    assert report.status == "measured"
    assert report.outflows() == ()


# ----------------------------------------------------------------------------------------
# the report cannot mix the two bases
# ----------------------------------------------------------------------------------------


def test_an_instruction_basis_report_may_not_carry_movements() -> None:
    """An instruction has no before-and-after, so a movement built from one is invented."""
    with pytest.raises(ValueError, match="direct outflows, not movements"):
        TokenDeltaReport(
            status="measured",
            movements=(
                TokenMovement(
                    mint=MINT,
                    owner=PAYER,
                    decimals=6,
                    pre_raw=1,
                    post_raw=0,
                    delta_raw=-1,
                    ui_delta="-0.000001",
                ),
            ),
            refusals=(),
            basis="instruction-trace",
        )


def test_a_balances_basis_report_may_not_carry_direct_outflows() -> None:
    with pytest.raises(ValueError, match="movements, not direct outflows"):
        TokenDeltaReport(
            status="measured",
            movements=(),
            refusals=(),
            basis="token-balances",
            instruction_outflows=(
                TokenOutflow(mint=MINT, owner=PAYER, decimals=6, raw=1, ui="0.000001"),
            ),
        )


def test_the_default_basis_is_the_strong_one() -> None:
    """A producer that never thought about provenance cannot claim the weaker source."""
    assert TokenDeltaReport(status="measured", movements=(), refusals=()).basis == (
        "token-balances"
    )

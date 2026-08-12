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
    TokenDeltaRefusal,
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


@pytest.mark.parametrize("kind", ["transfer", "burn", "closeAccount"])
def test_an_unpriceable_type_is_refused_even_when_the_NODE_supplies_the_missing_fields(
    kind: str,
) -> None:
    """The allowlist keys on the TYPE, and this is what proves it.

    Written because the test above turned out to be vacuous: it omits ``mint`` and
    ``tokenAmount``, so adding ``transfer`` to the priceable set left it green — the
    refusal arrived by the missing-field path instead, with the same reason code. A
    mutation that widened the allowlist was invisible.

    It also encodes a real property rather than just closing a test hole. Some RPC
    providers ENRICH a parsed ``transfer`` with the mint they looked up server-side.
    Pricing that would mean pricing a movement from a value the NODE supplied rather than
    one the instruction states — a node promoted to a trust root of a spend decision. The
    type is refused no matter how complete the object looks.
    """
    enriched = _checked(kind=kind)
    report = parse_token_deltas_from_instructions(_value(enriched), fee_payer=PAYER)
    assert report is not None
    assert report.status == "unmeasurable", (
        f"{kind} must be refused on its TYPE, even carrying a node-supplied mint and "
        f"tokenAmount"
    )
    assert report.refusals[0].reason == "instruction-not-priceable"
    assert report.instruction_outflows == ()


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


# ----------------------------------------------------------------------------------------
# "wired" is not "reaches the caller" — the selection rule, through simulate()
# ----------------------------------------------------------------------------------------


def test_the_arrays_win_whenever_they_speak() -> None:
    """Constraint 1. An arrays refusal must not be talked out of by an instruction sum.

    A Token-2022 transfer-fee refusal is the arrays saying "we saw something we will not
    reduce to a number". The instruction path cannot see that fee either, so letting it
    answer here would replace a correct refusal with a confident wrong number.
    """
    from gecko.simulate import _arrays_said_nothing

    spoke = TokenDeltaReport(
        status="unmeasurable",
        movements=(),
        refusals=(
            TokenDeltaRefusal(reason="transfer-fee", mint=MINT, detail="fee extension"),
        ),
    )
    assert _arrays_said_nothing(spoke) is False


@pytest.mark.parametrize(
    "report",
    [
        None,
        TokenDeltaReport(
            status="unmeasurable",
            movements=(),
            refusals=(
                TokenDeltaRefusal(reason="balances-null", mint=None, detail="declined"),
            ),
        ),
    ],
    ids=["key-absent", "present-and-null"],
)
def test_only_an_absent_or_declined_arrays_read_yields_to_the_fallback(
    report: TokenDeltaReport | None,
) -> None:
    """The two cases that are NOT "nothing moved", and the only two the fallback may cover."""
    from gecko.simulate import _arrays_said_nothing

    assert _arrays_said_nothing(report) is True


def test_a_measured_zero_from_the_arrays_is_not_yielded_to_the_fallback() -> None:
    """An observed zero is an ANSWER. Re-deriving it from instructions could only weaken it."""
    from gecko.simulate import _arrays_said_nothing

    assert (
        _arrays_said_nothing(
            TokenDeltaReport(status="measured", movements=(), refusals=())
        )
        is False
    )


def test_the_fee_payer_is_decoded_locally_and_never_read_off_the_response() -> None:
    """The node must not choose which account its own numbers get attributed to.

    ``_traced_token_delta`` takes the payer from the simulated BYTES. If it read a field
    off the response instead, a node could name the payer as the authority of every
    movement and every drain would attribute cleanly.
    """
    import inspect

    from gecko.simulate import _traced_token_delta

    source = inspect.getsource(_traced_token_delta)
    assert "decode_message" in source
    assert "built.tx" in source
    # No response field is consulted for identity — only the instruction list is read from
    # ``value``, and that happens inside the parser.
    for forbidden in ('value.get("feePayer")', 'value["feePayer"]', "accountKeys"):
        assert forbidden not in source


# ----------------------------------------------------------------------------------------
# F1e — the falsifier where it actually has to hold: simulate() -> Receipt -> the GATE
#
# Everything above tests the parser. A parser that refuses correctly and a signing path
# that authorizes anyway is the exact failure this project keeps naming: "wired" is not
# "reaches the caller". These two run the whole seam, and they are a matched pair — the
# refusal is only meaningful because the counterexample beside it is authorized on the
# same wiring, the same policy and the same ledger.
# ----------------------------------------------------------------------------------------


_DEX = "Vote111111111111111111111111111111111111111"
_DEST = "SysvarRent111111111111111111111111111111111"
_SWAP_DISC = bytes([0xA3, 0x34, 0xC8, 0xE7, 0x8C, 0x03, 0x45, 0xBA])


def _unsigned_tx() -> str:
    """One allowlisted NON-token top-level call, fee payer ``PAYER``, base64, unsigned.

    Non-token on purpose: the drain under test must exist ONLY as a CPI, which is the
    whole reason the balance arrays are the normal way to see it and the instruction trace
    is a fallback. A top-level token instruction refuses for a different reason and would
    make this pass for the wrong one.
    """
    import base64

    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    payer = Pubkey.from_string(PAYER)
    metas = [
        AccountMeta(payer, is_signer=True, is_writable=True),
        AccountMeta(Pubkey.from_string(_DEST), is_signer=False, is_writable=True),
    ]
    instruction = Instruction(Pubkey.from_string(_DEX), _SWAP_DISC + b"\x10" * 8, metas)
    message = Message.new_with_blockhash([instruction], payer, Hash.default())
    return base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()


def _receipt_from_inner(*instructions: Any) -> Any:
    """Run the REAL ``simulate()`` over a response that has CPIs and no balance arrays.

    No ``preTokenBalances``/``postTokenBalances`` key at all — the not-tracked state that
    stock ``simulateTransaction`` returns and the only state the fallback may cover.
    """
    from gecko.networks import UNKNOWN_NETWORK
    from gecko.simulate import BuiltTx, simulate

    encoded = _unsigned_tx()
    value = {
        "err": None,
        "unitsConsumed": 42_000,
        "logs": ["Program log: swap", "Program success"],
        "accounts": [{"lamports": 900}],
        "innerInstructions": [{"index": 0, "instructions": list(instructions)}],
    }

    def rpc(_url: str, method: str, _params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            return {"result": {"value": {"lamports": 1_000}}}
        if method == "simulateTransaction":
            return {"result": {"value": value}}
        raise AssertionError(f"unexpected method {method}")

    return (
        encoded,
        simulate(
            {"feePayer": PAYER, "accounts": {}, "args": {}},
            rpc_url="http://127.0.0.1:8899",
            rpc_call=rpc,
            build_call=lambda _plan: BuiltTx(tx=encoded, encoding="base64"),
            track=[PAYER],
            network=UNKNOWN_NETWORK,
        ),
    )


def _authorizing_gate() -> Any:
    """A policy that says YES to this exact transaction, so only the token leg can refuse.

    Every other predicate is satisfied deliberately: the program and discriminator are
    allowlisted, the destination is allowlisted, the mint is capped generously enough to
    admit the drain if an amount for it ever reached the caps. If this gate refuses, it
    refused on the token leg.
    """
    from gecko.spend_policy import (
        AllowedInstruction,
        InMemorySpendLedger,
        SpendPolicy,
        SpendPolicyGate,
        TokenCap,
        TokenCaps,
    )

    return SpendPolicyGate(
        policy=SpendPolicy(
            authorized=True,
            per_transaction_cap_lamports=100_000_000,
            hourly_cap_lamports=1_000_000_000,
            daily_cap_lamports=1_000_000_000,
            max_transactions_per_day=5,
            allowed_instructions=frozenset(
                {AllowedInstruction(program_id=_DEX, discriminator=_SWAP_DISC)}
            ),
            allowed_destinations=frozenset({_DEST}),
            token_caps=TokenCaps.of(
                (
                    TokenCap(
                        mint=MINT,
                        decimals=6,
                        per_transaction_raw=100_000_000,
                        hourly_raw=100_000_000,
                        daily_raw=100_000_000,
                    ),
                )
            ),
        ),
        ledger=InMemorySpendLedger(),
    )


def test_the_cpi_drain_reaches_the_gate_as_a_refusal_and_never_as_a_zero() -> None:
    """F1e end to end. The delegate drain must come out of the SEAM unauthorized.

    Without the authority check this is the shipped hazard in full: the report is
    ``measured`` with no outflows, the gate reads an observed zero, zero passes every cap,
    and a transaction that empties the payer's ATA is authorized for signing.
    """
    encoded, receipt = _receipt_from_inner(_checked(authority=DELEGATE))

    assert receipt.token_delta is not None
    assert receipt.token_delta.basis == "instruction-trace", (
        "the arrays were absent, so this number can only have come from the CPI trace"
    )
    assert receipt.token_delta.status == "unmeasurable"

    verdict = _authorizing_gate().authorize(encoded, receipt, now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "amount-unresolvable"


def test_the_same_wiring_authorizes_when_the_payer_is_the_authority() -> None:
    """The counterexample. Without it, the test above passes on a gate that refuses all.

    Same transaction, same policy, same fallback path, one field different — and now a
    real 25 USDC outflow is priced and charged. This is what proves F1 RECOVERED a
    measurement rather than merely adding a new way to say no.
    """
    encoded, receipt = _receipt_from_inner(_checked(authority=PAYER))

    assert receipt.token_delta is not None
    assert receipt.token_delta.basis == "instruction-trace"
    assert receipt.token_delta.status == "measured"

    verdict = _authorizing_gate().authorize(encoded, receipt, now=1_000.0)
    assert verdict.authorized is True, verdict.reason
    assert [(spend.mint, spend.raw) for spend in verdict.outflow_tokens] == [
        (MINT, 25_000_000)
    ]

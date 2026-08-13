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

from collections.abc import Mapping
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


def _parse(value: Mapping[str, Any], **over: Any) -> TokenDeltaReport | None:
    """The parser with the message context a real caller always has.

    ``account_keys`` is required on the production signature — a default there would let a
    caller who never thought about mint corroboration look like one who did. Tests want
    the ordinary case to stay one line, so the ordinary case lives here: a message that
    references the payer and the mint, which is what any real transaction moving that
    mint does. Tests about the check itself pass their own.
    """
    over.setdefault("fee_payer", PAYER)
    over.setdefault("account_keys", [PAYER, MINT])
    return parse_token_deltas_from_instructions(value, **over)


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
    report = _parse(_value(_checked(authority=DELEGATE)), fee_payer=PAYER)
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
    report = _parse(
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
    report = _parse(_value(_checked()), fee_payer=PAYER)
    assert report is not None
    assert report.status == "measured"
    assert report.basis == "instruction-trace"
    assert report.outflows() == (
        TokenOutflow(
            mint=MINT, owner=PAYER, decimals=6, raw=25_000_000, ui="25.000000"
        ),
    )


def test_two_movements_of_one_mint_are_summed() -> None:
    report = _parse(
        _value(_checked(amount="1000000"), _checked(amount="500000")), fee_payer=PAYER
    )
    assert report is not None
    assert report.outflows()[0].raw == 1_500_000


def test_a_burn_checked_is_an_outflow_too() -> None:
    """Burning is leaving. Pricing only transfers would let a burn read as zero."""
    report = _parse(_value(_checked(kind="burnChecked", amount="7")), fee_payer=PAYER)
    assert report is not None
    assert report.status == "measured"
    assert report.outflows()[0].raw == 7


def test_token_2022_is_recognised_by_program_id_as_well_as_name() -> None:
    """Recognised — and then held to the SAME extension rule the arrays basis applies.

    Extension evidence is supplied here because without it the answer is a refusal, which
    is the subject of the test immediately below.
    """
    instruction = _checked(program="spl-token-2022")
    instruction["programId"] = TOKEN_2022_PROGRAM_ID
    report = _parse(_value(instruction), fee_payer=PAYER, mint_extensions={MINT: []})
    assert report is not None
    assert report.status == "measured"


def test_a_token_2022_mint_whose_extensions_were_never_read_refuses() -> None:
    """The weaker basis does not get to repeal "unread is not none".

    Measured before the fix: a Token-2022 ``burnChecked`` priced at 25 USDC on ZERO
    extension evidence, because the fallback never received ``mint_extensions``. Several
    extensions — transfer fees above all — make the delta not the debit, so pricing one
    unread states a number that is wrong in the direction a cap does not catch.
    """
    instruction = _checked(program="spl-token-2022")
    instruction["programId"] = TOKEN_2022_PROGRAM_ID
    report = _parse(_value(instruction), fee_payer=PAYER)
    assert report is not None
    assert report.status == "unmeasurable"
    assert [refusal.reason for refusal in report.refusals] == [
        "token-2022-extensions-unread"
    ]


def test_a_node_naming_a_mint_the_transaction_never_references_refuses() -> None:
    """The mint is NODE-AUTHORED under this basis, so it is corroborated against the bytes.

    Measured before the fix: the node names an allowlisted, generously capped mint while
    the real movement is a different one, and the gate authorises it — ``mint-not-
    allowlisted`` converted into an authorised spend by a string. A CPI can only touch
    accounts the outer message references, so membership is a sound local check.
    """
    report = _parse(_value(_checked()), fee_payer=PAYER, account_keys=[PAYER, DELEGATE])
    assert report is not None
    assert report.status == "unmeasurable"
    assert [refusal.reason for refusal in report.refusals] == [
        "instruction-not-priceable"
    ]
    assert "does not reference" in report.refusals[0].detail


def test_the_mint_check_passes_when_the_transaction_does_reference_it() -> None:
    """The counterexample: the check is a corroboration, not a blanket refusal."""
    report = _parse(_value(_checked()), fee_payer=PAYER, account_keys=[PAYER, MINT])
    assert report is not None
    assert report.status == "measured"


def test_a_program_name_contradicting_its_program_id_refuses() -> None:
    """Two answers to "which token program ran this" is not one answer.

    It decides whether the mint may carry extensions, so resolving the contradiction
    either way picks a rule on the node's behalf.
    """
    instruction = _checked(program="spl-token-2022")
    instruction["programId"] = TOKEN_PROGRAM_ID
    report = _parse(_value(instruction), fee_payer=PAYER, mint_extensions={MINT: []})
    assert report is not None
    assert report.status == "unmeasurable"
    assert [refusal.reason for refusal in report.refusals] == [
        "instruction-unrecognised"
    ]


@pytest.mark.parametrize(
    ("amount", "decimals"),
    [("\u00b2", 6), ("1", 10**6)],
    ids=["superscript-digit-does-not-raise", "unbounded-decimals-does-not-hang"],
)
def test_a_hostile_amount_field_refuses_rather_than_escaping(
    amount: str, decimals: int
) -> None:
    """Two node-triggered faults that were not refusals. Both measured before the fix.

    ``"²".isdigit()`` is ``True`` and ``int("²")`` raises, so the old predicate turned one
    character into a ``ValueError`` out of ``simulate()``. And ``decimals`` was unbounded
    while ``_render_ui`` computes ``10 ** decimals`` — 10^6 spent 0.116s building a
    one-megabyte string, 10^7 spent 4.8s. The arrays path had already bounded both.
    """
    report = _parse(
        _value(
            _checked(
                amount=amount,
                decimals=decimals,
            )
        ),
        fee_payer=PAYER,
    )
    assert report is not None
    assert report.status == "unmeasurable"
    assert [refusal.reason for refusal in report.refusals] == ["malformed-balance"]


def test_a_non_token_program_is_ignored_rather_than_refused() -> None:
    """A System transfer is not a token movement, and its absence here is a fact.

    Refusing on it would make the basis useless — every real transaction contains
    instructions that are not token instructions.
    """
    report = _parse(
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
    report = _parse(
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
    report = _parse(_value(enriched), fee_payer=PAYER)
    assert report is not None
    assert report.status == "unmeasurable", (
        f"{kind} must be refused on its TYPE, even carrying a node-supplied mint and "
        f"tokenAmount"
    )
    assert report.refusals[0].reason == "instruction-not-priceable"
    assert report.instruction_outflows == ()


def test_an_instruction_outside_the_closed_allowlist_refuses() -> None:
    """The allowlist is CLOSED. "We have not heard of it" is not "it moved nothing"."""
    report = _parse(
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
    report = _parse(
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
    report = _parse(_value(instruction), fee_payer=PAYER)
    assert report is not None
    assert report.refusals[0].reason == "authority-not-payer"


def test_a_top_level_token_instruction_refuses_the_whole_report() -> None:
    """Constraint 4. ``innerInstructions`` carries only CPIs.

    A top-level SPL transfer is invisible here and would read as zero. Its amount cannot
    be recovered either: the local decoder keeps the selector and discards the argument
    bytes on purpose. Refusing is the only answer that neither holds payload nor reads a
    real transfer as nothing.
    """
    report = _parse(_value(_checked()), fee_payer=PAYER, top_level_token_instructions=1)
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
    report = _parse(_value(instruction), fee_payer=PAYER)
    assert report is not None
    assert report.outflows()[0].raw == 2**63 - 1


def test_a_non_numeric_amount_refuses() -> None:
    report = _parse(_value(_checked(amount="not-a-number")), fee_payer=PAYER)
    assert report is not None
    assert report.refusals[0].reason == "malformed-balance"


# ----------------------------------------------------------------------------------------
# tracked / not-tracked / declined — the three states kept apart
# ----------------------------------------------------------------------------------------


def test_no_inner_instructions_key_is_NOT_TRACKED_and_not_zero() -> None:
    assert _parse({}, fee_payer=PAYER) is None


def test_a_null_inner_instructions_is_a_declined_read_not_an_empty_one() -> None:
    report = _parse({"innerInstructions": None}, fee_payer=PAYER)
    assert report is not None
    assert report.status == "unmeasurable"
    assert report.refusals[0].reason == "balances-null"


def test_no_cpis_at_all_is_an_observed_zero() -> None:
    """Empty is a real answer here: the node said there were no inner instructions."""
    report = _parse({"innerInstructions": []}, fee_payer=PAYER)
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


def test_the_named_residuals_survive_in_the_docstring() -> None:
    """G3's lesson, applied here. A residual nobody pins is a residual somebody deletes.

    These are limits of the SOURCE, not gaps in the reading, and they are the difference
    between "the token leg was measured" and "the token leg was measured, and here is what
    that sentence does not cover". Asserting on the word "residual" alone was measured to
    be insufficient in ``gecko/spend_policy.py``: the label survived while the section
    under it was deleted. So each claim is pinned by its own words.
    """
    import re

    from gecko.simulate import parse_token_deltas_from_instructions as parser

    doc = re.sub(r"\s+", " ", parser.__doc__ or "")
    for claim in (
        "THE AMOUNT REMAINS NODE-AUTHORED",
        "AN OMITTED CPI READS AS AN OBSERVED ZERO",
        "A ZERO HERE IS WEAKER THAN A ZERO THERE",
        "ONLY THE ``*Checked`` VARIANTS ARE PRICEABLE",
        "PAIRING THE RECEIPT TO THE RIGHT BYTES IS THE OTHER GATE'S JOB",
    ):
        assert claim in doc, f"the residual {claim!r} was deleted from the docstring"


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


def test_only_a_wholly_absent_arrays_read_yields_to_the_fallback() -> None:
    """The ONE case the fallback may cover: the key was never there."""
    from gecko.simulate import _arrays_said_nothing

    assert _arrays_said_nothing(None) is True


def test_a_nulled_array_does_not_yield_to_the_fallback() -> None:
    """The hole this closed, and it is worth stating as its own test.

    ``parse_token_deltas`` checks null FIRST and returns, so ONE nulled array suppresses
    every stronger refusal the arrays would have raised. Measured: a node returning
    ``preTokenBalances: null`` beside a ``postTokenBalances`` showing the payer's
    Token-2022 account falling from 100 USDC to zero downgraded a hard
    ``token-2022-extensions-unread`` refusal into an authorised 1 USDC spend. The node
    chose which basis measured it, and picked the one with less corroboration.

    Closing it costs nothing: stock ``simulateTransaction`` OMITS the keys, it does not
    null them, so no working case stops working.
    """
    from gecko.simulate import _arrays_said_nothing

    nulled = TokenDeltaReport(
        status="unmeasurable",
        movements=(),
        refusals=(
            TokenDeltaRefusal(reason="balances-null", mint=None, detail="declined"),
        ),
    )
    assert _arrays_said_nothing(nulled) is False


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
        # The MINT, because a CPI can only touch accounts the outer message references and
        # the parser now corroborates the node's mint string against exactly that set.
        AccountMeta(Pubkey.from_string(MINT), is_signer=False, is_writable=False),
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

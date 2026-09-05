"""The pre-signature effects summary.

Most of these tests are about what the summary REFUSES to say. A number nobody can
derive, printed in the sentence a reader sees before signing, is worse than a gap: a gap
prompts a question and a wrong number ends one.
"""

from __future__ import annotations

import pytest

from gecko.effects import Effects, describe_effects
from gecko.simulate import (
    Receipt,
    TokenDeltaRefusal,
    TokenDeltaReport,
    TokenMovement,
)
from gecko.txbind import DecodedInstruction, DecodedMessage

PAYER = "Payer1111111111111111111111111111111111111"
STORE = "Store1111111111111111111111111111111111111"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"


def _message(*programs: str) -> DecodedMessage:
    return DecodedMessage(
        version="legacy",
        fee_payer=PAYER,
        account_keys=(PAYER, STORE, USDC),
        writable_accounts=frozenset({PAYER, STORE}),
        instructions=tuple(
            DecodedInstruction(
                program_id=program,
                discriminator=b"\x01",
                data_length=9,
                account_indexes=(0, 1),
            )
            for program in programs
        ),
    )


def _receipt(**overrides: object) -> Receipt:
    base: dict[str, object] = {
        "status": "pass",
        "err": None,
        "revert_class": None,
        "units_consumed": 42_375,
        "sol_delta": -5_000,
        "tokens_received": (),
        "logs_tail": (),
        "network_label": "surfnet fork of mainnet",
        "origin": "simulated",
        "observed_slot": 440_510_159,
    }
    base.update(overrides)
    return Receipt(**base)  # type: ignore[arg-type]


def _moved(delta_raw: int, decimals: int = 6) -> TokenDeltaReport:
    return TokenDeltaReport(
        status="measured",
        movements=(
            TokenMovement(
                mint=USDC,
                owner=PAYER,
                decimals=decimals,
                pre_raw=1_000_000,
                post_raw=1_000_000 + delta_raw,
                delta_raw=delta_raw,
                ui_delta="-0.05",
            ),
        ),
        refusals=(),
    )


def test_it_renders_the_mint_and_amount_that_actually_left() -> None:
    effects = describe_effects(
        _message(COMPUTE_BUDGET, TOKEN_PROGRAM), _receipt(token_delta=_moved(-50_000))
    )
    assert len(effects.tokens_out) == 1
    movement = effects.tokens_out[0]
    assert movement.mint == USDC
    assert movement.owner == PAYER
    assert movement.raw == 50_000, "the raw magnitude the program moves, positive"
    assert movement.decimals == 6
    assert effects.unmeasured == ()


def test_programs_keep_call_order_and_are_not_resorted() -> None:
    """The order an instruction list is written in is a fact about the transaction."""
    effects = describe_effects(
        _message(COMPUTE_BUDGET, TOKEN_PROGRAM, COMPUTE_BUDGET),
        _receipt(token_delta=_moved(-1)),
    )
    assert effects.programs == (COMPUTE_BUDGET, TOKEN_PROGRAM), "deduped, not sorted"


def test_an_unmeasurable_token_leg_says_so_instead_of_reporting_zero() -> None:
    """One refused mint makes the whole leg unmeasurable — nothing proves it is not the drain."""
    unmeasurable = TokenDeltaReport(
        status="unmeasurable",
        movements=(),
        refusals=(
            TokenDeltaRefusal(
                mint=USDC,
                reason="transfer-fee",
                detail="the observed delta is not the debit",
            ),
        ),
    )
    effects = describe_effects(
        _message(TOKEN_PROGRAM), _receipt(token_delta=unmeasurable)
    )

    assert effects.tokens_out == (), "no movement may be claimed"
    assert effects.unmeasured, "and the reader must be told why"
    assert "token movements" in effects.unmeasured[0]
    # The distinction that matters: absent, never a rendered zero.
    assert "tokens_out" not in effects.as_dict()


def test_no_token_balances_at_all_is_distinct_from_a_measured_zero() -> None:
    no_report = describe_effects(_message(TOKEN_PROGRAM), _receipt(token_delta=None))
    measured_zero = describe_effects(
        _message(TOKEN_PROGRAM),
        _receipt(
            token_delta=TokenDeltaReport(status="measured", movements=(), refusals=())
        ),
    )
    assert no_report.unmeasured, "a node that returned nothing is not an observed zero"
    assert measured_zero.unmeasured == (), "an observed zero is a measurement"
    assert measured_zero.tokens_out == ()


def test_the_summary_inherits_the_receipts_standing() -> None:
    """A description of a simulated future is only as good as the simulation."""
    asserted = describe_effects(_message(TOKEN_PROGRAM), _receipt(origin="asserted"))
    assert asserted.origin == "asserted"
    assert asserted.as_dict()["origin"] == "asserted", (
        "a reader must be able to tell an observation from an assertion"
    )


def test_absent_facts_are_omitted_from_the_dict_never_nulled() -> None:
    """A null reads as 'the answer is nothing'; a missing key reads as 'not established'."""
    effects = Effects(fee_payer=PAYER, programs=(TOKEN_PROGRAM,))
    payload = effects.as_dict()
    for key in ("sol_delta", "compute_units", "observed_slot", "network", "tokens_out"):
        assert key not in payload, f"{key} was not established and must not appear"
    assert payload["fee_payer"] == PAYER


@pytest.mark.parametrize(
    ("lamports", "rendered"),
    [
        (-5_000, "-0.000005"),
        (1_000_000_000, "1"),
        (-1_234_500_000, "-1.2345"),
        (0, "0"),
        (1, "0.000000001"),
    ],
)
def test_sol_is_rendered_exactly_never_rounded(lamports: int, rendered: str) -> None:
    """The reader is about to sign this; a rounded amount is a different amount."""
    effects = describe_effects(_message(TOKEN_PROGRAM), _receipt(sol_delta=lamports))
    assert effects.sol_delta_ui == rendered


def test_the_wiring_attaches_effects_to_a_real_prepared_transaction() -> None:
    """`_effects_field` on bytes we can actually decode.

    The unit tests above check the composition; this checks the seam that puts it in
    front of an agent, over a genuinely serialized transaction rather than a fixture.
    """
    import base64

    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    from gecko.prepare_purchase import _effects_field

    payer = Pubkey.from_string("SysvarC1ock11111111111111111111111111111111")
    dest = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
    program = Pubkey.from_string("Vote111111111111111111111111111111111111111")
    instruction = Instruction(
        program,
        b"\x01" * 8,
        [
            AccountMeta(payer, is_signer=True, is_writable=True),
            AccountMeta(dest, is_signer=False, is_writable=True),
        ],
    )
    message = Message.new_with_blockhash([instruction], payer, Hash.default())
    tx = base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()

    field = _effects_field(tx, _receipt(token_delta=_moved(-50_000)))
    assert "effects" in field
    effects = field["effects"]
    assert effects["fee_payer"] == str(payer)
    assert effects["programs"] == [str(program)]
    assert effects["tokens_out"][0]["mint"] == USDC
    assert effects["origin"] == "simulated"


def test_undecodable_bytes_attach_no_effects_rather_than_an_empty_summary() -> None:
    """A sentence about bytes we could not read would be a guess wearing authority."""
    from gecko.prepare_purchase import _effects_field

    assert _effects_field("not-base64-at-all!!", _receipt()) == {}

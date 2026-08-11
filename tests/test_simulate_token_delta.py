"""G6 — a token-denominated delta a policy can read, or an explicit refusal.

``Receipt.tokens_received`` was hardcoded ``None``, so a USDC ``make_purchase`` reached a
lamport-denominated cap as ~0 outflow. These tests pin the replacement: a TYPED per-mint
delta parsed from the simulation value's ``pre``/``postTokenBalances``, carrying mint,
decimals, owner, the raw base-unit amount and a LOCALLY rendered ui amount — and, for
every Token-2022 case where the observed delta is not the debit, an explicit REFUSAL
instead of a number. A zero for "could not measure" is the bug, not the fix.

No network: every payload here is a light fake shaped like the RPC's own reply.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.networks import UNKNOWN_NETWORK
from gecko.simulate import (
    TOKEN_2022_PROGRAM_ID,
    TOKEN_DELTA_REFUSALS,
    TOKEN_PROGRAM_ID,
    BuiltTx,
    TokenDeltaReport,
    TokenDeltaUnmeasurable,
    parse_token_deltas,
    simulate,
)

USDC = "EPjFWdd5AujKTFTPu7c9EQoyTPMxxdG3hzYqvXvHW5R"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
OWNER = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"
PLAN = {
    "accounts": {"user": OWNER},
    "args": {"amount": 1},
    "feePayer": OWNER,
    "build_url": "https://api.orquestra.dev/api/x/instructions/buy/build",
}


def _balance(
    *,
    index: int = 3,
    mint: Any = USDC,
    owner: str | None = OWNER,
    program: str | None = TOKEN_PROGRAM_ID,
    amount: Any = "100000000",
    decimals: Any = 6,
    ui_amount_string: str = "100",
) -> dict[str, Any]:
    """One RPC-shaped token-balance entry (``getTransaction``/fork-sim meta shape)."""
    entry: dict[str, Any] = {
        "accountIndex": index,
        "mint": mint,
        "uiTokenAmount": {
            "amount": amount,
            "decimals": decimals,
            "uiAmount": 100.0,
            "uiAmountString": ui_amount_string,
        },
    }
    if owner is not None:
        entry["owner"] = owner
    if program is not None:
        entry["programId"] = program
    return entry


def _value(
    pre: list[dict[str, Any]] | None,
    post: list[dict[str, Any]] | None,
    **extra: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {"err": None, "logs": [], "unitsConsumed": 86_669}
    if pre is not None:
        value["preTokenBalances"] = pre
    if post is not None:
        value["postTokenBalances"] = post
    value.update(extra)
    return value


def _build_ok(_plan: Any) -> BuiltTx:
    return BuiltTx(tx="QkFTRTY0VFg=", encoding="base64")


def _rpc(value: dict[str, Any]):
    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            return {"result": {"value": None}}
        if method == "simulateTransaction":
            return {"result": {"value": value}}
        raise AssertionError(f"unexpected method {method}")

    return rpc


def _report(value: dict[str, Any], **kw: Any) -> TokenDeltaReport:
    report = parse_token_deltas(value, **kw)
    assert report is not None
    return report


# --- the bug: a USDC outflow must be READABLE, in the mint's own units ----------------


def test_usdc_outflow_is_measured_in_raw_base_units() -> None:
    """25 USDC leaving the payer: raw -25_000_000 at 6 decimals, never ~0."""
    report = _report(
        _value(
            [_balance(amount="100000000")],
            [_balance(amount="75000000")],
        )
    )
    assert report.status == "measured"
    (movement,) = report.movements
    assert movement.mint == USDC
    assert movement.owner == OWNER
    assert movement.decimals == 6
    assert movement.pre_raw == 100_000_000
    assert movement.post_raw == 75_000_000
    assert movement.delta_raw == -25_000_000
    assert movement.ui_delta == "-25.000000"
    (outflow,) = report.outflows()
    assert (outflow.mint, outflow.owner, outflow.raw, outflow.decimals) == (
        USDC,
        OWNER,
        25_000_000,
        6,
    )
    assert outflow.ui == "25.000000"


def test_the_ui_amount_is_rendered_locally_not_taken_from_the_rpc() -> None:
    """``uiAmountString`` is untrusted input and, for interest-bearing mints, a function
    of time. The ui we report is raw/10**decimals computed here."""
    report = _report(
        _value(
            [_balance(amount="1000000", ui_amount_string="9999999")],
            [_balance(amount="0", ui_amount_string="0.000001")],
        )
    )
    (movement,) = report.movements
    assert movement.ui_delta == "-1.000000"


def test_a_zero_decimal_mint_does_not_render_a_decimal_point() -> None:
    report = _report(
        _value(
            [_balance(mint=BONK, amount="12", decimals=0)],
            [_balance(mint=BONK, amount="2", decimals=0)],
        )
    )
    (movement,) = report.movements
    assert movement.delta_raw == -10
    assert movement.ui_delta == "-10"


def test_an_ata_created_in_transaction_is_measured_as_pre_zero_not_skipped() -> None:
    """First-time buyer: no ATA before the tx, so the mint is absent from pre. Skipping
    it is the zero-for-unmeasured bug in a different costume."""
    report = _report(_value([], [_balance(amount="42000000")]))
    assert report.status == "measured"
    (movement,) = report.movements
    assert movement.pre_raw == 0
    assert movement.post_raw == 42_000_000
    assert movement.delta_raw == 42_000_000


def test_an_account_present_in_pre_but_absent_from_post_refuses() -> None:
    """Absent-from-post is not zero: we were not told what it holds."""
    report = _report(_value([_balance(amount="100000000")], []))
    assert report.status == "unmeasurable"
    assert [r.reason for r in report.refusals] == ["post-balance-missing"]


def test_both_arrays_present_and_empty_is_a_measured_zero() -> None:
    """A real 'no token moved' claim — distinct from 'not tracked'."""
    report = _report(_value([], []))
    assert report.status == "measured"
    assert report.movements == ()
    assert report.outflows() == ()


def test_no_token_balance_keys_at_all_is_not_tracked_not_zero() -> None:
    assert parse_token_deltas(_value(None, None)) is None


def test_outflows_aggregates_two_accounts_of_the_same_owner_and_mint() -> None:
    report = _report(
        _value(
            [
                _balance(index=3, amount="100000000"),
                _balance(index=7, amount="5000000"),
            ],
            [
                _balance(index=3, amount="75000000"),
                _balance(index=7, amount="0"),
            ],
        )
    )
    (outflow,) = report.outflows()
    assert outflow.raw == 30_000_000
    assert report.net_raw(mint=USDC, owner=OWNER) == -30_000_000
    assert report.net_raw(mint=BONK, owner=OWNER) == 0


def test_an_inflow_is_not_reported_as_an_outflow() -> None:
    report = _report(_value([_balance(amount="0")], [_balance(amount="7")]))
    assert report.outflows() == ()
    assert report.net_raw(mint=USDC, owner=OWNER) == 7


# --- the refusals: Token-2022 cases we will not under-report --------------------------


@pytest.mark.parametrize(
    "extension,reason",
    [
        ("transferFeeConfig", "transfer-fee"),
        ("transferFeeAmount", "transfer-fee"),
        ("transferHook", "transfer-hook"),
        ("confidentialTransferMint", "confidential-transfer"),
        ("confidentialTransferAccount", "confidential-transfer"),
        ("permanentDelegate", "permanent-delegate"),
        ("nonTransferable", "non-transferable"),
        ("interestBearingConfig", "interest-bearing"),
    ],
)
def test_named_token_2022_extensions_refuse_rather_than_report_a_number(
    extension: str, reason: str
) -> None:
    report = _report(
        _value(
            [_balance(program=TOKEN_2022_PROGRAM_ID, amount="100000000")],
            [_balance(program=TOKEN_2022_PROGRAM_ID, amount="75000000")],
        ),
        mint_extensions={USDC: [extension]},
    )
    assert report.status == "unmeasurable"
    assert [r.reason for r in report.refusals] == [reason]
    assert report.refusals[0].mint == USDC
    assert reason in TOKEN_DELTA_REFUSALS


def test_a_token_2022_mint_with_unread_extensions_refuses() -> None:
    """Extensions are on-chain state. Unread is not 'none' — that is the assume-classic
    bug the house rules forbid."""
    report = _report(
        _value(
            [_balance(program=TOKEN_2022_PROGRAM_ID, amount="100000000")],
            [_balance(program=TOKEN_2022_PROGRAM_ID, amount="75000000")],
        )
    )
    assert report.status == "unmeasurable"
    assert [r.reason for r in report.refusals] == ["token-2022-extensions-unread"]


def test_an_unrecognised_extension_fails_closed() -> None:
    report = _report(
        _value(
            [_balance(program=TOKEN_2022_PROGRAM_ID, amount="100000000")],
            [_balance(program=TOKEN_2022_PROGRAM_ID, amount="75000000")],
        ),
        mint_extensions={USDC: ["someFutureExtensionNobodyModelled"]},
    )
    assert [r.reason for r in report.refusals] == ["extension-unrecognised"]


def test_sound_token_2022_extensions_still_measure() -> None:
    """Metadata/immutable-owner change nothing about what a transfer debits."""
    report = _report(
        _value(
            [_balance(program=TOKEN_2022_PROGRAM_ID, amount="100000000")],
            [_balance(program=TOKEN_2022_PROGRAM_ID, amount="75000000")],
        ),
        mint_extensions={USDC: ["metadataPointer", "immutableOwner", "tokenMetadata"]},
    )
    assert report.status == "measured"
    assert report.movements[0].delta_raw == -25_000_000


def test_extension_evidence_for_a_classic_spl_mint_refuses() -> None:
    """A classic SPL mint cannot carry extensions; evidence naming one contradicts the
    programId, and a contradiction is refused rather than silently dropped."""
    report = _report(
        _value(
            [_balance(program=TOKEN_PROGRAM_ID, amount="100000000")],
            [_balance(program=TOKEN_PROGRAM_ID, amount="75000000")],
        ),
        mint_extensions={USDC: ["transferFeeConfig"]},
    )
    assert report.status == "unmeasurable"
    assert [r.reason for r in report.refusals] == ["token-program-mismatch"]


def test_an_unknown_token_program_is_flagged_not_defaulted_to_classic() -> None:
    report = _report(
        _value(
            [_balance(program=None, amount="100000000")],
            [_balance(program=None, amount="75000000")],
        )
    )
    assert report.status == "unmeasurable"
    assert [r.reason for r in report.refusals] == ["token-program-unknown"]


def test_a_decimals_disagreement_between_pre_and_post_refuses() -> None:
    report = _report(
        _value(
            [_balance(amount="100000000", decimals=6)],
            [_balance(amount="75000000", decimals=9)],
        )
    )
    assert [r.reason for r in report.refusals] == ["decimals-inconsistent"]


def test_a_mint_disagreement_on_the_same_account_refuses() -> None:
    report = _report(_value([_balance(mint=USDC)], [_balance(mint=BONK, amount="1")]))
    assert [r.reason for r in report.refusals] == ["mint-inconsistent"]


def test_a_missing_owner_refuses_rather_than_reporting_an_ownerless_delta() -> None:
    report = _report(
        _value(
            [_balance(owner=None, amount="100000000")],
            [_balance(owner=None, amount="75000000")],
        )
    )
    assert [r.reason for r in report.refusals] == ["owner-unresolved"]


@pytest.mark.parametrize(
    "amount",
    ["not-a-number", 100.5, True, "-5", None, "0x10", ""],
)
def test_a_malformed_amount_refuses_rather_than_coercing(amount: Any) -> None:
    report = _report(_value([_balance(amount=amount)], [_balance(amount="75000000")]))
    assert [r.reason for r in report.refusals] == ["malformed-balance"]


@pytest.mark.parametrize("decimals", [-1, 25, "6", 6.0, True, None])
def test_a_malformed_decimals_refuses_rather_than_guessing_a_scale(
    decimals: Any,
) -> None:
    report = _report(
        _value(
            [_balance(decimals=decimals)],
            [_balance(decimals=decimals, amount="75000000")],
        )
    )
    assert [r.reason for r in report.refusals] == ["malformed-balance"]


@pytest.mark.parametrize("mint", ["", "not base58 at all!", 7, "0OIl" * 11])
def test_a_mint_that_is_not_even_base58_shaped_refuses(mint: Any) -> None:
    """The value-domain floor: a string is not a mint."""
    report = _report(_value([_balance(mint=mint)], [_balance(mint=mint, amount="1")]))
    assert report.status == "unmeasurable"


def test_one_refusal_poisons_the_whole_report_even_beside_a_clean_mint() -> None:
    """Fail closed: we cannot say the refused mint is not the drain."""
    report = _report(
        _value(
            [
                _balance(index=3, amount="100000000"),
                _balance(index=4, mint=BONK, program=TOKEN_2022_PROGRAM_ID),
            ],
            [
                _balance(index=3, amount="75000000"),
                _balance(index=4, mint=BONK, program=TOKEN_2022_PROGRAM_ID, amount="0"),
            ],
        )
    )
    assert report.status == "unmeasurable"
    assert any(m.mint == USDC for m in report.movements)
    with pytest.raises(TokenDeltaUnmeasurable):
        report.outflows()


def test_reading_an_amount_off_an_unmeasurable_report_raises_never_zero() -> None:
    report = _report(
        _value([_balance(program=TOKEN_2022_PROGRAM_ID)], [_balance(amount="0")])
    )
    assert report.status == "unmeasurable"
    with pytest.raises(TokenDeltaUnmeasurable):
        report.outflows()
    with pytest.raises(TokenDeltaUnmeasurable):
        report.net_raw(mint=USDC, owner=OWNER)


def test_the_refusal_vocabulary_is_closed_and_names_the_token_2022_cases() -> None:
    for reason in (
        "transfer-fee",
        "transfer-hook",
        "confidential-transfer",
        "permanent-delegate",
        "non-transferable",
        "interest-bearing",
    ):
        assert reason in TOKEN_DELTA_REFUSALS


# --- the Receipt seam ------------------------------------------------------------------


def test_simulate_fills_the_typed_token_delta() -> None:
    receipt = simulate(
        PLAN,
        rpc_url="https://rpc.example/x",
        rpc_call=_rpc(
            _value(
                [_balance(amount="100000000")],
                [_balance(amount="75000000")],
            )
        ),
        build_call=_build_ok,
        network=UNKNOWN_NETWORK,
    )
    assert receipt.token_delta is not None
    assert receipt.token_delta.status == "measured"
    assert receipt.token_delta.movements[0].delta_raw == -25_000_000


def test_simulate_leaves_the_legacy_tokens_received_permanently_none() -> None:
    """The int field must not survive as a second, weaker parallel source."""
    receipt = simulate(
        PLAN,
        rpc_url="https://rpc.example/x",
        rpc_call=_rpc(
            _value([_balance(amount="100000000")], [_balance(amount="75000000")])
        ),
        build_call=_build_ok,
        network=UNKNOWN_NETWORK,
    )
    assert receipt.tokens_received is None


def test_simulate_without_token_balances_reports_not_tracked() -> None:
    receipt = simulate(
        PLAN,
        rpc_url="https://rpc.example/x",
        rpc_call=_rpc(_value(None, None)),
        build_call=_build_ok,
        network=UNKNOWN_NETWORK,
    )
    assert receipt.token_delta is None


def test_simulate_passes_mint_extension_evidence_through_to_the_refusal() -> None:
    receipt = simulate(
        PLAN,
        rpc_url="https://rpc.example/x",
        rpc_call=_rpc(
            _value(
                [_balance(program=TOKEN_2022_PROGRAM_ID, amount="100000000")],
                [_balance(program=TOKEN_2022_PROGRAM_ID, amount="75000000")],
            )
        ),
        build_call=_build_ok,
        network=UNKNOWN_NETWORK,
        mint_extensions={USDC: ["transferFeeConfig"]},
    )
    assert receipt.token_delta is not None
    assert receipt.token_delta.status == "unmeasurable"
    assert receipt.token_delta.refusals[0].reason == "transfer-fee"


def test_the_movement_carries_no_token_account_address_or_payload() -> None:
    """Invariant #1: mint / decimals / owner / amounts — and nothing else."""
    report = _report(
        _value([_balance(amount="100000000")], [_balance(amount="75000000")])
    )
    movement = report.movements[0]
    assert set(movement.__dataclass_fields__) == {
        "mint",
        "owner",
        "decimals",
        "pre_raw",
        "post_raw",
        "delta_raw",
        "ui_delta",
    }


def test_the_corpus_projection_still_carries_no_token_values() -> None:
    """A new Receipt field must not open a leak into the categorical corpus."""
    import json

    from gecko.corpus import simulated_outcome_from, to_simulated_record

    receipt = simulate(
        PLAN,
        rpc_url="https://rpc.example/x",
        rpc_call=_rpc(
            _value([_balance(amount="100000000")], [_balance(amount="75000000")])
        ),
        build_call=_build_ok,
        network=UNKNOWN_NETWORK,
    )
    blob = json.dumps(
        to_simulated_record(
            simulated_outcome_from(
                receipt,
                program_id="PUMPProgram1111111111111111111111111111111",
                instruction="buy",
                recipe_hash="deadbeef",
                slot=250,
                network="fork",
                ts=1_700_000_000_000,
                surface_id="orquestra:PUMPProgram",
            )
        )
    )
    for canary in (USDC, OWNER, "25000000", "75000000", "token_delta"):
        assert canary not in blob

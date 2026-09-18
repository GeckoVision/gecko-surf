"""The route on mainnet, falsified offline: convert, then buy, both relay-paid.

The parties are the fakes from ``tests/test_autonomous_purchase_relay.py``: a relay that
appends its assertion and signs slot 0, a buyer backend that signs its own slot, the
replaying fake node. What this file pins is the ORDER and the two gates: the purchase leg
is prepared only after the convert leg landed, a convert leg that does not land stops the
route before anything is prepared, and the swap's policy authorises the swap and nothing
the shop's policy authorises.
"""

from __future__ import annotations

import pytest

from gecko.autonomous_purchase import (
    COMPUTE_BUDGET_PROGRAM,
    LET_ME_BUY_PROGRAM,
    LIGHTHOUSE_PROGRAM,
    SWAP_V2_DISCRIMINATOR,
    WHIRLPOOL_PROGRAM,
    PurchaseConfigurationError,
    PurchaseRefused,
    PurchaseSettled,
    settle_route,
    swap_spend_policy,
)
from gecko.trace import Trace
from tests.test_autonomous_purchase_relay import (
    BUYER,
    BuyerBackend,
    FakeRelay,
    _gate,
    _relay_built_tx,
    _rpc,
    _signer,
)

USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"


def _prepared_purchase() -> dict:
    return {
        "refused": False,
        "transaction": {"unsigned_transaction": _relay_built_tx().tx},
        "expires": {"last_valid_block_height": 1},
    }


def _route(*, relay=None, prepare=None):
    relay = relay if relay is not None else FakeRelay()
    backend = BuyerBackend()
    calls: list[int] = []

    def prepare_purchase():
        calls.append(len(relay.asked))  # how many relay signatures existed when asked
        return (prepare or _prepared_purchase)()

    trace = Trace(lane="route", network="fork")
    outcome = settle_route(
        _relay_built_tx().tx,  # the convert leg's bytes; shape is what matters here
        prepare_purchase=prepare_purchase,
        network="fork",
        rpc_url="http://127.0.0.1:8999",
        relay=relay,
        convert_signer=_signer(_gate(), backend),
        purchase_signer=_signer(_gate(), backend),
        authority=BUYER,
        rpc_call=_rpc(),
        trace=trace,
        sleep=lambda _s: None,
    )
    return outcome, relay, backend, calls, trace


def test_both_legs_land_in_order_and_the_purchase_is_prepared_after_the_convert() -> (
    None
):
    outcome, relay, backend, calls, trace = _route()
    assert isinstance(outcome.convert, PurchaseSettled)
    assert isinstance(outcome.purchase, PurchaseSettled)
    assert outcome.landed and outcome.objections == ()
    assert len(relay.asked) == 2 and backend.asked == 2, (
        "each leg: relay first, then buyer"
    )
    assert calls == [1], (
        "the purchase was prepared exactly once, after the relay signed leg 1"
    )
    steps = [row.step for row in trace.rows]
    assert steps.index("convert") < steps.index("prepare"), (
        "convert is recorded before prepare"
    )
    assert steps.count("send") == 2


def test_a_convert_leg_that_is_refused_stops_the_route_before_any_prepare() -> None:
    from tests.test_autonomous_purchase_relay import _relay_built_tx as built

    tampered = FakeRelay(tamper=built().tx)  # returns bytes with NO relay signature
    outcome, relay, backend, calls, _ = _route(relay=tampered)
    assert isinstance(outcome.convert, PurchaseRefused)
    assert outcome.purchase is None
    assert calls == [], "nothing was prepared"
    assert backend.asked == 0, "the buyer was never asked to sign"
    assert not outcome.landed
    assert "purchase leg was not attempted" in outcome.objections[0]


def test_a_purchase_that_cannot_be_prepared_after_the_convert_is_a_named_refusal() -> (
    None
):
    outcome, relay, backend, calls, _ = _route(
        prepare=lambda: {
            "refused": True,
            "code": "receipt-failed",
            "reason": "no USDC yet",
        }
    )
    assert isinstance(outcome.convert, PurchaseSettled), "leg 1 still landed"
    assert isinstance(outcome.purchase, PurchaseRefused)
    assert outcome.purchase.code == "prepare-refused"
    assert "receipt-failed" in outcome.purchase.reason
    assert backend.asked == 1, "only the convert leg was signed"
    assert not outcome.landed


def test_the_swap_policy_authorises_the_swap_and_not_the_shop() -> None:
    policy = swap_spend_policy(
        allowed_destinations=frozenset({"pool111", "vault111"}),
        input_mint=USDG,
        input_decimals=6,
        input_per_transaction_raw=50_000,
    )
    programs = {(a.program_id, a.discriminator) for a in policy.allowed_instructions}
    assert (WHIRLPOOL_PROGRAM, SWAP_V2_DISCRIMINATOR) in programs
    assert (LIGHTHOUSE_PROGRAM, policy_lighthouse(policy)) in programs
    assert (COMPUTE_BUDGET_PROGRAM, b"\x03") in programs
    assert not any(p == LET_ME_BUY_PROGRAM for p, _ in programs), "the shop is not here"
    cap = policy.token_caps.caps[USDG]
    assert (cap.decimals, cap.per_transaction_raw) == (6, 50_000)
    assert (
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" not in policy.token_caps.caps
    ), "the mint being bought needs no cap: a credit is not a spend"


def policy_lighthouse(policy):
    return next(
        a.discriminator
        for a in policy.allowed_instructions
        if a.program_id == LIGHTHOUSE_PROGRAM
    )


def test_the_swap_policy_refuses_to_authorise_nothing() -> None:
    with pytest.raises(PurchaseConfigurationError):
        swap_spend_policy(
            allowed_destinations=frozenset(),
            input_mint=USDG,
            input_decimals=6,
            input_per_transaction_raw=1,
        )
    with pytest.raises(PurchaseConfigurationError):
        swap_spend_policy(
            allowed_destinations=frozenset({"pool111"}),
            input_mint=USDG,
            input_decimals=6,
            input_per_transaction_raw=0,
        )

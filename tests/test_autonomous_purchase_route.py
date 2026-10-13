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
    assert policy.accepted_mints == frozenset(), "no hooked mint is accepted by default"


def test_the_swap_policy_carries_the_human_acceptance_of_the_input_mint() -> None:
    from gecko.simulate import AcceptedMint

    accepted = AcceptedMint(
        mint=USDG,
        extensions=frozenset({"transferHook", "permanentDelegate"}),
        transfer_hook_program=None,
    )
    policy = swap_spend_policy(
        allowed_destinations=frozenset({"pool111"}),
        input_mint=USDG,
        input_decimals=6,
        input_per_transaction_raw=50_000,
        accepted_mints=(accepted,),
    )
    assert policy.accepted_mints == frozenset({accepted})


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


# --------------------------------------------------------------------------------------
# The rebate send endpoint is per LEG: the convert leg may use it, the purchase never.
# --------------------------------------------------------------------------------------

HELIUS_FAKE_KEY = "hk-ROUTE-SENTINEL-DO-NOT-LEAK"


class _RecordingHelius:
    """A fake `post` behind the real Helius transport: records URLs, relays the
    sendTransaction to the fake node so the leg can confirm."""

    def __init__(self, node) -> None:
        self.node = node
        self.urls: list[str] = []

    def __call__(self, url, body):
        import json

        self.urls.append(url)
        request = json.loads(body)
        return self.node("http://127.0.0.1:8999", request["method"], request["params"])


def _helius_send(post, relay=None):
    from gecko.credentials import ChainResolver, KeyringBackend
    from gecko.send_endpoint import helius_send

    class _Keyring:
        def get_keyring(self):
            return object()

        def get_password(self, service, user):
            return HELIUS_FAKE_KEY if service == "gecko:HELIUS_API_KEY" else None

    resolver = ChainResolver([KeyringBackend(module=_Keyring())])
    return helius_send(
        network="mainnet", resolver=resolver, post=post, relay_fee_payer=relay
    )


def _mainnet_signer(backend):
    """The fixtures' signer, asserted on mainnet: the Helius endpoint is mainnet only,
    and a signer profile names the network it signs for."""
    from dataclasses import replace

    signer = _signer(_gate(), backend)
    return replace(signer, profile=replace(signer.profile, network="mainnet"))


def _route_with_send(send, node, *, network="mainnet"):
    backend = BuyerBackend()
    return settle_route(
        _relay_built_tx().tx,
        prepare_purchase=_prepared_purchase,
        network=network,
        rpc_url="http://127.0.0.1:8999",
        relay=FakeRelay(),
        convert_signer=_mainnet_signer(backend),
        purchase_signer=_mainnet_signer(backend),
        authority=BUYER,
        rpc_call=node,
        sleep=lambda _s: None,
        convert_send=send,
    )


def _counting(node):
    sends: list[str] = []

    def call(url, method, params):
        if method == "sendTransaction":
            sends.append(url)
        return node(url, method, params)

    return call, sends


def test_the_convert_leg_goes_through_the_send_endpoint_and_the_purchase_does_not() -> (
    None
):
    """The fixture's convert bytes are a STORE instruction, so even the convert leg's
    request carries no rebate-address: the transport's own guard, independent of the
    route's per-leg choice. The purchase leg never touches the transport at all."""
    from urllib.parse import parse_qs, urlsplit

    node, public_sends = _counting(_rpc())
    post = _RecordingHelius(_rpc())
    outcome = _route_with_send(_helius_send(post), node)

    assert outcome.landed
    assert len(post.urls) == 1, "one send through Helius: the convert leg's"
    assert urlsplit(post.urls[0]).hostname == "mainnet.helius-rpc.com"
    assert "rebate-address" not in parse_qs(urlsplit(post.urls[0]).query)
    assert public_sends == ["http://127.0.0.1:8999"], "the purchase leg, unchanged"
    assert isinstance(outcome.convert, PurchaseSettled)
    assert outcome.convert.rebate is not None
    assert outcome.convert.rebate.requested is False
    assert isinstance(outcome.purchase, PurchaseSettled)
    assert outcome.purchase.rebate is None
    assert HELIUS_FAKE_KEY not in repr(outcome)


def test_without_a_send_endpoint_the_route_is_unchanged() -> None:
    node, public_sends = _counting(_rpc())
    outcome = _route_with_send(None, node)
    assert outcome.landed
    assert len(public_sends) == 2
    assert outcome.convert.rebate is None and outcome.purchase.rebate is None


def test_a_mainnet_only_endpoint_on_a_fork_leg_is_refused_before_anything() -> None:
    node, public_sends = _counting(_rpc())
    post = _RecordingHelius(_rpc())
    with pytest.raises(PurchaseConfigurationError, match="mainnet only"):
        _route_with_send(_helius_send(post), node, network="fork")
    assert post.urls == [] and public_sends == []


def test_the_relay_paid_swap_send_names_the_configured_relay_never_the_buyer() -> None:
    """`_send` with a Whirlpool, relay-paid message and the route's authority. Founder
    ruling, 8 Oct: the rebate refills the relay; the authority never chooses it."""
    from urllib.parse import parse_qs, urlsplit

    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    from gecko.autonomous_purchase import _send
    from gecko.landing import assemble_unsigned_tx
    from tests.test_autonomous_purchase_relay import RELAY

    swap = Instruction(
        Pubkey.from_string(WHIRLPOOL_PROGRAM),
        SWAP_V2_DISCRIMINATOR,
        [AccountMeta(Pubkey.from_string(BUYER), True, True)],
    )
    tx = assemble_unsigned_tx([swap], RELAY).tx
    urls: list[str] = []

    def post(url, body):
        urls.append(url)
        return {"result": "SigFake1111"}

    signature = _send(
        lambda *_a: pytest.fail("the public node must not see this send"),
        "http://127.0.0.1:8999",
        tx,
        send=_helius_send(post, relay=RELAY),
        authority=BUYER,
    )
    assert signature == "SigFake1111"
    assert parse_qs(urlsplit(urls[0]).query)["rebate-address"] == [RELAY]
    assert BUYER not in urls[0]


def test_a_failed_helius_send_is_a_transport_error_without_the_key() -> None:
    import urllib.error

    from gecko.autonomous_purchase import PurchaseTransportError, _send

    def post(url, body):
        raise urllib.error.HTTPError(url, 503, "down", {}, None)

    with pytest.raises(PurchaseTransportError) as caught:
        _send(
            lambda *_a: pytest.fail("no fallback to public mid-transaction"),
            "http://127.0.0.1:8999",
            _relay_built_tx().tx,
            send=_helius_send(post),
            authority=BUYER,
        )
    assert "HTTP 503" in str(caught.value)
    assert HELIUS_FAKE_KEY not in str(caught.value)
    # The only context is the transport's own, already-redacted RpcError, which itself
    # has no context: the HTTPError holding the keyed URL is unreachable.
    assert HELIUS_FAKE_KEY not in repr(caught.value.__context__)
    assert caught.value.__context__.__context__ is None

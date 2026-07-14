"""HttpFacilitatorClient (``X402_MODE=live``) — the HTTP relay to a real x402 facilitator.

Falsified fully OFFLINE (Pattern B): an injected ``PostJson`` fake + an injected resolver
mean no DNS, no sockets, no real facilitator. The suite pins: the wire envelope
(``{x402Version, paymentPayload, paymentRequirements}`` POSTed to ``/verify`` and
``/settle``), fail-closed behavior on every doubt (non-200, malformed JSON, timeout,
missing fields, unverified payment), the env factory (``X402ConfigError`` naming exactly
the missing vars; stub mode never reads the env), SSRF rejection at construction, bearer
redaction, and the end-to-end entitlement invariant: NO grant unless the facilitator
verifies AND settles.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gecko.entitlements import Entitlements
from gecko.netguard import UnsafeUrlError
from gecko.x402 import ChallengeError, PaymentPolicy

# The live adapter is re-exported through x402_pay — one seam, two adapters. Importing
# it from here pins the re-export contract.
from gecko.x402_pay import (
    FacilitatorClient,
    FacilitatorError,
    FakeFacilitator,
    HttpFacilitatorClient,
    Plan,
    Settlement,
    X402ConfigError,
    build_payment_requirements,
    facilitator_for_mode,
    facilitator_from_env,
    settle_subscription,
)

# --- config (all INJECTED — treasury/mint/network are never hardcoded) -----------------
_TREASURY = "GECKOtreasury1111111111111111111111111111111"
_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_PRICE = 99_000_000
_PERIOD = 30 * 24 * 3600
_NOW = 1_700_000_000
_URL = "https://facilitator.example"  # host "resolves" via the injected resolver only
_PUBLIC_IP = "93.184.216.34"  # a routable public address (offline stand-in)
_TOKEN = "sekret-facilitator-token-XYZ"

_PLAN = Plan(
    surface_id="pegana",
    price=_PRICE,
    period_seconds=_PERIOD,
    pay_to=_TREASURY,
    asset=_USDC,
    network="solana-mainnet",
)
_POLICY = PaymentPolicy(
    allowed_pay_to=frozenset({_TREASURY}),
    allowed_assets=frozenset({_USDC}),
    max_amount=_PRICE,
    scheme="exact",
    exact_amount=True,
)

_VERIFY_OK = (200, json.dumps({"isValid": True, "invalidReason": None}))
_SETTLE_OK = (
    200,
    json.dumps(
        {
            "success": True,
            "errorReason": None,
            "transaction": "tx_LIVE_1",
            "network": "solana-mainnet",
            "payer": "PAYER1111111111111111111111111111111111111",
        }
    ),
)

# All four vars the factory requires for live (token is optional, never required).
_LIVE_ENV = {
    "X402_FACILITATOR_URL": f"https://{_PUBLIC_IP}/facilitator",  # IP literal: no DNS
    "X402_PAY_TO": _TREASURY,
    "X402_ASSET": _USDC,
    "X402_NETWORK": "solana-mainnet",
}


def _resolver(host: str) -> list[str]:
    """Offline DNS: every hostname is a routable public address."""
    return [_PUBLIC_IP]


class _Transport:
    """``PostJson`` fake — records every call; replays queued ``(status, raw)`` or raises."""

    def __init__(self, *responses: tuple[int, str] | Exception) -> None:
        self.queue = list(responses)
        self.calls: list[tuple[str, dict[str, Any], dict[str, str], float]] = []

    def __call__(
        self,
        url: str,
        body: Any,
        headers: Any,
        timeout_s: float,
    ) -> tuple[int, str]:
        snapshot = json.loads(json.dumps(dict(body)))  # deep copy at call time
        self.calls.append((url, snapshot, dict(headers), timeout_s))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(
    *responses: tuple[int, str] | Exception,
    token: str | None = None,
    timeout: float = 10.0,
) -> tuple[HttpFacilitatorClient, _Transport]:
    transport = _Transport(*responses)
    client = HttpFacilitatorClient(
        _URL,
        auth_token=token,
        timeout_s=timeout,
        post=transport,
        resolver=_resolver,
    )
    return client, transport


def _reqs() -> dict[str, Any]:
    return build_payment_requirements(_PLAN, _POLICY)


def _payment_for(reqs: dict[str, Any]) -> dict[str, Any]:
    method = reqs["accepts"][0]
    return {
        "payTo": method["payTo"],
        "asset": method["asset"],
        "amount": int(method["maxAmountRequired"]),
        "nonce": "nonce-live-1",
    }


# --- verify: wire envelope + verdict mapping -------------------------------------------
def test_verify_posts_wire_envelope_and_returns_true():
    client, transport = _client(_VERIFY_OK, token=_TOKEN)
    reqs = _reqs()
    payment = _payment_for(reqs)

    assert client.verify(payment, reqs) is True

    url, body, headers, timeout_s = transport.calls[0]
    assert url == f"{_URL}/verify"
    assert body == {
        "x402Version": 1,
        "paymentPayload": payment,
        "paymentRequirements": reqs,
    }
    assert headers["Authorization"] == f"Bearer {_TOKEN}"
    assert headers["Content-Type"] == "application/json"
    assert timeout_s == 10.0


def test_verify_invalid_returns_false():
    client, _ = _client(
        (200, json.dumps({"isValid": False, "invalidReason": "expired"}))
    )
    reqs = _reqs()
    assert client.verify(_payment_for(reqs), reqs) is False


def test_verify_without_token_sends_no_authorization():
    client, transport = _client(_VERIFY_OK)
    reqs = _reqs()
    client.verify(_payment_for(reqs), reqs)
    _, _, headers, _ = transport.calls[0]
    assert "Authorization" not in headers


@pytest.mark.parametrize(
    "response",
    [
        (500, "internal error"),
        (402, json.dumps({"isValid": True})),  # only 200 carries a verdict
        (200, "not json at all"),
        (200, json.dumps({"weird": 1})),  # missing isValid
        (200, json.dumps({"isValid": "yes"})),  # non-boolean isValid
        TimeoutError("timed out"),
        OSError("connection refused"),
    ],
    ids=[
        "http_500",
        "http_402",
        "malformed_json",
        "missing_isvalid",
        "non_bool_isvalid",
        "timeout",
        "socket_error",
    ],
)
def test_verify_fails_closed_on_any_doubt(response):
    client, _ = _client(response)
    reqs = _reqs()
    with pytest.raises(FacilitatorError):
        client.verify(_payment_for(reqs), reqs)


# --- settle: verdict mapping + verify-first discipline ---------------------------------
def test_settle_success_maps_transaction_to_settlement_reference():
    client, transport = _client(_VERIFY_OK, _SETTLE_OK)
    reqs = _reqs()
    payment = _payment_for(reqs)

    assert client.verify(payment, reqs) is True
    settlement = client.settle(payment)

    assert isinstance(settlement, Settlement)
    assert settlement.reference == "tx_LIVE_1"
    url, body, _, _ = transport.calls[1]
    assert url == f"{_URL}/settle"
    # settle re-sends the SAME requirements the payment verified against.
    assert body == {
        "x402Version": 1,
        "paymentPayload": payment,
        "paymentRequirements": reqs,
    }


@pytest.mark.parametrize(
    "response",
    [
        (500, "internal error"),
        (200, "not json at all"),
        (200, json.dumps({"success": False, "errorReason": "insufficient funds"})),
        (200, json.dumps({"success": True, "errorReason": None, "transaction": None})),
        (200, json.dumps({"success": True})),  # missing transaction reference
        TimeoutError("timed out"),
    ],
    ids=[
        "http_500",
        "malformed_json",
        "success_false",
        "null_transaction",
        "missing_transaction",
        "timeout",
    ],
)
def test_settle_fails_closed_on_any_doubt(response):
    client, _ = _client(_VERIFY_OK, response)
    reqs = _reqs()
    payment = _payment_for(reqs)
    assert client.verify(payment, reqs) is True
    with pytest.raises(FacilitatorError):
        client.settle(payment)


def test_settle_refuses_a_payment_never_verified():
    # The Protocol's settle(payment) carries no requirements; the client only settles a
    # payment it verified — an unverified payment fails closed, no wire call.
    client, transport = _client(_SETTLE_OK)
    reqs = _reqs()
    with pytest.raises(FacilitatorError):
        client.settle(_payment_for(reqs))
    assert transport.calls == []  # never reached the facilitator


def test_settle_error_reason_is_short_and_scrubbed():
    echoing = (
        200,
        json.dumps({"success": False, "errorReason": f"denied {_TOKEN} " + "x" * 500}),
    )
    client, _ = _client(_VERIFY_OK, echoing, token=_TOKEN)
    reqs = _reqs()
    payment = _payment_for(reqs)
    client.verify(payment, reqs)
    with pytest.raises(FacilitatorError) as excinfo:
        client.settle(payment)
    message = str(excinfo.value)
    assert _TOKEN not in message
    assert len(message) < 300  # short reason, never a payload dump


def test_client_satisfies_the_neutral_protocol():
    client, _ = _client()
    assert isinstance(client, FacilitatorClient)


# --- redaction: the bearer token never leaks through errors ----------------------------
def test_transport_error_never_leaks_the_token():
    # Simulate a hostile/echoing transport failure that embeds the bearer token.
    client, _ = _client(
        RuntimeError(f"boom Authorization: Bearer {_TOKEN}"), token=_TOKEN
    )
    reqs = _reqs()
    with pytest.raises(FacilitatorError) as excinfo:
        client.verify(_payment_for(reqs), reqs)
    assert _TOKEN not in str(excinfo.value)
    assert _TOKEN not in repr(excinfo.value)


def test_http_error_message_carries_status_not_payload():
    client, _ = _client((503, "upstream exploded"), token=_TOKEN)
    reqs = _reqs()
    payment = _payment_for(reqs)
    with pytest.raises(FacilitatorError) as excinfo:
        client.verify(payment, reqs)
    message = str(excinfo.value)
    assert "503" in message
    assert _TOKEN not in message
    assert payment["nonce"] not in message  # never the X-PAYMENT payload


# --- SSRF: the facilitator URL is validated at construction ----------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8402",
        "http://169.254.169.254/latest",
        "file:///etc/passwd",
        "ftp://facilitator.example",
    ],
    ids=["loopback", "metadata_ip", "file_scheme", "non_http_scheme"],
)
def test_unsafe_facilitator_url_is_rejected_at_construction(url):
    with pytest.raises(UnsafeUrlError):
        HttpFacilitatorClient(url, resolver=_resolver)


def test_private_resolving_host_is_rejected_at_construction():
    with pytest.raises(UnsafeUrlError):
        HttpFacilitatorClient(
            "https://facilitator.internal", resolver=lambda host: ["10.0.0.5"]
        )


# --- end-to-end: the entitlement is written ONLY on a settled payment ------------------
def _settle_e2e(
    facilitator: HttpFacilitatorClient, ents: Entitlements, now: int = _NOW
) -> Any:
    reqs = _reqs()
    return settle_subscription(
        customer_id="cust_1",
        surface_id="pegana",
        returned_terms=reqs,
        payment=_payment_for(reqs),
        policy=_POLICY,
        facilitator=facilitator,
        entitlements=ents,
        period_seconds=_PERIOD,
        now=now,
    )


def test_e2e_settled_payment_grants_cloud_entitlement():
    client, _ = _client(_VERIFY_OK, _SETTLE_OK, token=_TOKEN)
    ents = Entitlements()
    ent = _settle_e2e(client, ents)
    assert ent.kind == "cloud"
    assert ent.expires_at == _NOW + _PERIOD
    assert ent.payment_ref == "tx_LIVE_1"  # the opaque facilitator reference
    assert ents.get("cust_1", "pegana") is ent


def test_e2e_failed_settlement_grants_nothing():
    failed = (200, json.dumps({"success": False, "errorReason": "insufficient funds"}))
    client, _ = _client(_VERIFY_OK, failed, token=_TOKEN)
    ents = Entitlements()
    with pytest.raises(FacilitatorError):
        _settle_e2e(client, ents)
    assert ents.get("cust_1", "pegana") is None  # NO entitlement on any doubt


def test_e2e_facilitator_rejection_grants_nothing():
    rejected = (200, json.dumps({"isValid": False, "invalidReason": "bad signature"}))
    client, _ = _client(rejected, token=_TOKEN)
    ents = Entitlements()
    with pytest.raises(ChallengeError):
        _settle_e2e(client, ents)
    assert ents.get("cust_1", "pegana") is None


def test_e2e_renewal_after_expiry_reuses_the_same_call_path():
    settle_2 = (
        200,
        json.dumps({"success": True, "errorReason": None, "transaction": "tx_LIVE_2"}),
    )
    client, _ = _client(_VERIFY_OK, _SETTLE_OK, _VERIFY_OK, settle_2, token=_TOKEN)
    ents = Entitlements()
    first = _settle_e2e(client, ents, now=_NOW)
    assert first.payment_ref == "tx_LIVE_1"
    later = _NOW + _PERIOD + 1  # expired -> the renew leg re-verifies + re-settles
    second = _settle_e2e(client, ents, now=later)
    assert second.payment_ref == "tx_LIVE_2"
    assert second.expires_at == later + _PERIOD
    assert ents.get("cust_1", "pegana") is second


# --- env factory -----------------------------------------------------------------------
def test_factory_missing_everything_names_every_required_var():
    with pytest.raises(X402ConfigError) as excinfo:
        facilitator_from_env(env={})
    message = str(excinfo.value)
    for name in ("X402_FACILITATOR_URL", "X402_PAY_TO", "X402_ASSET", "X402_NETWORK"):
        assert name in message
    assert "X402_FACILITATOR_TOKEN" not in message  # the token is optional


def test_factory_names_exactly_the_missing_vars():
    partial = dict(_LIVE_ENV)
    del partial["X402_PAY_TO"]
    partial["X402_ASSET"] = "   "  # whitespace-only counts as missing
    with pytest.raises(X402ConfigError) as excinfo:
        facilitator_from_env(env=partial)
    message = str(excinfo.value)
    assert "X402_PAY_TO" in message
    assert "X402_ASSET" in message
    assert "X402_FACILITATOR_URL" not in message
    assert "X402_NETWORK" not in message


def test_factory_builds_the_client_from_a_complete_env():
    env = dict(_LIVE_ENV)
    env["X402_FACILITATOR_TOKEN"] = _TOKEN
    client = facilitator_from_env(env=env, resolver=_resolver)
    assert isinstance(client, HttpFacilitatorClient)
    assert client.facilitator_url == _LIVE_ENV["X402_FACILITATOR_URL"]
    assert client._auth_token == _TOKEN  # noqa: SLF001 — pins the bearer wiring


def test_factory_token_stays_optional():
    client = facilitator_from_env(env=dict(_LIVE_ENV), resolver=_resolver)
    assert client._auth_token is None  # noqa: SLF001


# --- mode resolution: live builds from env; stub ignores the env entirely --------------
def test_live_mode_builds_http_client_from_env(monkeypatch):
    for name, value in _LIVE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("X402_MODE", "live")
    facilitator = facilitator_for_mode()
    assert isinstance(facilitator, HttpFacilitatorClient)
    assert isinstance(facilitator, FacilitatorClient)


def test_live_mode_with_incomplete_env_fails_closed(monkeypatch):
    for name in _LIVE_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(X402ConfigError) as excinfo:
        facilitator_for_mode("live")
    assert "X402_FACILITATOR_URL" in str(excinfo.value)


def test_stub_mode_ignores_the_env_entirely(monkeypatch):
    # Even a hostile/broken facilitator env must be irrelevant in stub mode.
    monkeypatch.setenv("X402_MODE", "stub")
    monkeypatch.setenv("X402_FACILITATOR_URL", "http://127.0.0.1:1")
    facilitator = facilitator_for_mode()
    assert isinstance(facilitator, FakeFacilitator)


def test_unknown_mode_fails_closed():
    with pytest.raises(X402ConfigError):
        facilitator_for_mode("prod")

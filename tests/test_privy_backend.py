"""What the Privy backend refuses, proven with no network, no Privy account and no key.

Pattern B: the transport is injected, so every branch below is falsifiable offline and the
live call is the last check rather than the debugger. Nothing here holds key material — the
"signature" written by the fake transport is a 64-byte marker, and the app secret is a
literal that only ever reaches an assertion.

THE TEST THAT JUSTIFIES THE FILE is ``test_an_unsigned_echo_is_refused``. A binding is taken
over the MESSAGE and a signature lives outside it, so a backend that returns the transaction
untouched passes :func:`gecko.signer._rebind` — the seam compares the half of the transaction
that signing does not change. Only the backend can see that no signature arrived, and this
asserts it does, together with the companion test proving the echo really would have
satisfied the seam (otherwise the refusal is a rule guarding nothing).
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gecko.signer import SigningAttestation  # noqa: E402
from gecko.txbind import message_binding  # noqa: E402
from scripts.privy_backend import (  # noqa: E402
    PRIVY_API_BASE,
    SIGN_METHOD,
    PrivyBackend,
    PrivyBackendError,
    PrivyRequest,
    first_signature,
    from_env,
)

WALLET_ID = "wallet-abc123"
APP_ID = "app-public-id"
APP_SECRET = "secret-that-must-never-leak"
ADDRESS = "DMjTEZJuV3mpfzBNeeuFy9m47A1bj5CXVhCNVo7BEPzy"

#: One signature slot (compact-u16 count = 1), then 64 zero bytes, then a stub message.
#: Not a real Solana message — nothing in this file decodes past the signature slot except
#: the two tests that say they do, and those build a real one.
UNSIGNED = bytes([1]) + bytes(64) + b"\x01\x00\x01\x02message-bytes"
SIGNED = bytes([1]) + bytes([0xEE] * 64) + b"\x01\x00\x01\x02message-bytes"


def _attestation(**overrides: Any) -> SigningAttestation:
    fields: dict[str, Any] = {
        "binding": "0" * 64,
        "strength": "exact",
        "network": "mainnet",
        "fee_payer": ADDRESS,
        "receipt_slot": 100,
        "current_slot": 101,
        "units_consumed": 36_399,
        "profile": "developer",
    }
    fields.update(overrides)
    return SigningAttestation(**fields)


class FakeTransport:
    """Answers the two calls the backend makes, and records what it was asked."""

    def __init__(
        self, *, signed: bytes | None = SIGNED, address: str = ADDRESS
    ) -> None:
        self.signed = signed
        self.address = address
        self.requests: list[PrivyRequest] = []
        self.headers: list[Mapping[str, str]] = []

    def __call__(
        self, request: PrivyRequest, headers: Mapping[str, str]
    ) -> dict[str, Any]:
        self.requests.append(request)
        self.headers.append(dict(headers))
        if request.method == "GET":
            return {"address": self.address, "chain_type": "solana"}
        return {
            "method": SIGN_METHOD,
            "data": {
                "signed_transaction": base64.b64encode(self.signed or b"").decode(),
                "encoding": "base64",
            },
        }


def _backend(transport: FakeTransport, **overrides: Any) -> PrivyBackend:
    fields: dict[str, Any] = {
        "wallet_id": WALLET_ID,
        "app_id": APP_ID,
        "app_secret": APP_SECRET,
        "network": "mainnet",
        "transport": transport,
    }
    fields.update(overrides)
    return PrivyBackend(**fields)


# -- the happy path, and what it proves about the wire ------------------------


def test_it_signs_and_asks_for_the_right_method() -> None:
    transport = FakeTransport()
    signed = _backend(transport).sign_transaction(UNSIGNED, _attestation())

    assert signed == SIGNED
    post = [r for r in transport.requests if r.method == "POST"]
    assert len(post) == 1
    assert post[0].url == f"{PRIVY_API_BASE}/v1/wallets/{WALLET_ID}/rpc"
    assert post[0].body is not None
    assert post[0].body["method"] == SIGN_METHOD
    assert post[0].body["params"]["encoding"] == "base64"
    assert base64.b64decode(post[0].body["params"]["transaction"]) == UNSIGNED


def test_it_never_asks_privy_to_broadcast() -> None:
    """``signAndSendTransaction`` would move the send to a vendor — the rule evaded, not kept."""
    transport = FakeTransport()
    _backend(transport).sign_transaction(UNSIGNED, _attestation())

    for request in transport.requests:
        assert "signAndSend" not in str(request.body)
        assert "signAndSend" not in request.url


def test_the_address_is_asked_for_once_and_never_asserted() -> None:
    transport = FakeTransport()
    backend = _backend(transport)

    assert backend.pubkey == ADDRESS
    assert backend.pubkey == ADDRESS  # cached: still one GET
    assert sum(1 for r in transport.requests if r.method == "GET") == 1


# -- the check the seam cannot make -------------------------------------------


def test_an_unsigned_echo_is_refused() -> None:
    """Privy returns the transaction untouched: no error, no signature."""
    transport = FakeTransport(signed=UNSIGNED)
    with pytest.raises(PrivyBackendError, match="EMPTY signature slot"):
        _backend(transport).sign_transaction(UNSIGNED, _attestation())


def test_the_unsigned_echo_would_have_satisfied_the_seam() -> None:
    """Without the test above, the refusal guards nothing — so prove the hole is real.

    Built on a REAL transaction rather than the stub, because the point is what
    :func:`gecko.signer._rebind` computes: the binding over an unsigned transaction and over
    the same transaction signed are equal, so upstream cannot tell them apart.
    """
    solders = pytest.importorskip("solders")
    from solders.hash import Hash
    from solders.instruction import Instruction
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.transaction import Transaction

    assert solders is not None
    keypair = Keypair()
    message = Message.new_with_blockhash(
        [Instruction(Keypair().pubkey(), b"\x00", [])],
        keypair.pubkey(),
        Hash.default(),
    )
    unsigned = bytes(Transaction.new_unsigned(message))
    signed = bytes(
        Transaction.populate(message, [keypair.sign_message(bytes(message))])
    )

    assert unsigned != signed
    assert first_signature(unsigned) == bytes(64)
    assert first_signature(signed) != bytes(64)
    # The seam's check — identical, which is exactly why the backend must make its own.
    assert message_binding(unsigned, strength="exact") == message_binding(
        signed, strength="exact"
    )


# -- refusals: the ones the seam also makes, made again by a second party ------


def test_an_unconfigured_network_refuses() -> None:
    transport = FakeTransport()
    with pytest.raises(PrivyBackendError, match="no network configured"):
        _backend(transport, network="unknown").sign_transaction(
            UNSIGNED, _attestation()
        )
    assert transport.requests == []


def test_a_verdict_for_another_network_refuses() -> None:
    transport = FakeTransport()
    with pytest.raises(PrivyBackendError, match="authorises nothing"):
        _backend(transport).sign_transaction(UNSIGNED, _attestation(network="devnet"))
    assert not [r for r in transport.requests if r.method == "POST"]


def test_a_fee_payer_that_is_not_this_wallet_refuses() -> None:
    transport = FakeTransport()
    with pytest.raises(PrivyBackendError, match="fee payer is not this wallet"):
        _backend(transport).sign_transaction(
            UNSIGNED,
            _attestation(fee_payer="SomeoneElsesAccount11111111111111111111111"),
        )
    assert not [r for r in transport.requests if r.method == "POST"]


def test_a_non_solana_wallet_refuses() -> None:
    """Refused on the CHAIN, before the fee-payer comparison — an Ethereum address can
    never equal a Solana fee payer, so the wrong-chain reason has to fire on its own."""

    def ethereum(request: PrivyRequest, headers: Mapping[str, str]) -> dict[str, Any]:
        return {"address": "0xabc", "chain_type": "ethereum"}

    with pytest.raises(PrivyBackendError, match="not a Solana wallet"):
        _backend(ethereum).sign_transaction(UNSIGNED, _attestation())  # type: ignore[arg-type]


def test_an_answer_with_no_signed_transaction_refuses() -> None:
    def empty(request: PrivyRequest, headers: Mapping[str, str]) -> dict[str, Any]:
        if request.method == "GET":
            return {"address": ADDRESS, "chain_type": "solana"}
        return {"method": SIGN_METHOD, "data": {}}

    with pytest.raises(PrivyBackendError, match="no signed transaction"):
        _backend(empty).sign_transaction(UNSIGNED, _attestation())  # type: ignore[arg-type]


# -- the secret ----------------------------------------------------------------


def test_the_secret_never_reaches_a_repr() -> None:
    rendered = repr(_backend(FakeTransport()))
    assert APP_SECRET not in rendered
    assert "<redacted>" in rendered


def test_the_secret_reaches_the_transport_only_as_a_basic_header() -> None:
    """It has to be sent — this pins WHERE, so a leak elsewhere is a failing test."""
    transport = FakeTransport()
    _backend(transport).sign_transaction(UNSIGNED, _attestation())

    for request in transport.requests:
        assert APP_SECRET not in str(request.body)
        assert APP_SECRET not in request.url
        assert APP_SECRET not in str(dict(request.privy_headers))
    for headers in transport.headers:
        assert APP_SECRET not in headers.get("privy-app-id", "")
        decoded = base64.b64decode(headers["Authorization"].split(" ", 1)[1]).decode()
        assert decoded == f"{APP_ID}:{APP_SECRET}"


def test_a_refusal_never_carries_the_secret() -> None:
    def boom(request: PrivyRequest, headers: Mapping[str, str]) -> dict[str, Any]:
        raise PrivyBackendError("Privy answered HTTP 401")

    with pytest.raises(PrivyBackendError) as caught:
        _backend(boom).sign_transaction(UNSIGNED, _attestation())  # type: ignore[arg-type]
    assert APP_SECRET not in str(caught.value)


# -- configuration --------------------------------------------------------------


def test_from_env_names_every_missing_variable() -> None:
    with pytest.raises(PrivyBackendError) as caught:
        from_env({})
    message = str(caught.value)
    for name in ("PRIVY_APP_ID", "PRIVY_APP_SECRET", "PRIVY_WALLET_ID"):
        assert name in message


def test_from_env_defaults_the_network_to_unknown_which_refuses() -> None:
    backend = from_env(
        {
            "PRIVY_APP_ID": APP_ID,
            "PRIVY_APP_SECRET": APP_SECRET,
            "PRIVY_WALLET_ID": WALLET_ID,
        }
    )
    assert backend.network == "unknown"
    assert backend.authorization_key is None


def test_an_authorization_signature_is_sent_only_on_non_get_requests() -> None:
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    der = key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    transport = FakeTransport()
    backend = _backend(
        transport, authorization_key=base64.b64encode(der).decode("ascii")
    )
    backend.sign_transaction(UNSIGNED, _attestation())

    by_method = {r.method: h for r, h in zip(transport.requests, transport.headers)}
    assert "privy-authorization-signature" not in by_method["GET"]
    assert by_method["POST"]["privy-authorization-signature"]

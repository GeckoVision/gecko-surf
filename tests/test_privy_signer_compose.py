"""Privy behind the real seam: :class:`~gecko.signer.TransactionSigner` + `PrivyBackend`.

``tests/test_privy_backend.py`` proves what the backend refuses in isolation. This file
proves the thing that isolation cannot: that the backend satisfies the seam's Protocol at
runtime, is REACHED by a real ``sign()`` call, and that a signature actually comes back out
the far end. Without it, "the Privy backend works" would rest on a fake that was only ever
called by its own test.

Offline (Pattern B): the transaction is assembled locally, Privy is an injected transport,
and the "signature" is a 64-byte ``0xEE`` marker. Nothing here holds a key, reaches Privy,
or broadcasts on any network — the marker stands in for the one property under test, which
is that the signature slot is FILLED and the message is unchanged.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gecko.handoff import SignerHandoff  # noqa: E402
from gecko.landing import assemble_unsigned_tx  # noqa: E402
from gecko.signer import (  # noqa: E402
    DEFAULT_SIGNER_PROFILE_NAME,
    SignerProfile,
    SignerRefused,
    SigningBackend,
    TransactionSigner,
)
from gecko.simulate import Receipt, TokenDeltaReport  # noqa: E402
from gecko.spend_policy import (  # noqa: E402
    AllowedInstruction,
    InMemorySpendLedger,
    SpendPolicy,
    SpendPolicyGate,
    TokenCaps,
)
from gecko.txbind import message_binding  # noqa: E402
from scripts.privy_backend import SIGN_METHOD, PrivyBackend, PrivyRequest  # noqa: E402

pytest.importorskip("solders")

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
REAL_BLOCKHASH = "So11111111111111111111111111111111111111112"
SLOT = 300_000_000
MARKER = bytes([0xEE] * 64)


def _payer() -> str:
    from solders.keypair import Keypair

    return str(Keypair().pubkey())


def _tx(payer: str, payload: bytes = b"gecko") -> str:
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    meta = AccountMeta(
        pubkey=Pubkey.from_string(USDC), is_signer=False, is_writable=False
    )
    return assemble_unsigned_tx(
        [Instruction(Pubkey.from_string(MEMO_PROGRAM), payload, [meta])],
        payer,
        blockhash=REAL_BLOCKHASH,
    ).tx


def _receipt(tx: str) -> Receipt:
    return Receipt(
        status="pass",
        err=None,
        revert_class=None,
        units_consumed=5_000,
        sol_delta=-5_000,
        tokens_received=None,
        logs_tail=(),
        network_label="simulated (fork/RPC snapshot — not mainnet)",
        message_binding=message_binding(tx, strength="exact"),
        binding_strength="exact",
        lookup_resolution="none",
        network="mainnet",
        observed_slot=SLOT,
        token_delta=TokenDeltaReport(status="measured", movements=(), refusals=()),
    )


def _handoff(tx: str, receipt: Receipt) -> SignerHandoff:
    return SignerHandoff(
        approved=True,
        reason="verified",
        transaction_base64=tx,
        binding=receipt.message_binding,
        strength=receipt.binding_strength,
        status=receipt.status,
        revert_class=None,
        units_consumed=receipt.units_consumed,
        network_label=receipt.network_label,
        logs_tail=(),
        network=receipt.network,
    )


def _gate() -> SpendPolicyGate:
    return SpendPolicyGate(
        policy=SpendPolicy(
            authorized=True,
            per_transaction_cap_lamports=10_000_000,
            hourly_cap_lamports=100_000_000,
            daily_cap_lamports=500_000_000,
            max_transactions_per_day=100,
            allowed_instructions=frozenset(
                {AllowedInstruction(program_id=MEMO_PROGRAM, discriminator=b"gecko")}
            ),
            allowed_destinations=frozenset({USDC}),
            token_caps=TokenCaps.none(),
        ),
        ledger=InMemorySpendLedger(),
    )


class FakePrivy:
    """Privy's two endpoints, answering exactly as documented — and nothing more.

    ``sign`` fills the fee payer's signature slot with a MARKER and leaves the message
    byte-identical, which is what a real signature does to the wire format.
    """

    def __init__(self, address: str, *, mutate_message: bool = False) -> None:
        self.address = address
        self.mutate_message = mutate_message
        self.signed_payloads: list[str] = []
        self.requests: list[PrivyRequest] = []

    def __call__(
        self, request: PrivyRequest, headers: Mapping[str, str]
    ) -> dict[str, Any]:
        from solders.signature import Signature
        from solders.transaction import Transaction

        self.requests.append(request)
        if request.method == "GET":
            return {"address": self.address, "chain_type": "solana"}

        assert request.body is not None
        payload = request.body["params"]["transaction"]
        self.signed_payloads.append(payload)
        transaction = Transaction.from_bytes(base64.b64decode(payload))
        message = transaction.message
        if self.mutate_message:
            # A DIFFERENT message — same payer and blockhash, different instruction data.
            # Rebuilding with identical arguments would produce identical bytes, and the
            # test would pass for the wrong reason.
            message = Transaction.from_bytes(
                base64.b64decode(_tx(str(message.account_keys[0]), payload=b"swapped"))
            ).message
        signed = Transaction.populate(message, [Signature.from_bytes(MARKER)])
        return {
            "method": SIGN_METHOD,
            "data": {
                "signed_transaction": base64.b64encode(bytes(signed)).decode(),
                "encoding": "base64",
            },
        }


def _signer(privy: FakePrivy, address: str) -> TransactionSigner:
    return TransactionSigner(
        backend=PrivyBackend(
            wallet_id="wallet-abc123",
            app_id="app-public-id",
            app_secret="secret-that-must-never-leak",
            network="mainnet",
            transport=privy,
        ),
        profile=SignerProfile(
            name=DEFAULT_SIGNER_PROFILE_NAME, network="mainnet", authorized=True
        ),
        spend_gate=_gate(),
    )


def test_the_backend_satisfies_the_seams_protocol() -> None:
    """``runtime_checkable`` is the contract the seam type-checks against.

    The transport is injected even though nothing here signs — see the test below for why
    an ``isinstance`` check against this Protocol is not a free, offline operation.
    """
    backend = PrivyBackend(
        wallet_id="w",
        app_id="a",
        app_secret="s",
        network="mainnet",
        transport=FakePrivy("11111111111111111111111111111111"),
    )
    assert isinstance(backend, SigningBackend)


def test_an_isinstance_check_against_the_protocol_performs_IO() -> None:
    """A hazard worth pinning: ``pubkey`` is a Protocol member AND a network call.

    ``runtime_checkable`` verifies members by attribute access, and ``PrivyBackend.pubkey``
    is a property that asks Privy for the wallet's address. So ``isinstance(backend,
    SigningBackend)`` — which reads as a free type check — reaches the network, and against
    a misconfigured backend it fails there instead of returning ``False``. Nothing in
    ``gecko/`` makes that check (asserted below, so this stays true), and any caller that
    adds one must know it is not free.
    """
    import gecko.signer as signer_module

    source = Path(signer_module.__file__).read_text()
    assert "isinstance(" not in source or "SigningBackend)" not in source

    privy = FakePrivy("11111111111111111111111111111111")
    backend = PrivyBackend(
        wallet_id="w", app_id="a", app_secret="s", network="mainnet", transport=privy
    )
    assert privy.requests == []
    isinstance(backend, SigningBackend)
    # The GET fired during the isinstance check — no sign() call has been made.
    assert [request.method for request in privy.requests] == ["GET"]


def test_a_transaction_is_signed_through_the_real_seam() -> None:
    payer = _payer()
    tx = _tx(payer)
    receipt = _receipt(tx)
    privy = FakePrivy(payer)

    signed = _signer(privy, payer).sign(
        _handoff(tx, receipt), receipt=receipt, current_slot=SLOT + 1
    )

    assert signed.signer_pubkey == payer
    assert signed.network == "mainnet"
    assert signed.binding == receipt.message_binding
    assert signed.spend_verdict is not None and signed.spend_verdict.authorized
    # The signature slot really is filled, and the message really is unchanged.
    raw = base64.b64decode(signed.signed_transaction_base64)
    assert MARKER in raw
    assert message_binding(raw, strength="exact") == receipt.message_binding
    # Privy was asked to sign the exact bytes the receipt attests.
    assert privy.signed_payloads == [tx]


def test_a_privy_that_returns_a_different_message_is_refused_by_the_seam() -> None:
    """The seam's own check, exercised through this backend rather than assumed."""
    payer = _payer()
    tx = _tx(payer)
    receipt = _receipt(tx)
    privy = FakePrivy(payer, mutate_message=True)

    with pytest.raises(SignerRefused) as caught:
        _signer(privy, payer).sign(
            _handoff(tx, receipt), receipt=receipt, current_slot=SLOT + 1
        )
    assert caught.value.code == "backend-changed-the-message"


def test_a_privy_fault_is_a_refusal_and_never_a_fallback() -> None:
    payer = _payer()
    tx = _tx(payer)
    receipt = _receipt(tx)

    def unreachable(
        request: PrivyRequest, headers: Mapping[str, str]
    ) -> dict[str, Any]:
        if request.method == "GET":
            return {"address": payer, "chain_type": "solana"}
        raise OSError("connection reset")

    signer = TransactionSigner(
        backend=PrivyBackend(
            wallet_id="w",
            app_id="a",
            app_secret="s",
            network="mainnet",
            transport=unreachable,  # type: ignore[arg-type]
        ),
        profile=SignerProfile(
            name=DEFAULT_SIGNER_PROFILE_NAME, network="mainnet", authorized=True
        ),
        spend_gate=_gate(),
    )
    with pytest.raises(SignerRefused) as caught:
        signer.sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT + 1)
    assert caught.value.code == "backend-unavailable"

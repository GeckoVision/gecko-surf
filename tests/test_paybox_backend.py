"""The PayBox authority backend, offline: what it sends the SDK CLI, what it refuses, and
what it never says. No PayBox account, no network, no key."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gecko.signer import SigningAttestation  # noqa: E402
from scripts.paybox_backend import (  # noqa: E402
    PayboxAuthorityBackend,
    PayboxBackendError,
    PayboxWallet,
)

TOKEN = "pbx-token-must-never-print"
SIGNING = "pbxk1.must-never-print"


@pytest.fixture(autouse=True)
def _creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYBOX_TOKEN", TOKEN)
    monkeypatch.setenv("PAYBOX_SIGNIN_KEY", SIGNING)


def _keys():
    from solders.keypair import Keypair

    return Keypair(), Keypair()  # relay, buyer


def _relay_signed_tx(relay, buyer) -> tuple[str, Any]:
    """The bytes PayBox is asked to sign: relay in slot 0, buyer's slot empty."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    ix = Instruction(
        Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"),
        b"espresso",
        [AccountMeta(buyer.pubkey(), True, True)],
    )
    message = Message.new_with_blockhash([ix], relay.pubkey(), Hash.default())
    tx = Transaction.new_unsigned(message)
    tx.partial_sign([relay], message.recent_blockhash)
    return base64.b64encode(bytes(tx)).decode(), message


def _wallet(buyer) -> PayboxWallet:
    return PayboxWallet("cred-1", "sol-default", str(buyer.pubkey()), "autonomous")


def _attestation(signing_as: str = "authority") -> SigningAttestation:
    return SigningAttestation(
        binding="b" * 64,
        strength="exact",
        network="mainnet",
        fee_payer="relay",
        receipt_slot=1,
        current_slot=2,
        units_consumed=1,
        profile="external-signer",
        signing_as=signing_as,  # type: ignore[arg-type]
    )


class FakeCli:
    """Records argv and env; answers `sign` by signing the buyer's slot for real."""

    def __init__(
        self, buyer, *, status: str = "success", tamper=None, drop_relay=False
    ):
        self.buyer = buyer
        self.status = status
        self.tamper = tamper
        self.drop_relay = drop_relay
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv, env):
        self.calls.append((list(argv), dict(env)))
        if "credentials" in argv:
            return 0, json.dumps(
                [
                    {
                        "credential": {
                            "credential_type": "wallet",
                            "id": "cred-1",
                            "name": "sol-default",
                            "metadata": {"address": str(self.buyer.pubkey())},
                        },
                        "grant": {"approval_mode": "autonomous"},
                    },
                    {
                        "credential": {
                            "credential_type": "card",
                            "id": "card-1",
                            "name": "visa",
                        },
                        "grant": {"approval_mode": "always_approve"},
                    },
                ]
            )
        intent = json.loads(argv[argv.index("--intent") + 1])
        if self.status != "success":
            return 0, json.dumps(
                {"status": self.status, "request_id": "req-1", "error": "parked"}
            )
        from solders.transaction import Transaction

        raw = self.tamper or base64.b64decode(intent["transactionBase64"])
        tx = Transaction.from_bytes(raw)
        if self.drop_relay:
            from solders.signature import Signature

            tx = Transaction.populate(
                tx.message, [Signature.default()] * len(tx.signatures)
            )
        tx.partial_sign([self.buyer], tx.message.recent_blockhash)
        return 0, json.dumps(
            {
                "status": "success",
                "request_id": "req-1",
                "output": {
                    "signedTransactionBase64": base64.b64encode(bytes(tx)).decode()
                },
            }
        )


def test_open_picks_the_autonomous_wallet_and_maps_the_app_env_names() -> None:
    relay, buyer = _keys()
    cli = FakeCli(buyer)
    backend = PayboxAuthorityBackend.open(run=cli, cli=["paybox"])
    assert backend.pubkey == str(buyer.pubkey())
    argv, env = cli.calls[0]
    assert argv == ["paybox", "--json", "credentials"]
    assert env["PAYBOX_ACCESS_TOKEN"] == TOKEN and env["PAYBOX_SIGNING_KEY"] == SIGNING
    assert "PAYBOX_TOKEN" not in env, (
        "the gecko-app names are mapped, not forwarded twice"
    )
    assert TOKEN not in repr(backend) and SIGNING not in repr(backend)


def test_open_refuses_a_wallet_that_waits_for_a_passkey() -> None:
    _relay, buyer = _keys()

    class Manual(FakeCli):
        def __call__(self, argv, env):
            code, out = super().__call__(argv, env)
            return code, out.replace("autonomous", "always_approve")

    with pytest.raises(PayboxBackendError, match="needs 'autonomous'"):
        PayboxAuthorityBackend.open(run=Manual(buyer), cli=["paybox"])


def test_sign_sends_the_solana_intent_and_returns_bytes_with_both_signatures() -> None:
    from solders.transaction import Transaction

    relay, buyer = _keys()
    unsigned_b64, message = _relay_signed_tx(relay, buyer)
    cli = FakeCli(buyer)
    backend = PayboxAuthorityBackend(wallet=_wallet(buyer), run=cli, cli=("paybox",))

    signed = backend.sign_transaction(base64.b64decode(unsigned_b64), _attestation())

    argv, env = cli.calls[-1]
    assert argv[:5] == ["paybox", "--json", "sign", "--credential", "cred-1"]
    intent = json.loads(argv[argv.index("--intent") + 1])
    assert intent == {
        "op": "solanaTransaction",
        "address": str(buyer.pubkey()),
        "transactionBase64": unsigned_b64,
    }
    tx = Transaction.from_bytes(signed)
    assert bytes(tx.message) == bytes(message), "the message came back untouched"
    assert all(tx.verify_with_results()), "relay's and buyer's signatures both verify"


def test_fee_payer_for_another_account_is_refused_before_paybox_is_asked() -> None:
    """The relay's bytes name the RELAY as fee payer. Asked to sign them as fee payer,
    the wallet would be paying for someone else; it refuses without calling PayBox."""
    relay, buyer = _keys()
    unsigned_b64, _ = _relay_signed_tx(relay, buyer)
    cli = FakeCli(buyer)
    backend = PayboxAuthorityBackend(
        wallet=_wallet(buyer), run=cli, cli=("paybox",), self_paid=True
    )
    with pytest.raises(PayboxBackendError, match="another account"):
        backend.sign_transaction(
            base64.b64decode(unsigned_b64), _attestation("fee-payer")
        )
    assert cli.calls == []


def test_a_self_paid_transaction_signs_as_its_own_fee_payer() -> None:
    """Self-paid: the wallet is slot 0. A one-time co-signer already filled its own slot
    (a Whirlpool position mint); PayBox signs the wallet's slot and leaves that one."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.transaction import Transaction

    _, buyer = _keys()
    position_mint = Keypair()
    ix = Instruction(
        Pubkey_from("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"),
        b"open",
        [AccountMeta(position_mint.pubkey(), True, True)],
    )
    message = Message.new_with_blockhash([ix], buyer.pubkey(), Hash.default())
    tx = Transaction.new_unsigned(message)
    tx.partial_sign([position_mint], message.recent_blockhash)
    cli = FakeCli(buyer)
    backend = PayboxAuthorityBackend(
        wallet=_wallet(buyer), run=cli, cli=("paybox",), self_paid=True
    )
    signed = backend.sign_transaction(bytes(tx), _attestation("fee-payer"))
    back = Transaction.from_bytes(signed)
    assert bytes(back.message) == bytes(message)
    assert all(back.verify_with_results()), (
        "wallet and co-signer signatures both verify"
    )


def test_fee_payer_is_refused_unless_the_runner_opted_in() -> None:
    """The self-paid scope is per runner: a backend opened without it keeps the
    authority-only rule, so a mis-wired fee-payer profile elsewhere still fails here."""
    _, buyer = _keys()
    from solders.hash import Hash
    from solders.message import Message
    from solders.transaction import Transaction

    message = Message.new_with_blockhash([], buyer.pubkey(), Hash.default())
    cli = FakeCli(buyer)
    backend = PayboxAuthorityBackend(wallet=_wallet(buyer), run=cli, cli=("paybox",))
    with pytest.raises(PayboxBackendError, match="self_paid"):
        backend.sign_transaction(
            bytes(Transaction.new_unsigned(message)), _attestation("fee-payer")
        )
    assert cli.calls == []


def test_the_authority_role_is_refused_when_the_bytes_make_the_wallet_fee_payer() -> (
    None
):
    _, buyer = _keys()
    from solders.hash import Hash
    from solders.message import Message
    from solders.transaction import Transaction

    message = Message.new_with_blockhash([], buyer.pubkey(), Hash.default())
    cli = FakeCli(buyer)
    backend = PayboxAuthorityBackend(wallet=_wallet(buyer), run=cli, cli=("paybox",))
    with pytest.raises(PayboxBackendError, match="role and the bytes disagree"):
        backend.sign_transaction(
            bytes(Transaction.new_unsigned(message)), _attestation()
        )
    assert cli.calls == []


def Pubkey_from(address: str):  # noqa: N802 - a local helper named for what it builds
    from solders.pubkey import Pubkey

    return Pubkey.from_string(address)


def test_a_parked_request_is_a_refusal_naming_status_and_id_only() -> None:
    relay, buyer = _keys()
    unsigned_b64, _ = _relay_signed_tx(relay, buyer)
    backend = PayboxAuthorityBackend(
        wallet=_wallet(buyer),
        run=FakeCli(buyer, status="pending_signature"),
        cli=("paybox",),
    )
    with pytest.raises(PayboxBackendError) as err:
        backend.sign_transaction(base64.b64decode(unsigned_b64), _attestation())
    assert "pending_signature" in str(err.value) and "req-1" in str(err.value)
    assert TOKEN not in str(err.value) and unsigned_b64[:20] not in str(err.value)


def test_a_signature_over_a_different_message_is_refused() -> None:
    relay, buyer = _keys()
    unsigned_b64, _ = _relay_signed_tx(relay, buyer)
    other_b64, _ = _relay_signed_tx(
        relay, buyer
    )  # same shape, fresh keys? no: same keys, same bytes
    # make a genuinely different message: different payload
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    drained = Message.new_with_blockhash(
        [
            Instruction(
                Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"),
                b"drain",
                [AccountMeta(buyer.pubkey(), True, True)],
            )
        ],
        relay.pubkey(),
        Hash.default(),
    )
    tampered = Transaction.new_unsigned(drained)
    tampered.partial_sign([relay], drained.recent_blockhash)
    backend = PayboxAuthorityBackend(
        wallet=_wallet(buyer),
        run=FakeCli(buyer, tamper=bytes(tampered)),
        cli=("paybox",),
    )
    with pytest.raises(PayboxBackendError, match="message-substituted"):
        backend.sign_transaction(base64.b64decode(unsigned_b64), _attestation())


def test_a_dropped_relay_signature_is_refused() -> None:
    relay, buyer = _keys()
    unsigned_b64, _ = _relay_signed_tx(relay, buyer)
    backend = PayboxAuthorityBackend(
        wallet=_wallet(buyer), run=FakeCli(buyer, drop_relay=True), cli=("paybox",)
    )
    with pytest.raises(PayboxBackendError, match="co-signature"):
        backend.sign_transaction(base64.b64decode(unsigned_b64), _attestation())


def test_a_transaction_that_does_not_name_the_wallet_is_refused_before_the_call() -> (
    None
):
    relay, buyer = _keys()
    _relay2, stranger = _keys()
    unsigned_b64, _ = _relay_signed_tx(relay, stranger)
    cli = FakeCli(buyer)
    backend = PayboxAuthorityBackend(wallet=_wallet(buyer), run=cli, cli=("paybox",))
    with pytest.raises(PayboxBackendError, match="required signer"):
        backend.sign_transaction(base64.b64decode(unsigned_b64), _attestation())
    assert cli.calls == []


def test_missing_credentials_refuse_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PAYBOX_TOKEN")
    monkeypatch.delenv("PAYBOX_SIGNIN_KEY")
    _relay, buyer = _keys()
    cli = FakeCli(buyer)
    with pytest.raises(PayboxBackendError, match="token"):
        PayboxAuthorityBackend.open(run=cli, cli=["paybox"])
    assert cli.calls == []

"""The relay-paid purchase, falsified offline: relay signs first, we re-verify, buyer
signs as authority, two signatures merge, one transaction is sent.

Every party here is a throwaway key in this process: the "relay" reproduces the shape
Kora returns (an appended Lighthouse assertion, its signature in slot 0), the buyer's
backend signs its own slot and nothing else, and the RPC is the replaying fake from
``tests/test_autonomous_purchase.py``. The assertion that matters is on the bytes the
fake node RECEIVED: both signatures verify against one message, and that message carries
both our instruction and the relay's.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import pytest

from gecko.autonomous_purchase import (
    PurchaseConfigurationError,
    PurchasePlan,
    PurchaseRefused,
    PurchaseSettled,
    default_spend_policy,
    run_purchase,
    settle_sponsored,
)
from gecko.cosign import unfilled_slots
from gecko.signer import (
    DEVELOPER_KEYPAIR_FILE_PROFILE_NAME,
    SignerProfile,
    SigningAttestation,
    TransactionSigner,
)
from gecko.simulate import BuiltTx
from gecko.spend_policy import InMemorySpendLedger, SpendPolicyGate
from tests.test_autonomous_purchase import (
    ATA_PROGRAM,
    BUYER_ATA,
    DISCRIMINATOR,
    FORK_BLOCKHASH,
    PROGRAM,
    STORE_AUTHORITY,
    STORE_RECEIPTS,
    STORE_TOKEN,
    SYSTEM_PROGRAM,
    TOKEN_PROGRAM,
    USDC,
    FakeRpc,
)
from tests.test_relay import kora_extend


def _keypair():
    from solders.keypair import Keypair

    return Keypair()


RELAY_KP = _keypair()
BUYER_KP = _keypair()
RELAY = str(RELAY_KP.pubkey())
BUYER = str(BUYER_KP.pubkey())


def _plan() -> PurchasePlan:
    return PurchasePlan(
        api_id="let_me_buy",
        instruction="make_purchase",
        accounts={
            "receipts": STORE_RECEIPTS,
            "signer": BUYER,
            "authority": STORE_AUTHORITY,
            "mint": USDC,
            "sender_token_account": BUYER_ATA,
            "recipient_token_account": STORE_TOKEN,
            "token_program": TOKEN_PROGRAM,
            "system_program": SYSTEM_PROGRAM,
            "associated_token_program": ATA_PROGRAM,
        },
        args={
            "store_name": "geckocoffee",
            "product_name": "Espresso",
            "table_number": 1,
        },
        fee_payer=RELAY,
        authority=BUYER,
    )


def _relay_built_tx() -> BuiltTx:
    """The Orquestra build for a relay-paid purchase: payer is the relay, the buyer is the
    instruction's signer. Two required signatures."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    def meta(address: str, *, writable: bool, signer: bool = False) -> AccountMeta:
        return AccountMeta(
            pubkey=Pubkey.from_string(address), is_signer=signer, is_writable=writable
        )

    instruction = Instruction(
        program_id=Pubkey.from_string(PROGRAM),
        data=DISCRIMINATOR + b"\x00" * 8,
        accounts=[
            meta(STORE_RECEIPTS, writable=True),
            meta(BUYER, writable=True, signer=True),
            meta(STORE_AUTHORITY, writable=True),
            meta(USDC, writable=False),
            meta(BUYER_ATA, writable=True),
            meta(STORE_TOKEN, writable=True),
            meta(TOKEN_PROGRAM, writable=False),
            meta(SYSTEM_PROGRAM, writable=False),
            meta(ATA_PROGRAM, writable=False),
        ],
    )
    message = Message.new_with_blockhash(
        [instruction], Pubkey.from_string(RELAY), Hash.from_string(FORK_BLOCKHASH)
    )
    return BuiltTx(
        tx=base64.b64encode(bytes(Transaction.new_unsigned(message))).decode(),
        encoding="base64",
    )


@dataclass
class FakeRelay:
    """Kora's shape: appends its assertion, signs slot 0, returns the whole thing."""

    asked: list[str] = field(default_factory=list)
    tamper: str | None = None

    @property
    def pubkey(self) -> str:
        return RELAY

    def sign_as_fee_payer(self, unsigned_transaction_base64: str) -> str:
        self.asked.append(unsigned_transaction_base64)
        if self.tamper is not None:
            return self.tamper
        return kora_extend(unsigned_transaction_base64, RELAY_KP)


@dataclass
class BuyerBackend:
    """Signs the BUYER's slot with the buyer's key and touches nothing else."""

    asked: int = 0

    @property
    def pubkey(self) -> str:
        return BUYER

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        from solders.transaction import Transaction

        self.asked += 1
        assert attestation.signing_as == "authority"
        tx = Transaction.from_bytes(unsigned_transaction)
        tx.partial_sign([BUYER_KP], tx.message.recent_blockhash)
        return bytes(tx)


def _gate() -> SpendPolicyGate:
    return SpendPolicyGate(
        policy=default_spend_policy(
            allowed_destinations=frozenset(
                {STORE_RECEIPTS, STORE_AUTHORITY, STORE_TOKEN, BUYER_ATA}
            ),
            sponsored=True,
        ),
        ledger=InMemorySpendLedger(),
    )


def _signer(gate: SpendPolicyGate, backend: BuyerBackend, role: str = "authority"):
    return TransactionSigner(
        backend=backend,
        profile=SignerProfile(
            name=DEVELOPER_KEYPAIR_FILE_PROFILE_NAME,
            network="fork",
            authorized=True,
            signing_as=role,  # type: ignore[arg-type]
        ),
        spend_gate=gate,
    )


def _rpc(**overrides):
    # The buyer holds nothing and keeps holding nothing: pre == post, so sol_delta is 0.
    defaults = dict(pre_lamports=0, post_lamports=0, token_owner=BUYER)
    defaults.update(overrides)
    return FakeRpc(**defaults)


def _run(*, relay=None, backend=None, rpc=None, gate=None, role="authority"):
    backend = backend or BuyerBackend()
    gate = gate or _gate()
    rpc = rpc or _rpc()
    relay = relay if relay is not None else FakeRelay()
    outcome = run_purchase(
        network="fork",
        rpc_url="http://127.0.0.1:8999",
        plan=_plan(),
        signer=_signer(gate, backend, role),
        spend_gate=gate,
        build_call=lambda _request: _relay_built_tx(),
        rpc_call=rpc,
        relay=relay,
        sleep=lambda _s: None,
    )
    return outcome, rpc, relay, backend


# --- the settled path -----------------------------------------------------------------


def test_a_relay_paid_purchase_lands_with_two_signatures_over_one_message() -> None:
    from solders.transaction import Transaction

    outcome, rpc, relay, backend = _run()

    assert isinstance(outcome, PurchaseSettled), getattr(outcome, "reason", None)
    assert outcome.signatures == 2
    assert outcome.fee_payer == RELAY
    assert outcome.authority == BUYER
    assert backend.asked == 1

    # The relay was asked ONCE, with the ORIGINAL bytes: one instruction, unsigned.
    assert len(relay.asked) == 1
    original = Transaction.from_bytes(base64.b64decode(relay.asked[0]))
    assert len(original.message.instructions) == 1
    assert unfilled_slots(relay.asked[0]) == (RELAY, BUYER)

    # What the node RECEIVED: both signatures, both valid, over the EXTENDED message.
    assert len(rpc.sent) == 1
    sent = Transaction.from_bytes(base64.b64decode(rpc.sent[0]))
    assert unfilled_slots(rpc.sent[0]) == ()
    assert all(sent.verify_with_results())
    assert len(sent.message.instructions) == 2, "ours, then the relay's assertion"
    assert str(sent.message.account_keys[0]) == RELAY

    # Simulated twice: the original bytes, then the relay's answer as a new subject.
    assert rpc.calls.count("simulateTransaction") == 2
    assert rpc.calls.index("sendTransaction") > rpc.calls.index("simulateTransaction")


def test_settle_sponsored_finishes_bytes_prepared_elsewhere() -> None:
    """The prepare_purchase tool hands back unsigned relay-paid bytes; this is the tail."""
    gate = _gate()
    backend = BuyerBackend()
    rpc = _rpc()
    relay = FakeRelay()
    outcome = settle_sponsored(
        _relay_built_tx().tx,
        network="fork",
        rpc_url="http://127.0.0.1:8999",
        relay=relay,
        signer=_signer(gate, backend),
        authority=BUYER,
        rpc_call=rpc,
        sleep=lambda _s: None,
    )
    assert isinstance(outcome, PurchaseSettled), getattr(outcome, "reason", None)
    assert outcome.signatures == 2
    assert unfilled_slots(rpc.sent[0]) == ()


# --- the refusals, each proving the next party was never reached ----------------------


def test_a_relay_that_alters_the_instruction_is_refused_before_the_buyer_signs() -> (
    None
):
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    drained = Message.new_with_blockhash(
        [
            Instruction(
                Pubkey.from_string(PROGRAM),
                DISCRIMINATOR + b"\xff" * 8,
                [AccountMeta(Pubkey.from_string(BUYER), True, True)],
            )
        ],
        Pubkey.from_string(RELAY),
        Hash.from_string(FORK_BLOCKHASH),
    )
    tx = Transaction.new_unsigned(drained)
    tx.partial_sign([RELAY_KP], drained.recent_blockhash)
    outcome, rpc, _relay, backend = _run(
        relay=FakeRelay(tamper=base64.b64encode(bytes(tx)).decode())
    )
    assert isinstance(outcome, PurchaseRefused)
    assert outcome.code == "relay-refused"
    assert "instruction-altered" in outcome.reason
    assert backend.asked == 0
    assert rpc.sent == []


def test_a_buyer_whose_lamports_moved_is_refused_by_the_authority_role() -> None:
    outcome, rpc, _relay, backend = _run(
        rpc=_rpc(pre_lamports=10_000, post_lamports=5_000)
    )
    assert isinstance(outcome, PurchaseRefused)
    assert outcome.code == "signer-refused"
    assert "authority-lamports-moved" in outcome.reason
    assert backend.asked == 0
    assert rpc.sent == []


def test_the_lighthouse_assertion_must_be_allowlisted_by_the_policy() -> None:
    """`sponsored=False` is a self-paid policy; a relay's addition is refused by the
    gate, which is the gate doing its job rather than a bug in the relay path."""
    gate = SpendPolicyGate(
        policy=default_spend_policy(
            allowed_destinations=frozenset(
                {STORE_RECEIPTS, STORE_AUTHORITY, STORE_TOKEN, BUYER_ATA}
            ),
            sponsored=False,
        ),
        ledger=InMemorySpendLedger(),
    )
    outcome, rpc, _relay, backend = _run(gate=gate)
    assert isinstance(outcome, PurchaseRefused)
    assert outcome.code == "spend-refused"
    assert backend.asked == 0
    assert rpc.sent == []


def test_a_relay_paid_plan_with_a_fee_payer_profile_is_a_mis_wiring() -> None:
    with pytest.raises(PurchaseConfigurationError):
        _run(role="fee-payer")


def test_a_plan_naming_an_authority_without_a_relay_is_a_mis_wiring() -> None:
    gate = _gate()
    with pytest.raises(PurchaseConfigurationError):
        run_purchase(
            network="fork",
            rpc_url="http://127.0.0.1:8999",
            plan=_plan(),
            signer=_signer(gate, BuyerBackend()),
            spend_gate=gate,
            build_call=lambda _request: _relay_built_tx(),
            rpc_call=_rpc(),
        )


def test_a_relay_that_is_not_the_plans_fee_payer_is_a_mis_wiring() -> None:
    @dataclass
    class Stranger(FakeRelay):
        @property
        def pubkey(self) -> str:
            return str(_keypair().pubkey())

    with pytest.raises(PurchaseConfigurationError):
        _run(relay=Stranger())

"""The relay-paid rehearsal, offline: relay signs first, buyer signs its slot, one merged
transaction lands on a fake fork, and the ledger says the buyer's SOL moved by zero.

The fake fork here LANDS: it answers simulateTransaction, applies the token movement on
sendTransaction, and reports the fee it took from the relay. It does not write the
program's receipt row (that is the program's, not the fork's), so the receipt refusal is
expected here and asserted as the ONLY discrepancy. Everything relay-specific is judged
on the bytes the fake node received.
"""

from __future__ import annotations

import base64
from typing import Any

from gecko.cosign import unfilled_slots
from gecko.sandbox import ephemeral_signer
from gecko.sandbox.rehearse import rehearse_purchase
from gecko.simulate import BuiltTx
from gecko.trace import Trace
from tests.test_relay import kora_extend
from tests.test_sandbox_rehearse import (
    USDC,
    FakeFork,
    fresh_pubkey,
    offline_proof,
    token_account_bytes,
    wired_fake,
)

PROGRAM = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
DISCRIMINATOR = bytes.fromhex("c13ee38869d4c914")
BLOCKHASH = "6xCk4Xgb64QofLjfh5Q5sy47W5dURHagdcDWWhhoAqgo"
FEE = 5_000
CU = 54_647


def _keypair():
    from solders.keypair import Keypair

    return Keypair()


class LandingFork(FakeFork):
    """A FakeFork that simulates, lands, and moves the tokens the purchase names."""

    def __init__(self, rpc_url: str, price: int) -> None:
        super().__init__(rpc_url)
        self.price = price
        self.sent: list[str] = []
        self.payer_of_last: str | None = None

    def _getLatestBlockhash(self, _params: list[Any]) -> dict[str, Any]:  # noqa: N802
        return {
            "context": {"slot": 1},
            "value": {"blockhash": BLOCKHASH, "lastValidBlockHeight": 526},
        }

    def _simulateTransaction(self, params: list[Any]) -> dict[str, Any]:  # noqa: N802
        tracked = (params[1].get("accounts") or {}).get("addresses") or []
        return {
            "context": {"slot": 1},
            "value": {
                "err": None,
                "unitsConsumed": CU,
                "logs": [f"Program {PROGRAM} success"],
                # surfpool's shape: the tracked account snapshot is null, the token
                # balance arrays are null. Nothing here can be read as a token leg.
                "accounts": [None for _ in tracked],
                "preTokenBalances": None,
                "postTokenBalances": None,
            },
        }

    def _sendTransaction(self, params: list[Any]) -> str:  # noqa: N802
        from solders.transaction import Transaction

        raw = base64.b64decode(params[0])
        tx = Transaction.from_bytes(raw)
        assert all(tx.verify_with_results()), "the fork refuses unverifiable bytes"
        self.sent.append(params[0])
        keys = [str(k) for k in tx.message.account_keys]
        payer = keys[0]
        self.payer_of_last = payer
        # the fee leaves the payer
        account = self.accounts.setdefault(payer, {"lamports": 0})
        account["lamports"] = account.get("lamports", 0) - FEE
        # the tokens move: instruction accounts 4 (sender ata) and 5 (recipient ata)
        ix = tx.message.instructions[0]
        indexes = list(bytes(ix.accounts))
        sender, recipient = keys[indexes[4]], keys[indexes[5]]
        self._move(sender, -self.price)
        self._move(recipient, self.price)
        return "5" + "k" * 86

    def _move(self, ata: str, delta: int) -> None:
        from gecko.sandbox.cheatcodes import _decode_token_account

        current = self.accounts.get(ata)
        mint, owner, amount = (
            _decode_token_account(current) if current else (None, None, None)
        )
        if mint is None:
            mint, owner, amount = USDC, self.owner_of(ata), 0
        self.accounts[ata] = {
            "lamports": 2_039_280,
            "data": [token_account_bytes(mint, owner, (amount or 0) + delta), "base64"],
            "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "space": 165,
        }

    def owner_of(self, ata: str) -> str:
        """The store's authority for its ATA; a fresh key for anything else."""
        from gecko.store_accounts import TOKEN_PROGRAM_ID, derive_ata

        for account in self.accounts.values():
            data = account.get("data")
            if account.get("owner") == PROGRAM and isinstance(data, list):
                raw = base64.b64decode(data[0])
                # authority sits after: 8 disc + 4 vec len + 8 total + borsh(name)
                name_len = int.from_bytes(raw[20:24], "little")
                authority = raw[24 + name_len : 56 + name_len]
                from solders.pubkey import Pubkey

                candidate = str(Pubkey.from_bytes(authority))
                if derive_ata(candidate, USDC, token_program=TOKEN_PROGRAM_ID) == ata:
                    return candidate
        return fresh_pubkey()

    def _getSignatureStatuses(self, _params: list[Any]) -> dict[str, Any]:  # noqa: N802
        return {
            "context": {"slot": 2},
            "value": [{"confirmationStatus": "confirmed", "err": None, "slot": 2}],
        }

    def _getTransaction(self, _params: list[Any]) -> dict[str, Any]:  # noqa: N802
        return {
            "slot": 2,
            "meta": {"err": None, "computeUnitsConsumed": CU, "fee": FEE},
        }

    def _getMultipleAccounts(self, params: list[Any]) -> dict[str, Any]:  # noqa: N802
        return {
            "context": {"slot": 1},
            "value": [self.accounts.get(a) for a in params[0]],
        }


def _builder(request: Any) -> BuiltTx:
    """The Orquestra shape for a relay-paid build: payer from the request, ONE signature
    slot under a two-signer header (the measured defect the package repairs)."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey

    accounts = request["accounts"]
    order = [
        ("receipts", True, False),
        ("signer", True, True),
        ("authority", True, False),
        ("mint", False, False),
        ("sender_token_account", True, False),
        ("recipient_token_account", True, False),
        ("token_program", False, False),
        ("system_program", False, False),
        ("associated_token_program", False, False),
    ]
    metas = [
        AccountMeta(Pubkey.from_string(accounts[name]), signer, writable)
        for name, writable, signer in order
    ]
    ix = Instruction(Pubkey.from_string(PROGRAM), DISCRIMINATOR + b"\x00" * 8, metas)
    message = Message.new_with_blockhash(
        [ix], Pubkey.from_string(request["feePayer"]), Hash.from_string(BLOCKHASH)
    )
    # one slot, two required: bytes([1]) + 64 zero bytes + message
    raw = bytes([1]) + bytes(64) + bytes(message)
    return BuiltTx(tx=base64.b64encode(raw).decode(), encoding="base64")


class FakeRelay:
    def __init__(
        self, keypair, *, lamports: int = 50_000_000, tamper: str | None = None
    ):
        self._kp = keypair
        self.asked: list[str] = []
        self.tamper = tamper

    @property
    def pubkey(self) -> str:
        return str(self._kp.pubkey())

    def sign_as_fee_payer(self, unsigned_transaction_base64: str) -> str:
        self.asked.append(unsigned_transaction_base64)
        return self.tamper or kora_extend(unsigned_transaction_base64, self._kp)


def _setup(price: int = 100_000):
    from gecko.store_accounts import TOKEN_PROGRAM_ID, derive_ata

    authority = fresh_pubkey()
    fork = wired_fake("teststore", authority, "Water", price)
    landing = LandingFork(fork.rpc_url, price)
    landing.accounts = fork.accounts
    # A real store's token account exists before the purchase; the fake seeds it at 0
    # so the credit reads as a delta rather than as an account appearing from nothing.
    landing._move(derive_ata(authority, USDC, token_program=TOKEN_PROGRAM_ID), 0)
    relay_kp = _keypair()
    landing.accounts[str(relay_kp.pubkey())] = {
        "lamports": 50_000_000,
        "data": ["", "base64"],
    }
    proof = offline_proof(landing.rpc_url)
    return landing, proof, relay_kp


def test_a_relay_paid_rehearsal_lands_two_signatures_and_the_buyer_pays_no_sol() -> (
    None
):
    from solders.transaction import Transaction

    fork, proof, relay_kp = _setup()
    relay = FakeRelay(relay_kp)
    buyer = ephemeral_signer(proof)

    trace = Trace(lane="rehearsal", network="fork")
    result = rehearse_purchase(
        proof,
        buyer=buyer,
        store="teststore",
        product="Water",
        rpc_call=fork,
        build_call=_builder,
        relay=relay,
        trace=trace,
    )

    assert result.landed, result.refusals
    assert result.signatures == 2
    # The run wrote its own graph's input: every step, in order, all ok.
    assert [row.step for row in trace.rows] == [
        "fund",
        "prepare",
        "sponsor",
        "resimulate",
        "cosign",
        "land",
        "judge",
        "reset",
    ]
    assert trace.refused is None
    assert [row.party for row in trace.rows][2:5] == ["relay", "node", "buyer"]
    assert trace.rows[2].note == "appended L2TExMFK"
    assert result.fee_payer == relay.pubkey
    # The buyer never held a lamport and still holds none.
    assert result.buyer_sol is not None
    assert result.buyer_sol.before is None and result.buyer_sol.after is None
    assert result.relay_sol is not None and result.relay_sol.moved == -FEE
    assert result.buyer_token is not None and result.buyer_token.moved == -100_000
    assert result.store_token is not None and result.store_token.moved == 100_000
    assert result.units_consumed == CU and result.simulated_units == CU
    # The only objection is the receipt row, which this fake fork does not write.
    assert len(result.discrepancies) == 1 and "receipt" in result.discrepancies[0]

    # The relay was asked once, with the ORIGINAL bytes: one instruction, two empty slots.
    assert len(relay.asked) == 1
    original = Transaction.from_bytes(base64.b64decode(relay.asked[0]))
    assert len(original.message.instructions) == 1
    assert unfilled_slots(relay.asked[0]) == (relay.pubkey, buyer.pubkey)

    # What the fork received: one transaction, two valid signatures, the relay's
    # assertion appended, and NO SOL was ever funded to the buyer.
    assert len(fork.sent) == 1
    sent = Transaction.from_bytes(base64.b64decode(fork.sent[0]))
    assert unfilled_slots(fork.sent[0]) == ()
    assert len(sent.message.instructions) == 2
    assert fork.payer_of_last == relay.pubkey
    funded_sol = [m for _u, m in fork.seen if m == "surfnet_setAccount"]
    assert funded_sol == [], "a relay-paid rehearsal funds the buyer with tokens only"


def test_a_relay_that_alters_the_instruction_is_refused_before_the_buyer_signs() -> (
    None
):
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    fork, proof, relay_kp = _setup()
    buyer = ephemeral_signer(proof)
    drained = Message.new_with_blockhash(
        [
            Instruction(
                Pubkey.from_string(PROGRAM),
                DISCRIMINATOR + b"\xff" * 8,
                [AccountMeta(Pubkey.from_string(buyer.pubkey), True, True)],
            )
        ],
        relay_kp.pubkey(),
        Hash.from_string(BLOCKHASH),
    )
    tx = Transaction.new_unsigned(drained)
    tx.partial_sign([relay_kp], drained.recent_blockhash)
    relay = FakeRelay(relay_kp, tamper=base64.b64encode(bytes(tx)).decode())

    result = rehearse_purchase(
        proof,
        buyer=buyer,
        store="teststore",
        product="Water",
        rpc_call=fork,
        build_call=_builder,
        relay=relay,
    )
    assert not result.landed
    assert [r.step for r in result.refusals] == ["sponsor"]
    assert "instruction-altered" in result.refusals[0].reason
    assert fork.sent == []
    assert result.balanced is None

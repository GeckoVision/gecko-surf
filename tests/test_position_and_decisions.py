"""The liquidity-position path and the decision log, falsified offline.

``settle_cosigned`` is ``settle_sponsored`` without a relay: a one-time key (a fresh
Whirlpool position mint) signs its own slot first, then the same simulate, verify, gate,
sign, merge. What this file pins: both signatures land over ONE message, the co-signer's
signature buys nothing the gate did not authorise, and the position policies authorise
their own instruction and nothing else. The decision log keeps its three terminal facts
apart and carries no address.
"""

from __future__ import annotations

import base64

import pytest
from solders.keypair import Keypair

from gecko.autonomous_purchase import (
    PurchaseRefused,
    PurchaseSettled,
    default_spend_policy,
    settle_cosigned,
)
from gecko.cosign import unfilled_slots
from gecko.decision_log import decision_row
from gecko.providers.whirlpool_position import (
    INCREASE_BY_AMOUNTS,
    OPEN_POSITION,
    position_spend_policies,
)
from gecko.simulate import AcceptedMint
from gecko.spend_policy import InMemorySpendLedger, SpendPolicyGate
from gecko.trace import Trace
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
)
from tests.test_autonomous_purchase_relay import BUYER, BUYER_KP, _rpc, _signer

COSIGNER = Keypair()
USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"


def _cosigned_tx() -> str:
    """Self-paid by the buyer; the instruction also needs the one-time key's signature."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    def meta(address: str, *, writable: bool, signer: bool = False) -> AccountMeta:
        return AccountMeta(Pubkey.from_string(address), signer, writable)

    instruction = Instruction(
        Pubkey.from_string(PROGRAM),
        DISCRIMINATOR + b"\x00" * 8,
        [
            meta(STORE_RECEIPTS, writable=True),
            meta(BUYER, writable=True, signer=True),
            meta(str(COSIGNER.pubkey()), writable=True, signer=True),
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
        [instruction], Pubkey.from_string(BUYER), Hash.from_string(FORK_BLOCKHASH)
    )
    return base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()


def _gate(*, include_cosigner: bool = True) -> SpendPolicyGate:
    destinations = {STORE_RECEIPTS, STORE_AUTHORITY, STORE_TOKEN, BUYER_ATA}
    if include_cosigner:
        destinations.add(str(COSIGNER.pubkey()))
    return SpendPolicyGate(
        policy=default_spend_policy(
            allowed_destinations=frozenset(destinations), sponsored=False
        ),
        ledger=InMemorySpendLedger(),
    )


class SelfPaidBackend:
    """Signs the wallet's slot as its own fee payer and touches nothing else."""

    asked = 0

    @property
    def pubkey(self) -> str:
        return BUYER

    def sign_transaction(self, unsigned_transaction: bytes, attestation) -> bytes:
        from solders.transaction import Transaction

        self.asked += 1
        assert attestation.signing_as == "fee-payer"
        tx = Transaction.from_bytes(unsigned_transaction)
        tx.partial_sign([BUYER_KP], tx.message.recent_blockhash)
        return bytes(tx)


def _settle(gate: SpendPolicyGate, cosigners=(COSIGNER,)):
    backend = SelfPaidBackend()
    rpc = _rpc()
    outcome = settle_cosigned(
        _cosigned_tx(),
        network="fork",
        rpc_url="http://127.0.0.1:8999",
        signer=_signer(gate, backend, role="fee-payer"),
        authority=BUYER,
        cosigners=cosigners,
        rpc_call=rpc,
        sleep=lambda _s: None,
    )
    return outcome, rpc, backend


def test_a_cosigned_transaction_lands_with_both_signatures_over_one_message() -> None:
    from solders.transaction import Transaction

    outcome, rpc, backend = _settle(_gate())
    assert isinstance(outcome, PurchaseSettled), getattr(outcome, "reason", None)
    assert outcome.signatures == 2
    assert outcome.fee_payer == outcome.authority == BUYER
    assert backend.asked == 1
    (sent,) = rpc.sent
    assert unfilled_slots(sent) == ()
    landed = Transaction.from_bytes(base64.b64decode(sent))
    original = Transaction.from_bytes(base64.b64decode(_cosigned_tx()))
    assert bytes(landed.message) == bytes(original.message), (
        "the message is ours, unchanged"
    )
    assert all(landed.verify_with_results())


def test_a_cosigner_signature_buys_nothing_the_gate_did_not_authorise() -> None:
    """The one-time key's account is written; a policy that does not name it refuses,
    and nothing is sent. Signing first does not get the bytes past the gate."""
    outcome, rpc, backend = _settle(_gate(include_cosigner=False))
    assert isinstance(outcome, PurchaseRefused)
    assert outcome.code == "spend-refused"
    assert rpc.sent == []


def test_a_send_that_fails_after_signing_is_a_named_refusal_not_a_crash() -> None:
    """A node failure after the bytes are signed comes back as `transport-failed`, so a
    runner records it instead of crashing, and a re-run is a decision, not a reflex."""
    from gecko.autonomous_purchase import PurchaseTransportError
    import gecko.autonomous_purchase as ap

    real = ap._send

    def failing(call, rpc_url, tx):
        raise PurchaseTransportError("node down", signature="sig-broadcast")

    ap._send = failing  # type: ignore[assignment]
    try:
        outcome, rpc, backend = _settle(_gate())
    finally:
        ap._send = real  # type: ignore[assignment]
    assert isinstance(outcome, PurchaseRefused)
    assert outcome.code == "transport-failed"
    assert "sig-broadcast" in outcome.reason


def test_a_missing_cosigner_is_refused_at_merge_never_sent_half_signed() -> None:
    outcome, rpc, backend = _settle(_gate(), cosigners=())
    assert isinstance(outcome, PurchaseRefused)
    assert outcome.code == "cosign-refused"
    assert rpc.sent == []


# --- the position policies ------------------------------------------------------------


def _plan() -> dict:
    return {
        "mints": {"a": USDG, "b": USDC},
        "discriminators": {
            OPEN_POSITION: "87802f4d0f98f031",
            INCREASE_BY_AMOUNTS: "effb097cd2c6352b",
        },
        "increase_values": {
            "method": {"ByTokenAmounts": {"token_max_a": 5000, "token_max_b": 4000}}
        },
    }


def test_each_position_policy_authorises_its_own_instruction_and_nothing_else() -> None:
    accepted = AcceptedMint(
        mint=USDG, extensions=frozenset({"transferHook"}), transfer_hook_program=None
    )
    open_policy, increase_policy = position_spend_policies(
        _plan(),
        open_destinations=frozenset({"pos1111", "mint1111", "ata1111"}),
        increase_destinations=frozenset({"pool1111", "vault1111"}),
        decimals={USDG: 6, USDC: 6},
        accepted_mints=(accepted,),
    )
    whirlpool = {
        a for a in open_policy.allowed_instructions if a.program_id.startswith("whirL")
    }
    assert [a.discriminator.hex() for a in whirlpool] == ["87802f4d0f98f031"]
    assert open_policy.token_caps is not None and not open_policy.token_caps.caps
    assert open_policy.accepted_mints == frozenset()
    whirlpool = {
        a
        for a in increase_policy.allowed_instructions
        if a.program_id.startswith("whirL")
    }
    assert [a.discriminator.hex() for a in whirlpool] == ["effb097cd2c6352b"]
    caps = increase_policy.token_caps.caps
    assert (caps[USDG].per_transaction_raw, caps[USDC].per_transaction_raw) == (
        5000,
        4000,
    )
    assert increase_policy.accepted_mints == frozenset({accepted})
    assert open_policy.missing_fields() == increase_policy.missing_fields() == ()


# --- the decision log -----------------------------------------------------------------


def _trace(*outcomes: str) -> Trace:
    trace = Trace(lane="route", network="mainnet")
    for index, outcome in enumerate(outcomes):
        with trace.step(f"s{index}", "gecko") as facts:
            if outcome != "ok":
                facts["outcome"] = outcome
    return trace


def test_a_landed_row_carries_signatures_and_no_code() -> None:
    row = decision_row(
        trace=_trace("ok", "ok"),
        lane="route",
        network="mainnet",
        programs=("whirlpool",),
        terminal="landed",
        signatures=("sig1",),
        at_stake=((USDC, 100_000, 6), (USDG, 60_000, 6), ("SoMeOtHeRmInT", 5, 9)),
    )
    assert row["terminal"] == "landed" and row["code"] is None
    assert row["signatures"] == ["sig1"]
    usd = {a["symbol"]: a["usd"] for a in row["at_stake"]}
    assert usd == {"USDC": 0.1, "USDG": 0.06, None: None}, (
        "a price we did not read is null"
    )


def test_a_refused_row_names_the_step_party_and_the_code_it_printed() -> None:
    row = decision_row(
        trace=_trace("ok", "backend-unavailable"),
        lane="settle",
        network="mainnet",
        programs=("let_me_buy",),
        terminal="refused",
        code="signer-refused",
    )
    assert (row["ended_at"], row["ended_by"], row["code"]) == (
        "s1",
        "gecko",
        "signer-refused",
    )
    assert row["signatures"] == []


def test_the_three_terminal_facts_cannot_be_blurred() -> None:
    with pytest.raises(ValueError):
        decision_row(
            trace=_trace("ok"),
            lane="x",
            network="mainnet",
            programs=(),
            terminal="landed",
        )
    with pytest.raises(ValueError):
        decision_row(
            trace=_trace("ok"),
            lane="x",
            network="mainnet",
            programs=(),
            terminal="refused",
        )


def test_a_row_carries_no_address_field() -> None:
    row = decision_row(
        trace=_trace("ok"),
        lane="x",
        network="mainnet",
        programs=("p",),
        terminal="abandoned",
        code="route-unaffordable",
    )
    assert set(row) == {
        "decision_id",
        "date",
        "network",
        "lane",
        "programs",
        "steps",
        "terminal",
        "code",
        "ended_at",
        "ended_by",
        "signatures",
        "at_stake",
    }


# --- the deposit's data, when the remote builder truncates the enum -------------------


def _deposit_plan() -> dict:
    return {
        "discriminators": {INCREASE_BY_AMOUNTS: "effb097cd2c6352b"},
        "increase_values": {
            "method": {
                "ByTokenAmounts": {
                    "token_max_a": 5000,
                    "token_max_b": 5000,
                    "min_sqrt_price": 2**64 - 1,
                    "max_sqrt_price": 2**64 + 1,
                }
            }
        },
    }


def _deposit_tx(data: bytes) -> str:
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    whirlpool = Pubkey.from_string("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc")
    ix = Instruction(
        whirlpool,
        data,
        [
            AccountMeta(Pubkey.from_string(STORE_RECEIPTS), False, True),
            AccountMeta(Pubkey.from_string(BUYER), True, False),
            AccountMeta(Pubkey.from_string(USDC), False, False),
        ],
    )
    message = Message.new_with_blockhash(
        [ix], Pubkey.from_string(BUYER), Hash.from_string(FORK_BLOCKHASH)
    )
    return base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()


def test_a_truncated_deposit_is_completed_and_nothing_else_changes() -> None:
    from solders.transaction import Transaction

    from gecko.providers.whirlpool_position import complete_increase_data, increase_data

    plan = _deposit_plan()
    full = increase_data(plan)
    assert len(full) == 58
    before = Transaction.from_bytes(base64.b64decode(_deposit_tx(full[:9])))
    after = Transaction.from_bytes(
        base64.b64decode(complete_increase_data(_deposit_tx(full[:9]), plan))
    )
    assert bytes(after.message.instructions[0].data) == full
    assert list(after.message.account_keys) == list(before.message.account_keys)
    assert bytes(after.message.header) == bytes(before.message.header)
    assert bytes(after.message.instructions[0].accounts) == bytes(
        before.message.instructions[0].accounts
    )


def test_a_deposit_whose_data_is_not_a_truncation_is_refused() -> None:
    from gecko.providers.whirlpool import WhirlpoolPlanError
    from gecko.providers.whirlpool_position import complete_increase_data, increase_data

    plan = _deposit_plan()
    tampered = bytearray(increase_data(plan))
    tampered[10] ^= 0xFF  # a different maximum, not a shorter encoding
    with pytest.raises(WhirlpoolPlanError, match="neither"):
        complete_increase_data(_deposit_tx(bytes(tampered)), plan)


def test_a_run_refused_after_a_landed_leg_keeps_that_signature() -> None:
    """The position run that opened and was refused on the deposit still moved money once;
    its row must say so, or the log hides a landed transaction."""
    row = decision_row(
        trace=_trace("ok", "receipt-fail"),
        lane="position",
        network="mainnet",
        programs=("whirlpool",),
        terminal="refused",
        code="receipt-failed",
        signatures=("open-sig",),
    )
    assert row["signatures"] == ["open-sig"]

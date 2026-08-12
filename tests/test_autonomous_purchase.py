"""The single-call autonomous purchase loop — falsified offline, before any wire.

Pattern B: every claim here is checked against injected transports, so the loop can be
refuted without a fork, a key, or a cent. The fork run is the FINAL check, never the
debugger.

THE TRANSACTION BYTES ARE REAL IN SHAPE. They are assembled here with solders from the
account layout and the Anchor discriminator observed on a real
``let_me_buy make_purchase`` build (``c13ee38869d4c914`` == sha256("global:make_purchase")
[:8]), so ``decode_message`` sees a genuine message rather than a mock.

THE TOKEN BALANCE ROWS ARE MAINNET-SHAPED AND SYNTHESIZED, and that is called out because
it matters: surfpool 1.1.1 returns ``preTokenBalances``/``postTokenBalances`` as **null**
for every simulation, so a fork cannot produce a measured token leg at all. These fixtures
carry the shape the mainnet RPC returns. They prove what the loop does GIVEN a node that
reports balances; they are not evidence that the fork does.
"""

from __future__ import annotations

import base64
import inspect
from dataclasses import dataclass, field, fields
from typing import Any, get_args

import pytest

from gecko.autonomous_purchase import (
    PurchasePlan,
    PurchaseRefused,
    PurchaseSettled,
    RefusedRunHasNoSignature,
    default_spend_policy,
    run_purchase,
)
from gecko.signer import (
    DEVELOPER_KEYPAIR_FILE_PROFILE_NAME,
    SignerProfile,
    SigningAttestation,
    TransactionSigner,
)
from gecko.simulate import BuiltTx
from gecko.spend_policy import (
    InMemorySpendLedger,
    SpendPolicyGate,
    TokenCap,
    TokenCaps,
    VelocityDecision,
    VelocityLimits,
)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
PROGRAM = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
DISCRIMINATOR = bytes.fromhex("c13ee38869d4c914")

BUYER = "49KEkqP6eq7VhuFaSK6tLqxdg9bBTmDFRTFLiGQuwg75"
BUYER_ATA = "8zR8MmJjWweSKfwJSkZtdzQ2h3DoHHnXtZkyHSfXkPTx"
STORE_RECEIPTS = "H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V"
STORE_AUTHORITY = "8D8qFHBnvS6oMsJy7EmGTrpoZcGd3aCC3pnPLi93Ag2V"
STORE_TOKEN = "FaK5981JTnAbraeKQTjptKAHiF74Zy4upg2hoBdLnGyY"

FORK_BLOCKHASH = "6xCk4Xgb64QofLjfh5Q5sy47W5dURHagdcDWWhhoAqgo"
#: 0.1 USDC, in USDC's own raw base units (6 decimals). Never lamports.
PRICE_RAW = 100_000
SIGNATURE = "5" + "j" * 86


def _accounts(sender: str = BUYER_ATA, recipient: str = STORE_TOKEN) -> dict[str, str]:
    return {
        "receipts": STORE_RECEIPTS,
        "signer": BUYER,
        "authority": STORE_AUTHORITY,
        "mint": USDC,
        "sender_token_account": sender,
        "recipient_token_account": recipient,
        "token_program": TOKEN_PROGRAM,
        "system_program": SYSTEM_PROGRAM,
        "associated_token_program": ATA_PROGRAM,
    }


def _plan(sender: str = BUYER_ATA, recipient: str = STORE_TOKEN) -> PurchasePlan:
    return PurchasePlan(
        api_id="let_me_buy",
        instruction="make_purchase",
        accounts=_accounts(sender, recipient),
        args={"store_name": "jonasbar", "product_name": "Water", "table_number": 11},
        fee_payer=BUYER,
    )


def _built_tx(recipient: str = STORE_TOKEN) -> BuiltTx:
    """A real legacy transaction, assembled from the observed layout."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    def meta(address: str, *, writable: bool, signer: bool = False) -> AccountMeta:
        return AccountMeta(
            pubkey=Pubkey.from_string(address),
            is_signer=signer,
            is_writable=writable,
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
            meta(recipient, writable=True),
            meta(TOKEN_PROGRAM, writable=False),
            meta(SYSTEM_PROGRAM, writable=False),
            meta(ATA_PROGRAM, writable=False),
        ],
    )
    message = Message.new_with_blockhash(
        [instruction], Pubkey.from_string(BUYER), Hash.from_string(FORK_BLOCKHASH)
    )
    raw = bytes(Transaction.new_unsigned(message))
    return BuiltTx(tx=base64.b64encode(raw).decode(), encoding="base64")


def _token_rows(pre_raw: int, post_raw: int) -> dict[str, list[dict[str, Any]]]:
    """Mainnet-SHAPED balance rows. Surfpool returns null here; see the module docstring."""

    def row(index: int, owner: str, amount: int) -> dict[str, Any]:
        return {
            "accountIndex": index,
            "mint": USDC,
            "owner": owner,
            "programId": TOKEN_PROGRAM,
            "uiTokenAmount": {
                "amount": str(amount),
                "decimals": 6,
                "uiAmount": amount / 1e6,
                "uiAmountString": str(amount / 1e6),
            },
        }

    return {
        "preTokenBalances": [row(4, BUYER, pre_raw), row(5, STORE_AUTHORITY, 0)],
        "postTokenBalances": [
            row(4, BUYER, post_raw),
            row(5, STORE_AUTHORITY, PRICE_RAW),
        ],
    }


@dataclass
class FakeRpc:
    """A replaying transport. Records every method so the ORDER stays falsifiable."""

    sim_err: Any | None = None
    units: int = 42_494
    consumed_units: int = 42_494
    pre_raw: int = 10_000_000
    calls: list[str] = field(default_factory=list)

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.calls.append(method)
        if method == "getLatestBlockhash":
            return {
                "result": {
                    "value": {
                        "blockhash": FORK_BLOCKHASH,
                        "lastValidBlockHeight": 655,
                    }
                }
            }
        if method == "getAccountInfo":
            return {
                "result": {"value": {"lamports": 5_000_000_000, "data": ["", "base64"]}}
            }
        if method == "getRecentPrioritizationFees":
            return {"result": [{"slot": 1, "prioritizationFee": 1_000}]}
        if method == "simulateTransaction":
            value: dict[str, Any] = {
                "err": self.sim_err,
                "logs": ["Program BUYux... success"],
                "unitsConsumed": self.units,
                "accounts": [{"lamports": 4_999_995_000, "data": ["", "base64"]}],
            }
            value.update(_token_rows(self.pre_raw, self.pre_raw - PRICE_RAW))
            return {"result": {"context": {"slot": 438_746_259}, "value": value}}
        if method == "getSlot":
            return {"result": 438_746_260}
        if method == "sendTransaction":
            return {"result": SIGNATURE}
        if method == "getSignatureStatuses":
            return {
                "result": {
                    "value": [
                        {"confirmationStatus": "confirmed", "err": None, "slot": 1}
                    ]
                }
            }
        if method == "getTransaction":
            return {
                "result": {
                    "slot": 438_746_261,
                    "meta": {"err": None, "computeUnitsConsumed": self.consumed_units},
                }
            }
        raise AssertionError(f"unexpected RPC method {method}")


@dataclass
class FakeBackend:
    """A signing backend that records whether it was ever ASKED. Holds no real key."""

    pubkey_value: str = BUYER
    asked: int = 0

    @property
    def pubkey(self) -> str:
        return self.pubkey_value

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        self.asked += 1
        from solders.keypair import Keypair
        from solders.transaction import Transaction

        transaction = Transaction.from_bytes(unsigned_transaction)
        # A throwaway key: the loop must not care WHICH signature comes back, only that
        # the message it attested is the message that returns.
        signature = Keypair().sign_message(bytes(transaction.message))
        return bytes(Transaction.populate(transaction.message, [signature]))


@dataclass
class RecordingLedger:
    """Wraps the real ledger and counts how many times a spend was actually RESERVED.

    ``authorizations`` counts distinct gate consultations: the ledger is READ once per
    `authorize()` call, so a second consultation is visible here even when the reserve is
    suppressed by a wrapper."""

    inner: InMemorySpendLedger = field(default_factory=InMemorySpendLedger)
    reserves: int = 0

    def reserve(
        self,
        *,
        at: float,
        lamports: int,
        limits: VelocityLimits,
        tokens: tuple[Any, ...] = (),
        digest: str | None = None,
    ) -> VelocityDecision:
        self.reserves += 1
        return self.inner.reserve(
            at=at, lamports=lamports, limits=limits, tokens=tokens, digest=digest
        )


def _gate(token_cap_raw: int = 1_000_000, ledger: Any = None) -> SpendPolicyGate:
    policy = default_spend_policy(
        allowed_destinations=frozenset(
            {STORE_RECEIPTS, STORE_AUTHORITY, STORE_TOKEN, BUYER_ATA}
        ),
        usdc_per_transaction_raw=token_cap_raw,
    )
    return SpendPolicyGate(policy=policy, ledger=ledger or InMemorySpendLedger())


def _signer(gate: SpendPolicyGate, backend: FakeBackend) -> TransactionSigner:
    return TransactionSigner(
        backend=backend,
        # A member of the SignerProfileName vocabulary. "local" was not one — and the name
        # travels to the backend inside SigningAttestation.profile, which is what an
        # external signer keys its own policy on.
        profile=SignerProfile(
            name=DEVELOPER_KEYPAIR_FILE_PROFILE_NAME, network="fork", authorized=True
        ),
        spend_gate=gate,
    )


def _run(**overrides: Any) -> Any:
    backend = overrides.pop("backend", None) or FakeBackend()
    gate = overrides.pop("gate", None) or _gate()
    rpc = overrides.pop("rpc", None) or FakeRpc()
    plan = overrides.pop("plan", None) or _plan()
    built = overrides.pop("built", None) or _built_tx()
    builds: list[Any] = []

    def build_call(request: Any) -> BuiltTx:
        builds.append(request)
        return built

    outcome = run_purchase(
        network="fork",
        rpc_url="http://127.0.0.1:8999",
        plan=plan,
        signer=_signer(gate, backend),
        spend_gate=gate,
        build_call=build_call,
        rpc_call=rpc,
        **overrides,
    )
    return outcome, backend, rpc, builds


def test_a_clean_purchase_settles_and_reports_predicted_versus_consumed_units() -> None:
    outcome, backend, rpc, builds = _run()

    assert isinstance(outcome, PurchaseSettled)
    assert outcome.signature == SIGNATURE
    assert outcome.network == "fork"
    assert outcome.predicted_units == 42_494
    assert outcome.consumed_units == 42_494
    assert outcome.verdict.authorized is True
    assert outcome.receipt.status == "pass"
    assert backend.asked == 1
    assert len(builds) == 1, "the builder must be asked exactly once, never per-step"
    assert "sendTransaction" in rpc.calls


def test_the_self_transfer_plan_is_refused_before_the_builder_is_ever_called() -> None:
    outcome, backend, _rpc, builds = _run(plan=_plan(recipient=BUYER_ATA))

    assert isinstance(outcome, PurchaseRefused)
    assert outcome.code == "plan-refused"
    assert builds == [], "the builder was asked about a plan that was already refused"
    assert backend.asked == 0


def test_a_price_over_the_token_cap_is_refused_and_never_reaches_the_backend() -> None:
    # The cap is set BELOW the 0.1 USDC price, in USDC's own raw base units.
    outcome, backend, rpc, _builds = _run(gate=_gate(token_cap_raw=PRICE_RAW - 1))

    assert isinstance(outcome, PurchaseRefused)
    assert outcome.code == "spend-refused"
    assert outcome.verdict is not None
    assert outcome.verdict.code == "over-per-transaction-token-cap"
    assert backend.asked == 0
    assert "sendTransaction" not in rpc.calls


def test_a_refused_run_has_no_readable_signature() -> None:
    outcome, _backend, _rpc, _builds = _run(plan=_plan(recipient=BUYER_ATA))

    assert isinstance(outcome, PurchaseRefused)
    # There is no `signature` FIELD to assign, and reading the attribute raises rather than
    # handing back a falsy placeholder the next line of caller code would read as an answer.
    assert "signature" not in {f.name for f in fields(outcome)}
    with pytest.raises(RefusedRunHasNoSignature):
        _ = outcome.signature


def test_a_failing_receipt_refuses_before_the_spend_gate_is_consulted() -> None:
    outcome, backend, rpc, _builds = _run(
        rpc=FakeRpc(sim_err={"InstructionError": [0, {"Custom": 3012}]})
    )

    assert isinstance(outcome, PurchaseRefused)
    assert outcome.code == "receipt-failed"
    assert outcome.verdict is None, "a receipt that failed must not be priced"
    assert backend.asked == 0
    assert "sendTransaction" not in rpc.calls


def test_run_purchase_accepts_no_verdict_and_no_policy_parameter() -> None:
    """By NAME and by ANNOTATION — the decision is taken inside, from the configured gate.

    A caller that can hand in a verdict can hand in ``authorized=True``, and a caller that
    can hand in a policy can hand in a wider one. Neither may be expressible.
    """
    from gecko.spend_policy import SpendPolicy, SpendVerdict

    def mentions(annotation: Any, forbidden: type) -> bool:
        """Does this annotation carry ``forbidden`` anywhere — including inside a union?

        Compared by IDENTITY, walking type arguments. A substring test would be wrong in
        both directions: it flags ``SpendPolicyGate`` (which is the configuration seam and
        is allowed) and it misses an alias that spells the type another way.
        """
        if annotation is forbidden:
            return True
        return any(mentions(arg, forbidden) for arg in get_args(annotation))

    signature = inspect.signature(run_purchase)
    hints = inspect.get_annotations(run_purchase, eval_str=True)
    assert "spend_gate" in signature.parameters, "the gate seam must still be present"

    for name, parameter in signature.parameters.items():
        assert "verdict" not in name.lower(), f"{name} could carry a decision"
        annotation = hints.get(name, parameter.annotation)
        assert not mentions(annotation, SpendVerdict), (
            f"{name} can carry a SpendVerdict"
        )
        assert not mentions(annotation, SpendPolicy), f"{name} can carry a SpendPolicy"


def test_one_purchase_reserves_the_advisory_budget_exactly_once() -> None:
    """The gate is consulted twice — once here, once inside the signer — and the ledger
    must still be charged ONCE. Two rows per purchase would silently halve every rolling
    cap the human authored."""
    recording = RecordingLedger()
    outcome, _backend, _rpc, _builds = _run(gate=_gate(ledger=recording))

    assert isinstance(outcome, PurchaseSettled)
    assert recording.reserves == 1, (
        f"the advisory ledger was charged {recording.reserves} times for one purchase"
    )


def test_a_gate_that_is_not_the_signers_own_is_refused() -> None:
    from gecko.autonomous_purchase import PurchaseConfigurationError

    backend = FakeBackend()
    signer_gate = _gate()
    other_gate = _gate()

    with pytest.raises(PurchaseConfigurationError):
        run_purchase(
            network="fork",
            rpc_url="http://127.0.0.1:8999",
            plan=_plan(),
            signer=_signer(signer_gate, backend),
            spend_gate=other_gate,
            build_call=lambda _request: _built_tx(),
            rpc_call=FakeRpc(),
        )


def test_the_default_policy_names_usdc_in_raw_base_units() -> None:
    policy = default_spend_policy(allowed_destinations=frozenset({STORE_TOKEN}))
    assert policy.token_caps is not None
    cap = policy.token_caps.cap_for(USDC)
    assert isinstance(cap, TokenCap)
    assert cap.decimals == 6
    assert isinstance(policy.token_caps, TokenCaps)
    assert policy.max_transactions_per_day is not None
    assert policy.missing_fields() == ()


@dataclass
class _CountingGate:
    """Delegates to a real gate and counts how many times it was CONSULTED.

    The ledger cannot answer this question: `_ChargeOnceLedger` suppresses the second
    reserve, so a ledger-based count reads 1 whether the gate was asked once or twice.
    Counting the authorization itself is what distinguishes a fixed design from a
    repaired symptom.
    """

    inner: SpendPolicyGate = None  # type: ignore[assignment]
    ledger: Any = None
    #: A LIST, not an int: the loop calls `replace()` on the gate, and a copy would carry
    #: its own counter. The shared reference is what makes the second consultation visible.
    seen: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.ledger is None and self.inner is not None:
            self.ledger = self.inner.ledger

    @property
    def authorizations(self) -> int:
        return len(self.seen)

    def authorize(self, *args: Any, **kwargs: Any) -> Any:
        self.seen.append("authorize")
        return self.inner.authorize(*args, **kwargs)


def test_the_gate_is_consulted_exactly_once_per_purchase() -> None:
    """One purchase, one authorization — not two answers reconciled after the fact.

    `_ChargeOnceLedger` made the double-charge harmless by replaying the first decision to
    the second identical call. That is a repair, not a design: the budget was still asked
    twice, and any future caller wiring the gate itself would reintroduce the bug the
    wrapper exists to hide. The signer already computes a verdict; the fix is to publish
    it, so there is exactly one authorization per purchase and nothing to reconcile.
    """
    counting = _CountingGate(inner=_gate())
    outcome, _backend, _rpc, _builds = _run(gate=counting)

    assert isinstance(outcome, PurchaseSettled)
    assert counting.authorizations == 1, (
        f"the gate was consulted {counting.authorizations} times for one purchase; "
        "the signer's verdict must be reused, not recomputed"
    )
    assert outcome.verdict is not None and outcome.verdict.authorized is True

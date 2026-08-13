"""The spend verdict as a PRECONDITION of signing — G7, closed structurally.

WHAT WAS WRONG. ``SpendPolicy`` expressed four caps and ``gecko/signer.py`` never
consulted it. The ordering lived in a caller's docstring
(``examples/autonomous_signing_demo.py``: "so this ordering is the caller's to keep") and
in a test named after the mutation. That is a gate a caller MAY consult, not a state that
changes what a caller CAN do: a second caller — or the same caller after a refactor —
reaches ``sign`` with no verdict at all and the signer answers.

WHAT IS ASSERTED HERE. Not "the verdict was False". Every refusal below is asserted as
*the signing backend was never reached* (``backend.calls == []``), because a refusal taken
after the bytes reached the key holder is a label, not a control.

THE MUTATION THIS FILE EXISTS TO CATCH. Delete the ``self._spend_gate().authorize(...)``
block from ``TransactionSigner.sign`` and
``test_an_over_cap_spl_spend_is_refused_before_the_backend_is_reached`` goes red — the
25-USDC spend against a 1-USDC cap reaches the backend and comes back signed. The verbatim
run is in the commit message. A precondition with no test that fails when it is removed is
worse than no precondition, because it reads as one.

NOTHING HERE SIGNS OR BROADCASTS. The backend is a fake that holds no key and echoes the
bytes it was handed; every receipt asserts ``network="fork"``; no RPC is reached.
"""

from __future__ import annotations

import ast
import base64
import inspect
from dataclasses import replace
from pathlib import Path
from typing import Any, get_args

import pytest

from gecko.handoff import SignerHandoff
from gecko.landing import assemble_unsigned_tx
from gecko.signer import (
    EXTERNAL_SIGNER_PROFILE_NAME,
    SignerProfile,
    SignerRefused,
    SigningAttestation,
    TransactionSigner,
)
from gecko.simulate import (
    Receipt,
    TokenDeltaRefusal,
    TokenDeltaReport,
    TokenMovement,
    parse_token_deltas,
)
from gecko.spend_policy import (
    AllowedInstruction,
    InMemorySpendLedger,
    SpendPolicy,
    SpendPolicyGate,
    SpendVerdict,
    TokenCap,
    TokenCaps,
)
from gecko.txbind import message_binding

_SIGNER_SOURCE = Path(__file__).resolve().parent.parent / "gecko" / "signer.py"

PAYER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
FOREIGN = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"
#: The two token accounts the transfer touches. Addresses, not owners — see the spend
#: policy's module docstring for why cap 4 is written in the message's own vocabulary.
SOURCE_ATA = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
DEST_ATA = "6dqjqu2QYVEXpTGVFMYSqPqKNRTz9rE9uDGoDgvzYs9m"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
REAL_BLOCKHASH = "So11111111111111111111111111111111111111112"
SLOT = 300_000_000

#: ``transferChecked``. SPL Token dispatches on ONE byte, and the author declares the
#: width their program uses — an eight-byte entry here could never match.
TRANSFER_CHECKED = b"\x0c"

#: 25 USDC in raw base units. The number that reads as ~0 against a lamport cap: it is
#: 0.000025 SOL if you fold it, and 25 dollars if you do not.
TWENTY_FIVE_USDC = 25_000_000
ONE_USDC = 1_000_000


def _tx(
    *,
    payer: str = PAYER,
    program: str = TOKEN_PROGRAM,
    data: bytes = TRANSFER_CHECKED + b"\x40\x78\x7d\x01\x00\x00\x00\x00\x06",
    writable: tuple[str, ...] = (SOURCE_ATA, DEST_ATA),
    blockhash: str = REAL_BLOCKHASH,
) -> str:
    """An UNSIGNED transfer-shaped transaction, base64. No key exists in this helper."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    metas = [
        AccountMeta(pubkey=Pubkey.from_string(key), is_signer=False, is_writable=True)
        for key in writable
    ]
    instruction = Instruction(Pubkey.from_string(program), data, metas)
    return assemble_unsigned_tx([instruction], payer, blockhash=blockhash).tx


def _distinct_tx(index: int) -> str:
    """The Nth DIFFERENT transaction: one plan, its own blockhash, its own bytes.

    Needed since B3 made the spend reservation idempotent on exact bytes. Calling ``_tx()``
    N times used to stand in for "N transactions" and now correctly means "one transaction
    submitted N times" — at most one of which can settle, because they share a signature.
    """
    from solders.hash import Hash

    return _tx(blockhash=str(Hash.from_bytes(bytes([index + 1]) * 32)))


def _movement(
    *,
    mint: str = USDC,
    owner: str = PAYER,
    decimals: int = 6,
    delta_raw: int = -TWENTY_FIVE_USDC,
) -> TokenMovement:
    return TokenMovement(
        mint=mint,
        owner=owner,
        decimals=decimals,
        pre_raw=100_000_000,
        post_raw=100_000_000 + delta_raw,
        delta_raw=delta_raw,
        ui_delta=str(delta_raw),
    )


def _measured(*movements: TokenMovement) -> TokenDeltaReport:
    return TokenDeltaReport(status="measured", movements=movements, refusals=())


def _unmeasurable() -> TokenDeltaReport:
    return TokenDeltaReport(
        status="unmeasurable",
        movements=(),
        refusals=(
            TokenDeltaRefusal(
                reason="transfer-fee",
                mint=USDC,
                detail="the mint carries a transfer fee, so the delta is not the debit",
            ),
        ),
    )


def _receipt(
    tx: str,
    *,
    sol_delta: int | None = -5_000,
    token_delta: TokenDeltaReport | None = None,
    observed_slot: int | None = SLOT,
) -> Receipt:
    return Receipt(
        status="pass",
        err=None,
        revert_class=None,
        units_consumed=5_000,
        sol_delta=sol_delta,
        tokens_received=None,
        logs_tail=(),
        network_label="simulated (fork/RPC snapshot — not mainnet)",
        message_binding=message_binding(tx, strength="exact"),
        binding_strength="exact",
        lookup_resolution="none",
        network="fork",
        observed_slot=observed_slot,
        token_delta=token_delta if token_delta is not None else _measured(),
        # B2. This file's subject is the SPEND predicate, so every receipt here has to get
        # past the origin check to reach it — a file where everything refused for the wrong
        # reason would be a green suite proving nothing about caps. The refusal itself is
        # owned by ``tests/test_signer.py``.
        origin="simulated",
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


class _FakeBackend:
    """Holds no key, produces no signature: it echoes the bytes and counts the calls.

    The count is the only witness that can testify a refusal happened BEFORE the key
    holder was reached rather than after.
    """

    def __init__(self, pubkey: str = PAYER) -> None:
        self._pubkey = pubkey
        self.calls: list[SigningAttestation] = []

    @property
    def pubkey(self) -> str:
        return self._pubkey

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        self.calls.append(attestation)
        return unsigned_transaction


def _policy(**overrides: Any) -> SpendPolicy:
    base: dict[str, Any] = {
        "authorized": True,
        "per_transaction_cap_lamports": 10_000_000,
        "hourly_cap_lamports": 100_000_000,
        "daily_cap_lamports": 500_000_000,
        "max_transactions_per_day": 20,
        "allowed_instructions": frozenset(
            {
                AllowedInstruction(
                    program_id=TOKEN_PROGRAM, discriminator=TRANSFER_CHECKED
                )
            }
        ),
        "allowed_destinations": frozenset({SOURCE_ATA, DEST_ATA}),
        "token_caps": TokenCaps.of(
            (
                TokenCap(
                    mint=USDC,
                    decimals=6,
                    per_transaction_raw=ONE_USDC,
                    hourly_raw=2 * ONE_USDC,
                    daily_raw=4 * ONE_USDC,
                ),
            )
        ),
    }
    base.update(overrides)
    return SpendPolicy(**base)


def _gate(policy: SpendPolicy | None = None) -> SpendPolicyGate:
    return SpendPolicyGate(
        policy=policy if policy is not None else _policy(),
        ledger=InMemorySpendLedger(),
    )


def _signer(
    backend: _FakeBackend,
    *,
    spend_gate: SpendPolicyGate | None = _gate(),
    **kw: Any,
) -> TransactionSigner:
    return TransactionSigner(
        backend=backend,
        profile=SignerProfile(
            name=EXTERNAL_SIGNER_PROFILE_NAME, network="fork", authorized=True
        ),
        spend_gate=spend_gate,
        **kw,
    )


# ----------------------------------------------------------------------------------------
# THE PRECONDITION — the mutation test first, because it is the one that must go red
# ----------------------------------------------------------------------------------------


def test_an_over_cap_spl_spend_is_refused_before_the_backend_is_reached() -> None:
    """THE REQUIRED RED TEST. 25 USDC out against a 1-USDC per-transaction cap.

    MUTATION: delete the ``self._spend_gate().authorize(...)`` block from
    ``TransactionSigner.sign``. This test then goes red — the backend is reached once and
    a signed transaction comes back — and every other refusal test in this file stays
    green, because none of them is about the amount.

    The lamport legs are deliberately innocent: ``sol_delta`` is the 5,000-lamport
    signature fee, well under the 0.01-SOL per-transaction cap. A lamport-denominated
    policy authorises this transaction. The drain is entirely in the token leg.
    """
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx, token_delta=_measured(_movement()))

    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)

    assert excinfo.value.code == "spend-not-authorized"
    assert "over-per-transaction-token-cap" in excinfo.value.reason
    assert backend.calls == [], (
        "the bytes reached the key holder before the cap was read"
    )


def test_the_same_spend_under_the_cap_signs(recwarn: pytest.WarningsRecorder) -> None:
    """The control for the test above: identical path, 0.5 USDC instead of 25.

    Without this, "it refused" could be a signer that refuses everything, which is a
    passing test suite and a broken product.
    """
    del recwarn
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx, token_delta=_measured(_movement(delta_raw=-500_000)))

    signed = _signer(backend).sign(
        _handoff(tx, receipt), receipt=receipt, current_slot=SLOT
    )

    assert len(backend.calls) == 1
    assert signed.signer_pubkey == PAYER
    assert base64.b64decode(signed.signed_transaction_base64)


# ----------------------------------------------------------------------------------------
# ABSENCE OF A VERDICT IS A REFUSAL, NEVER A PASS
# ----------------------------------------------------------------------------------------


def test_a_signer_with_no_spend_gate_refuses_and_never_reaches_the_backend() -> None:
    """Mirrors ``_configuration()``: an unconfigured control is a refusal, not a permission.

    This is the case G7 actually shipped — a ``TransactionSigner`` built with a backend and
    a profile and no policy in sight. Before this change it signed.
    """
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx)

    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend, spend_gate=None).sign(
            _handoff(tx, receipt), receipt=receipt, current_slot=SLOT
        )

    assert excinfo.value.code == "spend-policy-not-configured"
    assert backend.calls == []


def test_a_gate_holding_no_policy_refuses() -> None:
    """Configured-but-unauthored is the same answer, from the gate's own vocabulary."""
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx)

    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend, spend_gate=SpendPolicyGate()).sign(
            _handoff(tx, receipt), receipt=receipt, current_slot=SLOT
        )

    assert excinfo.value.code == "spend-not-authorized"
    assert "no-policy" in excinfo.value.reason
    assert backend.calls == []


def test_a_policy_that_never_authored_the_token_caps_refuses() -> None:
    """ "There is no cap that is unlimited because nobody thought about it" — extended to
    the per-mint map. An unauthored one is ``policy-incomplete``, not "no token limit"."""
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx, token_delta=_measured(_movement()))

    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend, spend_gate=_gate(_policy(token_caps=None))).sign(
            _handoff(tx, receipt), receipt=receipt, current_slot=SLOT
        )

    assert excinfo.value.code == "spend-not-authorized"
    assert "token_caps" in excinfo.value.reason
    assert backend.calls == []


# ----------------------------------------------------------------------------------------
# THE VERDICT IS NOT A PARAMETER
# ----------------------------------------------------------------------------------------


def test_sign_has_no_parameter_through_which_a_verdict_or_a_policy_can_arrive() -> None:
    """S3. A signer that ACCEPTS a verdict is a signer whose caller mints one.

    The gate is held by the signer and called by the signer; there is no
    ``spend_verdict=``, no ``policy=``, and no ``skip_``/``force_`` anything.
    """
    signature = inspect.signature(TransactionSigner.sign, eval_str=True)
    params = signature.parameters

    forbidden_names = {
        "spend_verdict",
        "verdict",
        "policy",
        "spend_policy",
        "authorization",
        "authorized",
        "skip_spend_policy",
        "force",
    }
    assert not (set(params) & forbidden_names), f"a verdict can arrive: {set(params)}"

    def mentions(annotation: Any, forbidden: type) -> bool:
        """Does this annotation carry ``forbidden`` anywhere — including inside a union?

        Membership in a bare tuple only catches the annotation written bare. ``SpendVerdict
        | None`` is a ``types.UnionType``, equal to no member of the tuple, so it passed.
        One level of ``get_args`` is not enough either: it flattens ``list[SpendVerdict] |
        None`` to ``(list[SpendVerdict], NoneType)``, neither of which is the forbidden
        type. The recursion is what closes it.

        Compared by IDENTITY, as in the sibling guard at
        ``tests/test_autonomous_purchase.py``. A substring test would be wrong in both
        directions: it flags ``SpendPolicyGate`` where that is the allowed configuration
        seam, and it misses an alias that spells the type another way.
        """
        if annotation is forbidden:
            return True
        return any(mentions(arg, forbidden) for arg in get_args(annotation))

    for name, parameter in params.items():
        for forbidden in (SpendVerdict, SpendPolicy, SpendPolicyGate):
            assert not mentions(parameter.annotation, forbidden), (
                f"{name} can carry a {forbidden.__name__}"
            )


def test_the_gate_is_asked_over_the_REVERIFIED_bytes_not_the_handoff_bytes() -> None:
    """The TOCTOU this seam exists to close, checked on the source.

    ``handoff.transaction_base64`` is the caller's copy; ``verified.transaction_base64``
    is what came back out of ``verify_handoff`` at ``exact`` strength on this call. They
    are equal today only because ``verify_handoff`` returns the subject it was given —
    which is exactly the kind of "equal today" that a refactor ends. A behavioural test
    cannot separate them, so this one reads the call site.
    """
    tree = ast.parse(_SIGNER_SOURCE.read_text(encoding="utf-8"))
    authorize_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "authorize"
    ]
    assert authorize_calls, "the signer never calls authorize()"
    for call in authorize_calls:
        first = call.args[0]
        assert isinstance(first, ast.Attribute), ast.dump(first)
        assert first.attr == "transaction_base64"
        assert isinstance(first.value, ast.Name)
        assert first.value.id == "verified", (
            "the gate must read the POST-re-verification bytes, never the handoff's"
        )
        keywords = {keyword.arg for keyword in call.keywords}
        assert "agent_supplied_policy" in keywords


def test_the_agent_supplied_policy_argument_is_pinned_to_None_at_the_call_site() -> (
    None
):
    """It exists to be refused. The signer must pass the literal ``None``, never a value
    it received — a signer that forwards one has re-opened the widening path."""
    tree = ast.parse(_SIGNER_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "authorize"
        ):
            supplied = [
                keyword
                for keyword in node.keywords
                if keyword.arg == "agent_supplied_policy"
            ]
            assert len(supplied) == 1
            assert isinstance(supplied[0].value, ast.Constant)
            assert supplied[0].value.value is None


# ----------------------------------------------------------------------------------------
# NO LAMPORT FOLD — a raw SPL amount is not a lamport
# ----------------------------------------------------------------------------------------


def test_a_raw_spl_amount_is_never_compared_against_the_lamport_cap() -> None:
    """The 1000× error the fold would hide, stated as a test.

    25 USDC is 25,000,000 raw. Folded into lamports it is 0.025 SOL — under the 0.01-SOL
    cap? No: it is 2.5× over, and that ACCIDENT is what makes the fold look safe. So the
    lamport cap here is raised well above the raw number: a folded implementation passes
    this transaction, a per-mint one refuses it. The 1-USDC cap is what must bite.
    """
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx, token_delta=_measured(_movement()))
    generous = _policy(
        per_transaction_cap_lamports=10 * TWENTY_FIVE_USDC,
        hourly_cap_lamports=100 * TWENTY_FIVE_USDC,
        daily_cap_lamports=100 * TWENTY_FIVE_USDC,
    )

    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend, spend_gate=_gate(generous)).sign(
            _handoff(tx, receipt), receipt=receipt, current_slot=SLOT
        )

    assert "over-per-transaction-token-cap" in excinfo.value.reason
    assert backend.calls == []


def test_a_mint_the_policy_never_named_refuses_rather_than_passing_unmeasured() -> None:
    backend = _FakeBackend()
    tx = _tx()
    other_mint = "So11111111111111111111111111111111111111112"
    receipt = _receipt(
        tx, token_delta=_measured(_movement(mint=other_mint, delta_raw=-1))
    )

    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)

    assert "mint-not-allowlisted" in excinfo.value.reason
    assert backend.calls == []


def test_a_decimals_disagreement_between_the_cap_and_the_observed_mint_refuses() -> (
    None
):
    """The cap is written in RAW base units, so it is only meaningful at one scale.

    A cap authored for 6-decimal USDC compared against a movement the node reported at 2
    decimals is a cap off by 10,000×. That disagreement is a refusal, never a conversion:
    converting would mean trusting the node's decimals over the human's.
    """
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx, token_delta=_measured(_movement(decimals=2, delta_raw=-100)))

    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)

    assert "token-decimals-mismatch" in excinfo.value.reason
    assert backend.calls == []


# ----------------------------------------------------------------------------------------
# "COULD NOT MEASURE" IS NEVER "ZERO"
# ----------------------------------------------------------------------------------------


def test_an_unmeasurable_token_leg_refuses_rather_than_reading_zero() -> None:
    """S2's third state, mapped onto the existing amount-unresolvable path."""
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx, token_delta=_unmeasurable())

    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)

    assert "amount-unresolvable" in excinfo.value.reason
    assert backend.calls == []


def test_a_token_leg_that_was_never_tracked_refuses_on_a_token_capable_message() -> (
    None
):
    """``None`` means NOT TRACKED. On a message that invokes the token program, that is
    the drain we cannot see — and the stock ``simulateTransaction`` returns exactly this,
    which is the whole reason it may not read as zero."""
    backend = _FakeBackend()
    tx = _tx()
    # `replace` rather than the helper's default, because the helper deliberately attaches
    # an OBSERVED zero: "not tracked" has to be constructed explicitly here.
    receipt = replace(_receipt(tx), token_delta=None)

    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)

    assert "token-leg-not-measured" in excinfo.value.reason
    assert backend.calls == []


def test_a_receipt_with_no_lamport_delta_still_refuses() -> None:
    """The pre-existing amount rule, re-asserted through the new call path."""
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx, sol_delta=None, token_delta=_measured())

    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)

    assert "amount-unresolvable" in excinfo.value.reason
    assert backend.calls == []


# ----------------------------------------------------------------------------------------
# ORDERING — a transaction refused by a cheap check spends no velocity budget
# ----------------------------------------------------------------------------------------


#: WHY EVERY ORDERING TEST BELOW USES TWO DIFFERENT TRANSACTIONS.
#:
#: Both of these tests used to refuse one transaction and then re-sign THE SAME BYTES, and
#: that made them blind to the mutation they are named after. Since B3 the reservation is
#: idempotent on the exact bytes: whether or not the refused attempt reserved budget, the
#: second attempt is recognised as a replay of it and charges nothing, so the "one
#: transaction a day is still available" assertion passes either way. Measured, not
#: reasoned — moving ``_check_receipt_age`` and the fee-payer check below the spend gate
#: both left this file fully green.
#:
#: A second DISTINCT transaction is a second charge, which is the only version of this
#: assertion that can fail when the ordering is wrong.
_STALE_TX, _FRESH_TX = _distinct_tx(101), _distinct_tx(102)
_FOREIGN_TX, _OURS_TX = _distinct_tx(103), _distinct_tx(104)


def test_a_stale_receipt_is_refused_before_any_budget_is_reserved() -> None:
    """The age bound is cheaper than the ledger and must run first. If it did not, an
    attacker who cannot get anything signed could still exhaust the day's budget by
    replaying stale receipts."""
    backend = _FakeBackend()
    stale = _receipt(_STALE_TX, token_delta=_measured(_movement(delta_raw=-500_000)))
    gate = _gate(_policy(max_transactions_per_day=1))
    signer = _signer(backend, spend_gate=gate)

    with pytest.raises(SignerRefused) as excinfo:
        signer.sign(
            _handoff(_STALE_TX, stale), receipt=stale, current_slot=SLOT + 100_000
        )
    assert excinfo.value.code == "receipt-too-old"

    # The one transaction a day the policy allows is still available.
    fresh = _receipt(_FRESH_TX, token_delta=_measured(_movement(delta_raw=-500_000)))
    signed = signer.sign(_handoff(_FRESH_TX, fresh), receipt=fresh, current_slot=SLOT)
    assert signed.signer_pubkey == PAYER


def test_a_foreign_fee_payer_is_refused_before_any_budget_is_reserved() -> None:
    """The fee-payer check is the other cheap one. Same reasoning, different check."""
    backend = _FakeBackend(pubkey=FOREIGN)
    receipt = _receipt(
        _FOREIGN_TX, token_delta=_measured(_movement(delta_raw=-500_000))
    )
    gate = _gate(_policy(max_transactions_per_day=1))

    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend, spend_gate=gate).sign(
            _handoff(_FOREIGN_TX, receipt), receipt=receipt, current_slot=SLOT
        )
    assert excinfo.value.code == "fee-payer-not-controlled"

    ours = _FakeBackend()
    fresh = _receipt(_OURS_TX, token_delta=_measured(_movement(delta_raw=-500_000)))
    _signer(ours, spend_gate=gate).sign(
        _handoff(_OURS_TX, fresh), receipt=fresh, current_slot=SLOT
    )
    assert len(ours.calls) == 1


def test_the_velocity_windows_bind_across_signatures_in_the_mints_own_units() -> None:
    """A per-transaction cap of X is defeated by N transactions of X — in USDC too.

    Four half-USDC spends fit under the 2-USDC hourly token bound; the fifth does not,
    and it is refused at the seam with no bytes handed over.

    Each is a DISTINCT transaction. Four signatures over identical bytes would be four
    copies of one transaction, only one of which can ever land, and since B3 the ledger
    charges those once — correctly, and not what this test is about.
    """
    gate = _gate()
    for index in range(4):
        backend = _FakeBackend()
        tx = _distinct_tx(index)
        receipt = _receipt(tx, token_delta=_measured(_movement(delta_raw=-500_000)))
        _signer(backend, spend_gate=gate).sign(
            _handoff(tx, receipt), receipt=receipt, current_slot=SLOT
        )
        assert len(backend.calls) == 1, f"signature {index} should have been produced"

    fifth = _FakeBackend()
    tx = _distinct_tx(4)
    receipt = _receipt(tx, token_delta=_measured(_movement(delta_raw=-500_000)))
    with pytest.raises(SignerRefused) as excinfo:
        _signer(fifth, spend_gate=gate).sign(
            _handoff(tx, receipt), receipt=receipt, current_slot=SLOT
        )
    assert "over-hourly-token-cap" in excinfo.value.reason
    assert fifth.calls == []


# ----------------------------------------------------------------------------------------
# C13 — nothing here signs or broadcasts on any network
# ----------------------------------------------------------------------------------------


_BANNED_TOKENS = ("send" + "Transaction", "Key" + "pair", "secret" + "_key")


def test_neither_the_seam_nor_this_file_can_sign_or_broadcast() -> None:
    for path in (_SIGNER_SOURCE, Path(__file__)):
        source = path.read_text(encoding="utf-8")
        for token in _BANNED_TOKENS:
            assert token not in source, f"{path.name} names {token}"


def _balances_null() -> TokenDeltaReport:
    """The report a node's `preTokenBalances: null` produces.

    Built through the real parser rather than hand-assembled, so this pins the actual
    wire shape and not a guess about it. `null` is the node DECLINING to answer, which
    is not the node stating that nothing moved — see `gecko/simulate.py`.
    """
    report = parse_token_deltas({"preTokenBalances": None, "postTokenBalances": None})
    assert report is not None and report.status == "unmeasurable"
    assert [one.reason for one in report.refusals] == ["balances-null"]
    return report


def test_a_null_token_balance_array_refuses_rather_than_reading_zero() -> None:
    """The gate must not let a node's `null` become an observed zero outflow.

    `balances-null` is a newer member of the refusal vocabulary than this gate, so the
    handling is INHERITED — `outflows()` raises for any unmeasurable status — rather than
    written for it. Inherited is not proven, and a cap that reads zero from a node which
    declined to answer is exactly what this precondition exists to prevent.
    """
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx, token_delta=_balances_null())

    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)

    assert "amount-unresolvable" in excinfo.value.reason
    # Name the cause: "unmeasurable" alone does not tell an operator which of several
    # very different problems they are looking at.
    assert "balances-null" in excinfo.value.reason
    # Nothing reached the signing backend.
    assert backend.calls == []

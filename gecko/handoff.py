"""The signer handoff — a Receipt, the bytes it attests, and a verdict over someone else's.

Gecko never signs. Someone else does: Orquestra's orchestration, a keychain-backed MCP
signer, a custody provider. Everything between our simulation and their signature is
unverified, and that window is where a swapped recipient or a rewritten route lives.

WHY THIS IS TWO FUNCTIONS. ``txbind.evaluate_tx`` closes that window and shipped with no
callers outside its own module — a gate a caller MAY consult, not a state that changes
what a caller CAN do. The first fix gave it one caller: a single ``prepare_handoff`` that
built a transaction, simulated it, and then "verified" the very object it had captured.
Both sides of that comparison came from one variable. The inequality could not fire, the
refusal branch was unreachable, and the approval asserted only that a value equals
itself — a tautology wearing the word *verified*.

The fix is NOT an optional ``subject_tx`` parameter defaulting to the captured bytes:
that preserves the tautology on the default path and stamps it "independently verified",
which is strictly worse than an honest label. So the two jobs became two functions,
because they answer different questions and only one of them is a claim about safety:

* :func:`prepare_handoff` — build + simulate. Returns the bytes WE simulated and the
  Receipt for them. **No verification claim**, and deliberately no ``approved`` field.
  Its honest sentence is "these are the bytes we simulated, and the simulation passed".
* :func:`verify_handoff` — takes bytes from SOMEWHERE ELSE (the signer, the orchestrator,
  a resubmission) and decides whether the Receipt attests them. Subject bytes are a
  required positional, and ``require`` is keyword-only with **no default**, so no caller
  drifts into a strength nobody chose. There is no path to ``approved=True`` over bytes
  this module produced itself.

RESIDUAL, stated rather than buried. Under the ``structural`` default the blockhash is
normalised out of the binding, so a subject that differs from the simulated transaction
ONLY in its blockhash matches, is approved, and is returned — bytes that were never
simulated. That is the honest ceiling of a ``replaceRecentBlockhash:true`` simulation,
not a bug in this module, and closing it is D4's job. What keeps it survivable meanwhile:
:func:`verify_handoff` returns the SUBJECT bytes, never the simulated ones, so a caller
signs exactly what was checked.

Two conversions live here because both have bitten a live handoff:

* **base58 → base64.** Orquestra's ``/build`` returns base58; ``@orquestradev/signer-mcp``
  does ``Buffer.from(tx, 'base64')``. Passing the builder's string straight through is a
  decode failure at the far end, discovered on camera.
* **``transaction`` vs ``serializedTransaction``.** ``/build`` returns both. The plain
  ``transaction`` field is base58 of a JSON instruction plan — not a transaction at all,
  and it exceeds the 1232-byte limit. ``simulate._default_build_call`` already prefers the
  right one; this module inherits that and does not re-implement the choice.

Control plane only: transactions are returned to the caller, never stored, never logged.
No key is read, nothing is signed, nothing is sent.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .simulate import BuildCall, BuiltTx, Receipt, RpcCall, simulate
from .txbind import BindingStrength, TxDecodeError, _decode, evaluate_tx

__all__ = ["PreparedPlan", "SignerHandoff", "prepare_handoff", "verify_handoff"]


@dataclass(frozen=True)
class PreparedPlan:
    """The bytes we simulated, and the Receipt for them. NOT a verification.

    There is no ``approved`` field on purpose. Nothing here was checked against an
    independent input — the transaction was built, simulated, and handed back, and the
    strongest true sentence is "this simulated ``status``". To decide whether the
    transaction in front of a signer is this one, pass it to :func:`verify_handoff`.

    ``simulated_transaction_base64`` is populated only when the simulation passed AND the
    Receipt carries a binding, so a plan that reverted or that we could not decode never
    puts bytes in front of anyone.
    """

    #: The simulation passed and the Receipt binds these bytes. Not "safe to sign".
    simulation_passed: bool
    reason: str
    #: Base64 compiled wire transaction, UNSIGNED — the bytes THIS run simulated.
    #: ``None`` whenever the simulation did not pass or the Receipt binds nothing.
    simulated_transaction_base64: str | None
    #: The Receipt, carried out whole so a later :func:`verify_handoff` has something to
    #: check against without re-simulating (a second builder POST returns a different
    #: transaction — see the capture note in :func:`prepare_handoff`).
    receipt: Receipt

    @property
    def binding(self) -> str | None:
        """sha256 binding the Receipt attests. ``None`` when it could not be computed."""
        return self.receipt.message_binding

    @property
    def strength(self) -> str | None:
        """How much of the message the binding covers — ``structural`` omits blockhash."""
        return self.receipt.binding_strength

    @property
    def status(self) -> str:
        return self.receipt.status

    @property
    def revert_class(self) -> str | None:
        return self.receipt.revert_class

    @property
    def units_consumed(self) -> int | None:
        return self.receipt.units_consumed

    @property
    def network_label(self) -> str:
        return self.receipt.network_label

    @property
    def logs_tail(self) -> tuple[str, ...]:
        return self.receipt.logs_tail

    @property
    def receipt_line(self) -> str:
        """One legible line for a terminal or a log — no values, no payload."""
        units = f"{self.units_consumed:,} CU" if self.units_consumed else "CU unknown"
        outcome = "SIMULATED PASS" if self.simulation_passed else "NOT HANDED OVER"
        return f"{outcome} · simulated {self.status} · {units} · {self.reason}"


@dataclass(frozen=True)
class SignerHandoff:
    """What a signer may act on, and why — or the refusal that replaces it.

    ``approved`` is the only field a caller should branch on. ``transaction_base64`` is
    populated if and only if ``approved`` is true, so a caller that forgets to check the
    flag still cannot reach unapproved bytes. Those bytes are the SUBJECT that was
    verified, re-encoded to base64 and never substituted for anything else.
    """

    approved: bool
    reason: str
    #: Base64 of the SUBJECT transaction, UNSIGNED — exactly the bytes that were checked,
    #: which is what ``sign_transaction`` consumes. ``None`` on every refusal.
    transaction_base64: str | None
    #: sha256 binding the Receipt attests. ``None`` when it could not be computed.
    binding: str | None
    #: How much of the message the binding covers — ``structural`` omits the blockhash.
    strength: str | None
    #: Carried through so a caller can show the receipt beside the verdict.
    status: str
    revert_class: str | None
    units_consumed: int | None
    network_label: str
    logs_tail: tuple[str, ...] = ()

    @property
    def receipt_line(self) -> str:
        """One legible line for a terminal or a log — no values, no payload."""
        units = f"{self.units_consumed:,} CU" if self.units_consumed else "CU unknown"
        verdict = "APPROVED" if self.approved else "REFUSED"
        return f"{verdict} · simulated {self.status} · {units} · {self.reason}"


def prepare_handoff(
    plan: Mapping[str, Any],
    *,
    rpc_url: str,
    rpc_call: RpcCall | None = None,
    build_call: BuildCall | None = None,
    track: Sequence[str] = (),
    replace_blockhash: bool = True,
) -> PreparedPlan:
    """Build ``plan``, simulate it, and return the bytes that were simulated.

    This function VERIFIES NOTHING — it has no independent input to verify against. It
    withholds the transaction when the simulation did not pass or when the Receipt binds
    nothing, because bytes nobody can later check do not belong in front of a signer.

    ``replace_blockhash=False`` against a fresh blockhash is what earns the Receipt an
    ``exact`` binding; the default ``True`` is the honest ceiling for a plan-shaped check
    and yields ``structural``. Whether that is strong enough is the CALLER's call, made at
    :func:`verify_handoff` where the ``require`` argument has no default.

    Never raises for a bad build or an undecodable transaction: a handoff that throws is
    one someone wraps in ``try/except`` and bypasses. Every failure path withholds bytes.
    """
    # CAPTURE, never rebuild. `simulate` returns a Receipt, not the transaction, and the
    # obvious fix — call the builder again for the bytes — is wrong twice over: it is a
    # second POST to the builder, and `/build` embeds a fresh `recentBlockhash` each time,
    # so the second transaction is not the one that was simulated. A `structural` binding
    # would hide that (it normalises the blockhash out) and an `exact` one would refuse a
    # perfectly good plan.
    #
    # Capturing is right HERE precisely because this function makes no verification
    # claim. What it may not do is feed the captured object back into `evaluate_tx` and
    # call the result a check — that was the tautology this split removes.
    captured: list[BuiltTx] = []

    def capturing_build(inner_plan: Mapping[str, Any]) -> BuiltTx:
        built = (build_call or _lazy_default_build())(inner_plan)
        captured.append(built)
        return built

    receipt = simulate(
        plan,
        rpc_url=rpc_url,
        rpc_call=rpc_call,
        build_call=capturing_build,
        track=track,
        replace_blockhash=replace_blockhash,
    )
    if not captured:  # pragma: no cover - simulate always builds before it simulates
        return _withheld(receipt, "the builder produced no transaction")
    built = captured[-1]

    if receipt.status != "pass":
        return _withheld(receipt, f"simulation did not pass (status={receipt.status})")
    if not receipt.message_binding or receipt.binding_strength not in (
        "structural",
        "exact",
    ):
        # No binding means no later verification is possible, so these bytes could only
        # ever be signed on trust. Withhold them rather than launder them.
        return _withheld(
            receipt, "the receipt carries no binding — these bytes attest nothing"
        )

    try:
        payload = base64.b64encode(_decode(built.tx, built.encoding)).decode()
    except TxDecodeError:
        # `simulate` computed a binding, so it decoded once already; this is unreachable
        # in practice. It stays a withholding, never a crash.
        return _withheld(receipt, "the transaction could not be re-encoded")

    return PreparedPlan(
        simulation_passed=True,
        reason="simulation passed — these are the bytes that were simulated",
        simulated_transaction_base64=payload,
        receipt=receipt,
    )


def verify_handoff(
    tx: str | bytes,
    receipt: Receipt,
    *,
    require: BindingStrength,
    encoding: str = "base64",
) -> SignerHandoff:
    """Is ``tx`` — supplied from outside — the transaction ``receipt`` attests?

    ``tx`` is the SUBJECT: bytes that came back from a signer, an orchestrator, or a
    resubmission. It is a required positional and there is no fallback to anything this
    module built, because a "verification" whose subject defaults to the object under
    test is not a verification.

    ``require`` is keyword-only with **no default**. Every caller states the strength it
    accepts: a default would be a strength nobody chose, and the only safe-by-omission
    value (``exact``) refuses every fork simulation, so the tempting default is the weak
    one. ``structural`` does NOT cover the blockhash — see the residual in the module
    docstring.

    On approval ``transaction_base64`` is the SUBJECT re-encoded, never the simulated
    bytes: returning what we simulated after checking something else re-opens the window
    from the other side, with the caller signing bytes that were not the subject of the
    check.

    Never raises: every failure path is ``approved=False`` with no payload.
    """
    verdict = evaluate_tx(tx, receipt, encoding=encoding, require=require)

    payload: str | None = None
    if verdict.approved:
        try:
            payload = base64.b64encode(_decode(tx, encoding)).decode()
        except TxDecodeError:
            # Decoded once already inside `evaluate_tx`, so this is unreachable in
            # practice — but a handoff that cannot produce bytes is a refusal, not a
            # crash, and never an approval with `None` in the payload slot.
            return _refused(
                receipt, "approved but the transaction could not be re-encoded"
            )

    return SignerHandoff(
        approved=verdict.approved,
        reason=verdict.reason,
        transaction_base64=payload,
        binding=receipt.message_binding,
        strength=receipt.binding_strength,
        status=receipt.status,
        revert_class=receipt.revert_class,
        units_consumed=receipt.units_consumed,
        network_label=receipt.network_label,
        logs_tail=receipt.logs_tail,
    )


def _lazy_default_build() -> BuildCall:
    """The shipped builder, imported at call time to keep the module import-light."""
    from .simulate import _default_build_call

    return _default_build_call


def _withheld(receipt: Receipt, reason: str) -> PreparedPlan:
    return PreparedPlan(
        simulation_passed=False,
        reason=reason,
        simulated_transaction_base64=None,
        receipt=receipt,
    )


def _refused(receipt: Receipt, reason: str) -> SignerHandoff:
    return SignerHandoff(
        approved=False,
        reason=reason,
        transaction_base64=None,
        binding=receipt.message_binding,
        strength=receipt.binding_strength,
        status=receipt.status,
        revert_class=receipt.revert_class,
        units_consumed=receipt.units_consumed,
        network_label=receipt.network_label,
        logs_tail=receipt.logs_tail,
    )

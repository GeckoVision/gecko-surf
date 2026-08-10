"""The signer handoff — a Receipt, a verdict, and the exact bytes, or none of them.

Gecko never signs. Someone else does: Orquestra's orchestration, a keychain-backed MCP
signer, a custody provider. Everything between our simulation and their signature is
unverified, and that window is where a swapped recipient or a rewritten route lives.

``txbind.evaluate_tx`` closes that window and shipped with **no callers outside its own
module**. That is this repo's recurring shape, and it is worth naming precisely rather
than filing as an oversight: the gate was a function a caller MAY consult, not a state
that changes what a caller CAN do. The zero-effort path — take the builder's string, hand
it to the signer — bypassed it completely, and the zero-effort path is the one that ships.

So this module returns ONE object holding both the verdict and the payload, and
``transaction_base64`` is ``None`` on every refusal. There is no code path that yields
signable bytes without a passing, bound Receipt. That is the difference between a gate and
a suggestion.

Two conversions live here because both have bitten a live handoff:

* **base58 → base64.** Orquestra's ``/build`` returns base58; ``@orquestradev/signer-mcp``
  does ``Buffer.from(tx, 'base64')``. Passing the builder's string straight through is a
  decode failure at the far end, discovered on camera.
* **``transaction`` vs ``serializedTransaction``.** ``/build`` returns both. The plain
  ``transaction`` field is base58 of a JSON instruction plan — not a transaction at all,
  and it exceeds the 1232-byte limit. ``simulate._default_build_call`` already prefers the
  right one; this module inherits that and does not re-implement the choice.

Control plane only: the transaction is returned to the caller, never stored, never logged.
No key is read, nothing is signed, nothing is sent.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .simulate import BuildCall, BuiltTx, Receipt, RpcCall, simulate
from .txbind import BindingStrength, TxDecodeError, _decode, evaluate_tx

__all__ = ["SignerHandoff", "prepare_handoff"]


@dataclass(frozen=True)
class SignerHandoff:
    """What a signer may act on, and why — or the refusal that replaces it.

    ``approved`` is the only field a caller should branch on. ``transaction_base64`` is
    populated if and only if ``approved`` is true, so a caller that forgets to check the
    flag still cannot reach unapproved bytes.
    """

    approved: bool
    reason: str
    #: Base64 compiled wire transaction, UNSIGNED — exactly what `sign_transaction`
    #: consumes. ``None`` on every refusal.
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
    require: BindingStrength = "structural",
    replace_blockhash: bool = True,
) -> SignerHandoff:
    """Build ``plan``, simulate it, and hand back signable bytes only if it passed.

    ``require`` is the minimum binding strength accepted. ``structural`` is the honest
    ceiling for a simulation that replaced the blockhash; ``exact`` additionally covers the
    blockhash and is available only with ``replace_blockhash=False`` against a fresh one —
    and it expires with that blockhash, roughly a minute.

    Never raises for a bad build or an undecodable transaction: a signing gate that throws
    is a gate someone wraps in ``try/except`` and bypasses. Every failure path is a refusal.
    """
    # CAPTURE, never rebuild. `simulate` returns a Receipt, not the transaction, and the
    # obvious fix — call the builder again for the bytes — is wrong twice over: it is a
    # second POST to the builder, and `/build` embeds a fresh `recentBlockhash` each time,
    # so the second transaction is not the one that was simulated. A `structural` binding
    # would hide that (it normalises the blockhash out) and an `exact` one would refuse a
    # perfectly good plan. Wrapping the builder keeps the verified bytes and the simulated
    # bytes the same object.
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
        return _refused(receipt, "the builder produced no transaction to verify")
    built = captured[-1]
    verdict = evaluate_tx(built.tx, receipt, encoding=built.encoding, require=require)

    payload: str | None = None
    if verdict.approved:
        try:
            payload = base64.b64encode(_decode(built.tx, built.encoding)).decode()
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

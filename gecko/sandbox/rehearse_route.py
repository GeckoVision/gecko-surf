"""Two legs, one buyer who never holds SOL: convert, then buy, judged as one route.

The purchase rehearsal proves one instruction. The story Gecko tells is two: the buyer holds
the wrong token, Gecko converts it on a venue it can prove, then pays the store in the token
the store takes, and a relay pays every fee. This module runs the two legs on a proven
surfnet in that order and judges what only the pair can show: the buyer's SOL is untouched
across BOTH landings, the second leg spends what the first produced, and the relay paid
exactly two fees.

Leg 1 is :func:`gecko.sandbox.rehearse_instruction.rehearse_instruction` with a relay; leg 2
is :func:`gecko.sandbox.rehearse.rehearse_purchase` with ``prefunded=True`` so nothing tops
the buyer up between the legs. Nothing here builds, signs or sends on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..relay import FeePayerRelay
from ..rpc import RpcCall
from ..simulate import BuildCall
from ..trace import Trace
from .rehearse import LamportDelta, Rehearsal, rehearse_purchase
from .rehearse_instruction import InstructionRehearsal, rehearse_instruction
from .surfnet import EphemeralSigner, SurfnetProof

__all__ = ["RouteLeg", "RouteRehearsal", "rehearse_gasless_route"]


@dataclass(frozen=True)
class RouteLeg:
    """The convert leg, as `prepare_instruction` wants it, plus what to fund first."""

    program_id: str
    instruction: str
    values: Mapping[str, Any]
    #: ``[(mint, raw_amount[, token_program]), …]`` placed on the buyer before leg 1.
    fund_tokens: Sequence[tuple[str, int] | tuple[str, int, str]]
    idl_fetch: Any
    build_call: Any


@dataclass(frozen=True)
class RouteRehearsal:
    convert: InstructionRehearsal
    purchase: Rehearsal | None
    buyer_sol: LamportDelta
    relay_sol: LamportDelta
    #: Empty is the only passing value.
    objections: tuple[str, ...] = field(default_factory=tuple)

    @property
    def landed(self) -> bool:
        return self.convert.landed and bool(self.purchase and self.purchase.landed)


def rehearse_gasless_route(
    proof: SurfnetProof,
    *,
    buyer: EphemeralSigner,
    relay: FeePayerRelay,
    convert: RouteLeg,
    store: str,
    product: str,
    table_number: int = 0,
    rpc_call: RpcCall | None = None,
    purchase_build_call: BuildCall | None = None,
    trace: Trace | None = None,
) -> RouteRehearsal:
    """Convert, then buy, both relay-paid; judge the pair by the ledger."""
    from ..rpc import default_rpc_call
    from .rehearse import _account, _lamports

    call = rpc_call or default_rpc_call
    buyer_before = _lamports(_account(call, proof.rpc_url, buyer.pubkey))
    relay_before = _lamports(_account(call, proof.rpc_url, relay.pubkey))
    log = trace or Trace(lane="route", network="fork")

    with log.step("convert", "gecko") as facts:
        first = rehearse_instruction(
            proof,
            signer=buyer,
            program_id=convert.program_id,
            instruction=convert.instruction,
            values=convert.values,
            idl_fetch=convert.idl_fetch,
            build_call=convert.build_call,
            fund_tokens=convert.fund_tokens,
            rpc_call=call,
            relay=relay,
        )
        facts["units"] = first.compute_units
        if not first.landed:
            facts["outcome"] = (
                first.refusals[0].reason.split(":")[0]
                if first.refusals
                else "not-landed"
            )

    objections: list[str] = []
    second: Rehearsal | None = None
    if not first.landed:
        objections.append(
            "the convert leg did not land; the purchase leg was not attempted"
        )
    else:
        second = rehearse_purchase(
            proof,
            buyer=buyer,
            store=store,
            product=product,
            table_number=table_number,
            rpc_call=call,
            build_call=purchase_build_call,
            relay=relay,
            trace=log,
            prefunded=True,
        )
        if not second.landed:
            objections.append("the purchase leg did not land")
        elif second.discrepancies:
            objections.extend(f"purchase: {line}" for line in second.discrepancies)

    buyer_sol = LamportDelta(
        address=buyer.pubkey,
        before=buyer_before,
        after=_lamports(_account(call, proof.rpc_url, buyer.pubkey)),
    )
    relay_sol = LamportDelta(
        address=relay.pubkey,
        before=relay_before,
        after=_lamports(_account(call, proof.rpc_url, relay.pubkey)),
    )
    buyer_moved = (buyer_sol.after or 0) - (buyer_sol.before or 0)
    if buyer_moved != 0:
        objections.append(
            f"the buyer's SOL moved {buyer_moved} across the route; a gasless route "
            f"leaves it untouched"
        )
    fees = [
        delta
        for delta in (first.relay_sol, second.relay_sol if second else None)
        if delta is not None and delta.moved is not None
    ]
    paid = [-(delta.moved or 0) for delta in fees if (delta.moved or 0) < 0]
    if len(paid) < (2 if second is not None else 1):
        objections.append(
            f"the relay paid {len(paid)} fee(s) for {2 if second else 1} landing(s)"
        )
    return RouteRehearsal(
        convert=first,
        purchase=second,
        buyer_sol=buyer_sol,
        relay_sol=relay_sol,
        objections=tuple(objections),
    )

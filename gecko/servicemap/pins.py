"""The payTo a payer may send to, read from the registry's last challenge probe.

Measured 9 Oct: one x402 host (Orthogonal's) names a DIFFERENT Solana payTo per provider,
so one pinned address refused every provider but the first. The pin is now per
operation: the payTo of the most recently probed offer for that operation on the
payer's network and asset (:mod:`gecko.servicemap.probe` writes it). A payer refuses
when there is no such offer, and while the operation is quarantined because its payTo
changed (a probe saw a new address; it stands only after a second probe past the hold,
or the founder's :func:`confirm_pay_to`).

The challenge a payer reads at call time is attacker-influenceable; this pin is the
anchor it is checked against, and it never comes from the challenge being paid.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable

from gecko.x402_svm import SOLANA_MAINNET

from .ids import op_id, url_template
from .registry import Registry, RegistryError
from .types import Event, OfferObs

__all__ = [
    "SOLANA_USDC",
    "PayToPinError",
    "confirm_pay_to",
    "latest_offer",
    "op_for_call",
    "pay_to_resolver",
    "pinned_pay_to",
    "pinned_pay_tos",
]

#: Mainnet USDC. Repeated here (not imported from the purchase path) so the registry
#: package does not pull the Solana purchase engine in.
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class PayToPinError(RegistryError):
    """No payTo may be pinned for this operation. Names the reason, never a secret."""


def op_for_call(method: str, service_id: str, path: str) -> str:
    """The registry's op id for a call a payer is about to make."""
    return op_id(method, url_template(service_id, path))


def latest_offer(
    offers: Iterable[OfferObs],
    *,
    network: str = SOLANA_MAINNET,
    asset: str = SOLANA_USDC,
    scheme: str = "exact",
) -> OfferObs | None:
    """The most recently seen offer on ``network`` in ``asset``. Ties (two payTos seen
    in one probe) break on the address, so the answer never depends on read order."""
    wanted = [
        o for o in offers if (o.network, o.asset, o.scheme) == (network, asset, scheme)
    ]
    return max(wanted, key=lambda o: (o.last_seen, o.pay_to), default=None)


def pinned_pay_to(
    registry: Registry,
    op: str,
    *,
    network: str = SOLANA_MAINNET,
    asset: str = SOLANA_USDC,
) -> str:
    """The payTo to pin for ``op``, or :class:`PayToPinError`."""
    health = registry.health_for(op)
    if health is not None and health.status == "quarantined":
        raise PayToPinError(
            "this operation is quarantined: its payTo changed and has not been "
            "confirmed (a second probe after the hold, or the founder)"
        )
    offer = latest_offer(registry.offers_for(op), network=network, asset=asset)
    if offer is None:
        raise PayToPinError(
            "the registry holds no probed offer for this operation on this network; "
            "run scripts/servicemap_probe.py --write first"
        )
    return offer.pay_to


def pinned_pay_tos(
    registry: Registry,
    *,
    network: str = SOLANA_MAINNET,
    asset: str = SOLANA_USDC,
    only: Iterable[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Every payTo a payer may be pinned to now -> the services that use it, sorted.
    Quarantined operations are left out: a policy must not widen to an address that
    is not yet trusted. ``only`` keeps the services named."""
    keep = None if only is None else set(only)
    services = {o.op_id: o.service_id for o in registry.operations()}
    by_op: dict[str, list[OfferObs]] = {}
    for row in registry.offers():
        by_op.setdefault(row.op_id, []).append(row)
    quarantined = {h.op_id for h in registry.health() if h.status == "quarantined"}
    found: dict[str, set[str]] = {}
    for op, offers in by_op.items():
        service = services.get(op)
        if service is None or op in quarantined:
            continue
        if keep is not None and service not in keep:
            continue
        offer = latest_offer(offers, network=network, asset=asset)
        if offer is not None:
            found.setdefault(offer.pay_to, set()).add(service)
    return {pay_to: tuple(sorted(s)) for pay_to, s in sorted(found.items())}


def confirm_pay_to(
    registry: Registry,
    service_id: str,
    pay_to: str,
    *,
    at: str,
    network: str = SOLANA_MAINNET,
    asset: str = SOLANA_USDC,
) -> list[str]:
    """The founder confirms ``pay_to`` for ``service_id``: every quarantined operation
    of that service whose latest offer names exactly that address is lifted. Returns
    the op ids lifted; an address that is not the latest one lifts nothing."""
    lifted: list[str] = []
    ops = [o.op_id for o in registry.operations() if o.service_id == service_id]
    for op in sorted(ops):
        health = registry.health_for(op)
        if health is None or health.status != "quarantined":
            continue
        offer = latest_offer(registry.offers_for(op), network=network, asset=asset)
        if offer is None or offer.pay_to != pay_to:
            continue
        registry.put_health(
            dataclasses.replace(health, status="up", quarantined_until=None)
        )
        registry.append_event(
            Event(
                at=at,
                kind="status",
                op_id=op,
                service_id=service_id,
                catalog=None,
                before="quarantined",
                after="up",
                note="payTo confirmed by the founder",
            )
        )
        lifted.append(op)
    return lifted


def pay_to_resolver(
    registry: Registry,
    *,
    network: str = SOLANA_MAINNET,
    asset: str = SOLANA_USDC,
) -> Callable[[str, str, str], str]:
    """``(method, service_id, path) -> payTo``, read at call time, so a quarantine
    written by a probe takes effect on the next payment."""

    def pay_to_for(method: str, service_id: str, path: str) -> str:
        op = op_for_call(method, service_id, path)
        return pinned_pay_to(registry, op, network=network, asset=asset)

    return pay_to_for

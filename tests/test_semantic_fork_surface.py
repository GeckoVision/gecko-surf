"""The fork StoreSurface adapter — falsifiable offline, no surfpool needed.

The seams (rpc_call, the surfnet proof, the rehearsal loop) are injected, so
the mapping this module owns — item_id -> product name -> on-chain price/mint/
authority, and Rehearsal -> SpendResult — is tested with a fake fork. The live
run against a booted surfpool is scripts/semantic_run.py (a plan, not a test).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from gecko.sandbox.rehearse import LamportDelta, Rehearsal
from gecko.semantic_catalogue import get_item
from gecko.semantic_fork_surface import ForkStoreSurface, ForkSurfaceError
from gecko.semantic_gate import ProposedPurchase

# Reuse the store encoder the directory tests already trust.
from tests.test_store_directory import AUTHORITY, USDC, encode_store

STORE_ADDR = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"


def _store_bytes() -> bytes:
    # Three catalogue items on-chain, priced as the semantic catalogue declares.
    products = [
        (get_item("brewed-coffee").name, get_item("brewed-coffee").price_lamports, 9),
        (get_item("still-water").name, get_item("still-water").price_lamports, 9),
        (get_item("cappuccino").name, get_item("cappuccino").price_lamports, 9),
    ]
    return encode_store(
        "geckocoffee", products=products, authority=AUTHORITY, mint=USDC
    )


class FakeStoreRpc:
    """Returns the geckocoffee account bytes for getAccountInfo."""

    def __init__(self, raw: bytes) -> None:
        self.encoded = base64.b64encode(raw).decode()

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        return {
            "value": {"data": [self.encoded, "base64"], "owner": "x", "lamports": 1}
        }


@dataclass
class FakeProof:
    """Stands in for SurfnetProof — only rpc_url is read by the surface."""

    rpc_url: str = "http://127.0.0.1:8899"


@dataclass
class RehearsalRecorder:
    """A rehearse_purchase stand-in with a scriptable outcome."""

    landed: bool = True
    discrepancies: tuple[str, ...] = ()
    moved: int | None = -4_005_000
    mint: str = USDC
    calls: list[str] = field(default_factory=list)

    def __call__(
        self, proof: Any, *, buyer: Any, store: str, product: str, **kw: Any
    ) -> Rehearsal:
        self.calls.append(product)
        buyer_sol = None if self.moved is None else LamportDelta("buyer", 0, self.moved)
        return Rehearsal(
            store=store,
            product=product,
            price_raw=4_000_000,
            mint=self.mint,
            buyer="buyer",
            landed=self.landed,
            buyer_sol=buyer_sol,
            discrepancies=self.discrepancies,
        )


def _surface(
    monkeypatch: pytest.MonkeyPatch, rehearsal: RehearsalRecorder, **kw: Any
) -> ForkStoreSurface:
    monkeypatch.setattr("gecko.semantic_fork_surface.rehearse_purchase", rehearsal)
    monkeypatch.setattr(
        "gecko.semantic_fork_surface.ephemeral_signer", lambda proof: object()
    )
    return ForkStoreSurface(
        proof=FakeProof(),  # type: ignore[arg-type]
        store_name="geckocoffee",
        store_address=STORE_ADDR,
        rpc_call=FakeStoreRpc(_store_bytes()),
        **kw,
    )


def test_authority_is_read_from_the_store_account() -> None:
    surface = ForkStoreSurface(
        proof=FakeProof(),  # type: ignore[arg-type]
        store_name="geckocoffee",
        store_address=STORE_ADDR,
        rpc_call=FakeStoreRpc(_store_bytes()),
    )
    assert surface.authority() == AUTHORITY


def test_read_item_maps_id_to_on_chain_price_and_mint() -> None:
    surface = ForkStoreSurface(
        proof=FakeProof(),  # type: ignore[arg-type]
        store_name="geckocoffee",
        store_address=STORE_ADDR,
        rpc_call=FakeStoreRpc(_store_bytes()),
    )
    live = surface.read_item("cappuccino")
    assert live.price_lamports == get_item("cappuccino").price_lamports
    assert live.mint == USDC and live.in_stock


def test_out_of_stock_override_is_reported() -> None:
    surface = ForkStoreSurface(
        proof=FakeProof(),  # type: ignore[arg-type]
        store_name="geckocoffee",
        store_address=STORE_ADDR,
        rpc_call=FakeStoreRpc(_store_bytes()),
        out_of_stock=frozenset({"still-water"}),
    )
    assert not surface.read_item("still-water").in_stock


def test_item_absent_from_store_fails_closed() -> None:
    surface = ForkStoreSurface(
        proof=FakeProof(),  # type: ignore[arg-type]
        store_name="geckocoffee",
        store_address=STORE_ADDR,
        rpc_call=FakeStoreRpc(_store_bytes()),
    )
    with pytest.raises(ForkSurfaceError):
        surface.read_item("espresso-tonic")  # not seeded on this store


def test_balanced_rehearsal_becomes_a_landed_purchase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = _surface(monkeypatch, RehearsalRecorder(landed=True, discrepancies=()))
    result = surface.spend(ProposedPurchase("cappuccino", 4_000_000, AUTHORITY))
    assert result.landed and result.purchase is not None
    assert result.purchase.lamports_paid == 4_005_000  # whole outflow, fee included
    assert result.purchase.destination == AUTHORITY


def test_discrepant_rehearsal_is_not_a_purchase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = _surface(
        monkeypatch, RehearsalRecorder(landed=True, discrepancies=("price mismatch",))
    )
    result = surface.spend(ProposedPurchase("cappuccino", 4_000_000, AUTHORITY))
    assert not result.landed and result.purchase is None


def test_unlanded_rehearsal_is_not_a_purchase(monkeypatch: pytest.MonkeyPatch) -> None:
    surface = _surface(monkeypatch, RehearsalRecorder(landed=False))
    result = surface.spend(ProposedPurchase("cappuccino", 4_000_000, AUTHORITY))
    assert not result.landed


def test_untracked_outflow_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    surface = _surface(monkeypatch, RehearsalRecorder(landed=True, moved=None))
    result = surface.spend(ProposedPurchase("cappuccino", 4_000_000, AUTHORITY))
    assert not result.landed


def test_missing_store_account_fails_closed() -> None:
    def empty_rpc(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        return {"value": None}

    surface = ForkStoreSurface(
        proof=FakeProof(),  # type: ignore[arg-type]
        store_name="geckocoffee",
        store_address=STORE_ADDR,
        rpc_call=empty_rpc,
    )
    with pytest.raises(ForkSurfaceError):
        surface.authority()


def test_replace_smoke() -> None:
    # replace() on the frozen-ish dataclass keeps the seams — a guard against a
    # future field reorder silently dropping rpc_call.
    surface = ForkStoreSurface(
        proof=FakeProof(),  # type: ignore[arg-type]
        store_name="geckocoffee",
        store_address=STORE_ADDR,
        rpc_call=FakeStoreRpc(_store_bytes()),
    )
    twin = replace(surface, out_of_stock=frozenset({"cappuccino"}))
    assert not twin.read_item("cappuccino").in_stock

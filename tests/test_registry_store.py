"""Registry store: surface documents + revs + tiers (control plane only)."""

import pytest

from gecko.registry.store import RegistryError, RegistrySurface, SurfaceStore

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "T", "version": "1"},
    "paths": {
        "/x": {
            "get": {"operationId": "getX", "responses": {"200": {"description": "ok"}}}
        }
    },
}


def _store() -> SurfaceStore:
    return SurfaceStore(
        [
            RegistrySurface(name="colosseum", spec=SPEC, tier="free"),
            RegistrySurface(name="txline", spec=SPEC, tier="premium"),
        ]
    )


def test_names_and_get():
    store = _store()
    assert store.names() == ["colosseum", "txline"]
    assert store.get("colosseum").tier == "free"
    assert store.get("nope") is None


def test_manifest_carries_rev_tier_and_spec():
    store = _store()
    m = store.manifest("colosseum")
    assert m["name"] == "colosseum"
    assert m["tier"] == "free"
    assert m["spec"] == SPEC
    assert isinstance(m["surface_rev"], str) and len(m["surface_rev"]) >= 8


def test_manifest_unknown_surface_raises():
    with pytest.raises(RegistryError):
        _store().manifest("nope")


def test_tier_validated():
    with pytest.raises(RegistryError):
        RegistrySurface(name="x", spec=SPEC, tier="gold")


def test_duplicate_names_rejected():
    with pytest.raises(RegistryError, match="duplicate"):
        SurfaceStore(
            [
                RegistrySurface(name="x", spec=SPEC, tier="free"),
                RegistrySurface(name="x", spec=SPEC, tier="premium"),
            ]
        )


def test_surface_is_hashable():
    assert isinstance(hash(RegistrySurface(name="x", spec=SPEC, tier="free")), int)

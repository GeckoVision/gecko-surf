"""Task 3.1 — ``SimWorld`` + ``SimStore``: the per-session, ephemeral sim state.

The state gate's store is process-local and in-memory ONLY: it holds fabricated
``Decimal`` balances under opaque keys, never a raw account string, a real payload,
or a secret, and is never written to disk (invariant #1). These tests pin the three
load-bearing behaviors: per-session isolation, TTL eviction, and the LRU cap. The
clock is injected (``now: float``) so eviction is deterministic — no argless clock.
"""

from __future__ import annotations

from decimal import Decimal

from gecko.sandbox import SimStore, SimWorld


def test_per_session_isolation() -> None:
    # Two session ids must never see each other's balances.
    store = SimStore()
    a = store.get_or_create("sess-a", now=1000.0)
    b = store.get_or_create("sess-b", now=1000.0)
    assert isinstance(a, SimWorld) and isinstance(b, SimWorld)
    a.balances["self"] = Decimal("100")
    assert "self" not in b.balances
    # the same session id round-trips to the SAME world (state persists in-process)
    again = store.get_or_create("sess-a", now=1001.0)
    assert again is a
    assert again.balances["self"] == Decimal("100")


def test_ttl_eviction_drops_a_stale_world() -> None:
    store = SimStore(ttl=60.0)
    world = store.get_or_create("sess", now=1000.0)
    world.balances["self"] = Decimal("5")
    # accessed past the TTL -> evicted; a fresh, empty world is created.
    fresh = store.get_or_create("sess", now=1000.0 + 61.0)
    assert fresh is not world
    assert fresh.balances == {}


def test_ttl_keeps_a_recently_touched_world() -> None:
    store = SimStore(ttl=60.0)
    world = store.get_or_create("sess", now=1000.0)
    world.balances["self"] = Decimal("7")
    # within the TTL -> same world, state intact.
    same = store.get_or_create("sess", now=1000.0 + 59.0)
    assert same is world
    assert same.balances["self"] == Decimal("7")


def test_lru_cap_evicts_the_oldest_session() -> None:
    store = SimStore(max_sessions=2)
    store.get_or_create("a", now=1.0).balances["self"] = Decimal("1")
    store.get_or_create("b", now=2.0)
    store.get_or_create("c", now=3.0)  # over the cap -> evict the oldest ("a")
    assert store.get_or_create("a", now=4.0).balances == {}


def test_lru_cap_uses_recency_not_insertion_order() -> None:
    store = SimStore(max_sessions=2)
    store.get_or_create("a", now=1.0).balances["self"] = Decimal("1")
    store.get_or_create("b", now=2.0)
    store.get_or_create("a", now=3.0)  # touch "a" -> now the most-recently used
    store.get_or_create("c", now=4.0)  # over the cap -> evict "b" (now the oldest)
    survived = store.get_or_create("a", now=5.0)
    assert survived.balances["self"] == Decimal("1")


def test_gc_evicts_all_stale_worlds_at_once() -> None:
    store = SimStore(ttl=10.0)
    store.get_or_create("a", now=1.0)
    store.get_or_create("b", now=2.0)
    store.gc(now=100.0)  # both are well past the TTL
    # both come back fresh (proves they were evicted, not merely stale)
    assert store.get_or_create("a", now=100.0).balances == {}
    assert store.get_or_create("b", now=100.0).balances == {}

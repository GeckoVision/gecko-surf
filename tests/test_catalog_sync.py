"""catalog_sync: the let_me_buy surface projects into the three collections.

Load-bearing properties (architecture §1): the surface is source-agnostic and
carries TYPES not values; spec_rev is deterministic over the tool CONTRACT (not
the world), so it is reproducible and network-independent; the agent_specs doc
is immutable (re-sync is a no-op); make_purchase carries the store_name→PDA join.
"""

from __future__ import annotations

from gecko.store import InMemoryCollection
from gecko.store.catalog_sync import (
    let_me_buy_surface,
    surface_spec_rev,
    sync_surface,
)


def _cols() -> tuple[InMemoryCollection, InMemoryCollection, InMemoryCollection]:
    return InMemoryCollection(), InMemoryCollection(), InMemoryCollection()


def test_surface_has_the_four_proven_instructions() -> None:
    surface = let_me_buy_surface()
    ops = {e.operation_id for e in surface.endpoints}
    assert ops == {"initialize", "add_product", "make_purchase", "mark_as_delivered"}
    assert surface.surface_id == "orquestra:let_me_buy"


def test_arg_shapes_are_types_never_values() -> None:
    surface = let_me_buy_surface()
    for endpoint in surface.endpoints:
        for name, kind in endpoint.arg_shape.items():
            assert isinstance(name, str) and isinstance(kind, str)
            assert kind in {"string", "pubkey", "u64", "u8"}


def test_make_purchase_carries_the_pda_grounding_join() -> None:
    mp = next(
        e for e in let_me_buy_surface().endpoints if e.operation_id == "make_purchase"
    )
    assert "store_name" in mp.groundable_route_args  # grounds the receipts PDA
    assert "recipient_token_account" in mp.account_inputs  # the store's credited ATA


def test_sync_projects_all_three_collections() -> None:
    programs, endpoints, specs = _cols()
    surface = let_me_buy_surface()
    spec_rev = sync_surface(
        surface, programs=programs, endpoints=endpoints, agent_specs=specs
    )

    assert programs.count_documents({"surface_id": "orquestra:let_me_buy"}) == 1
    prog = next(programs.find({"surface_id": "orquestra:let_me_buy"}))
    assert prog["latest_spec_rev"] == spec_rev and prog["endpoint_count"] == 4

    assert endpoints.count_documents({"surface_id": "orquestra:let_me_buy"}) == 4
    ep = next(endpoints.find({"_id": "orquestra:let_me_buy:make_purchase"}))
    assert ep["spec_rev"] == spec_rev
    assert ep["arg_shape"] == {
        "store_name": "string",
        "product_name": "string",
        "table_number": "u8",
    }

    assert specs.count_documents({"_id": f"orquestra:let_me_buy:{spec_rev}"}) == 1
    spec = next(specs.find({"spec_rev": spec_rev}))
    assert sorted(spec["tool_def_names"]) == [
        "add_product",
        "initialize",
        "make_purchase",
        "mark_as_delivered",
    ]


def test_spec_rev_is_deterministic_and_network_independent() -> None:
    mainnet = let_me_buy_surface(network="mainnet")
    devnet = let_me_buy_surface(network="devnet")
    # Same tool contract, different world → same spec_rev (pins the CONTRACT).
    assert surface_spec_rev(mainnet) == surface_spec_rev(devnet)
    assert surface_spec_rev(mainnet).startswith("sr_")


def test_agent_spec_doc_is_immutable_on_resync() -> None:
    programs, endpoints, specs = _cols()
    surface = let_me_buy_surface()
    rev1 = sync_surface(
        surface, programs=programs, endpoints=endpoints, agent_specs=specs
    )
    rev2 = sync_surface(
        surface, programs=programs, endpoints=endpoints, agent_specs=specs
    )
    assert rev1 == rev2
    # Re-sync is a no-op on the immutable spec doc (same _id, written once).
    assert specs.count_documents({"_id": f"orquestra:let_me_buy:{rev1}"}) == 1


def test_score_reader_binds_to_a_synced_endpoint() -> None:
    # The catalog and the store layer agree on the (surface_id, operation_id,
    # spec_rev) key: a synced endpoint is exactly what endpoint_score reads for.
    from gecko.store import endpoint_score

    programs, endpoints, specs = _cols()
    spec_rev = sync_surface(
        let_me_buy_surface(), programs=programs, endpoints=endpoints, agent_specs=specs
    )
    outcomes = InMemoryCollection()  # no runs yet
    score = endpoint_score(
        outcomes,
        surface_id="orquestra:let_me_buy",
        operation_id="make_purchase",
        spec_rev=spec_rev,
    )
    assert score.n == 0 and score.first_call_correct is None  # not evaluated yet

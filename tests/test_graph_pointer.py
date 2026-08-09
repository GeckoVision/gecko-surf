"""R1 — the graph carries WHERE a response value lives and HOW MANY there are.

Today a derived document binds `$response.body#/id` while the value sits at `/0/id`
(Pegana, top-level array) or `/data/items/0/address` (Birdeye, `data` wrapper). Three
layers each drop the container path, and the arity — "there are many of these" — has no
representation at all, so the not-evaluated case falls into the type's zero value.

Two decisions here, both from review and both load-bearing:

**The pointer lives on the field `Node`, not on `ExplainEntry`.** `ExplainEntry` is a
per-plan projection with two construction sites, and one of them (`compose.py`) builds
from `(op, field_name)` with no schema access — so cross-surface entries would carry an
empty pointer forever. `arazzo._locations(graphs)` already sets the precedent by reading
a parameter's `in` off the graph nodes.

**The stored pointer is SCHEMA-SPACE: container path, no index.** A numeric segment other
than a canonical representative proves the pointer came from an observation rather than a
schema, which would put a data-plane artifact in a content-addressed structure. Position
is introduced at render time by whatever emits an expression — or refused.
"""

from __future__ import annotations

from pathlib import Path

from gecko.client import AgentApiClient
from gecko.surface import Surface

REPO = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures"
PEGANA = FIX / "pegana_p0_openapi.json"
TXLINE = FIX / "txline_openapi.yaml"
BIRDEYE = REPO / "examples/birdeye_demo/spec/birdeye_openapi.json"


def _fields(spec: Path, surface_id: str, operation_id: str, name: str):
    graph = Surface.of(AgentApiClient(str(spec), surface_id=surface_id)).graph
    return [
        n
        for n in graph.nodes
        if n.kind == "field" and n.owner == operation_id and n.name == name
    ]


def test_top_level_array_is_recorded_as_many() -> None:
    """`getApiFixturesSnapshot` returns `type: array` (txline_openapi.yaml), so its join
    key is reached THROUGH a collection. That fact must be on the node — it is the
    difference between "the id" and "the id of whichever element sorted first"."""
    nodes = _fields(TXLINE, "txline", "getApiFixturesSnapshot", "FixtureId")
    assert nodes, "the txline snapshot must expose FixtureId"
    assert nodes[0].arity == "many"


def test_object_at_the_body_root_is_recorded_as_one() -> None:
    """The ordinary case must not be swept into `many` — a report that says everything is
    plural is as useless as one that says nothing is."""
    graph = Surface.of(AgentApiClient(str(PEGANA), surface_id="pegana")).graph
    singular = [n for n in graph.nodes if n.kind == "field" and n.arity == "one"]
    assert singular, "pegana must have body-root scalars"


def test_pointer_is_schema_space_with_no_index() -> None:
    """A stored pointer names the CONTAINER PATH. A numeric segment would encode "the
    third element", which schema space has no notion of, and would put an observation
    into the content-addressed graph."""
    graph = Surface.of(AgentApiClient(str(BIRDEYE), surface_id="birdeye")).graph
    for node in graph.nodes:
        if node.kind != "field" or not node.source_pointer:
            continue
        segments = [s for s in node.source_pointer.split("/") if s]
        assert not any(s.isdigit() for s in segments), (
            f"{node.source_pointer!r} carries an index — stored pointers are schema-space"
        )
        assert node.source_pointer.startswith("/"), "RFC 6901 pointers start with /"


def test_nested_wrapper_survives_to_the_pointer() -> None:
    """Birdeye's join key sits under a `data` wrapper. Ingest flattens the name; the
    pointer is what remembers the container, and without it the emitted expression
    resolves to nothing."""
    graph = Surface.of(AgentApiClient(str(BIRDEYE), surface_id="birdeye")).graph
    nested = [
        n for n in graph.nodes if n.kind == "field" and n.source_pointer.count("/") > 1
    ]
    assert nested, "birdeye has fields below the body root; the pointer must show it"


def test_arity_defaults_to_unknown_never_to_one() -> None:
    """The zero value must be the honest one. `unknown` is a real member: a field whose
    container we could not establish is not a scalar, it is unestablished — and the whole
    class of bug this closes is the not-evaluated case falling into the type's default."""
    from gecko.graph import Node

    bare = Node(kind="field", id="x", name="y")
    assert bare.arity == "unknown"
    assert bare.source_pointer == ""

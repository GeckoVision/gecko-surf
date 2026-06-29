"""Ingestor tests, anchored to the real TxODDS spec (tests/fixtures/txodds_docs.yaml).

Testing against the actual upstream surface — not a toy spec — is the point:
the comprehension layer must survive a real, human-shaped API.
"""

from pathlib import Path

from gecko.ingest import Operation, extract_operations, load_spec, resolve_refs

FIXTURE = Path(__file__).parent / "fixtures" / "txodds_docs.yaml"


def _ops() -> list[Operation]:
    return extract_operations(load_spec(str(FIXTURE)))


def test_extracts_all_operations():
    # TxODDS docs.yaml currently exposes exactly 18 operations.
    assert len(_ops()) == 18


def test_path_param_is_resolved_and_required():
    odds = next(
        o
        for o in _ops()
        if o.path == "/api/odds/snapshot/{fixtureId}" and o.method == "GET"
    )
    fixture_id = next(p for p in odds.parameters if p.name == "fixtureId")
    assert fixture_id.location == "path"
    assert fixture_id.required is True
    # schema must be dereferenced to a concrete type, not a bare {"$ref": ...}
    assert "$ref" not in fixture_id.schema


def test_core_tags_present():
    tags = {t for o in _ops() for t in o.tags}
    assert {"Authentication", "Fixtures", "Odds", "Scores"} <= tags


def test_activate_endpoint_has_request_body():
    activate = next(
        o for o in _ops() if o.path == "/api/token/activate" and o.method == "POST"
    )
    assert activate.request_body is not None


def test_resolve_refs_breaks_cycles():
    spec = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {"next": {"$ref": "#/components/schemas/Node"}},
                }
            }
        },
    }
    resolved = resolve_refs({"$ref": "#/components/schemas/Node"}, spec)
    # should terminate (not infinite-recurse) and keep a usable shape
    assert resolved["type"] == "object"

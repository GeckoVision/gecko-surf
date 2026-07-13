from pathlib import Path

from gecko.ingest import extract_operations, load_spec
from gecko.sample import example_from_schema

FIXTURE = Path(__file__).parent / "fixtures" / "txodds_docs.yaml"


def test_object_and_array_shapes():
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "name": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }
    out = example_from_schema(schema)
    assert out == {"id": 0, "name": "sample", "tags": ["sample"]}


def test_required_field_present_even_if_not_in_properties():
    """A field in `required` but missing from `properties` must still be emitted —
    else a well-formed call is (wrongly) rejected for a missing required field."""
    schema = {
        "type": "object",
        "required": ["a", "b"],
        "properties": {"a": {"type": "string"}},
    }
    out = example_from_schema(schema)
    assert "a" in out and "b" in out  # b is required but absent from properties


def test_nested_required_field_present():
    """The real Stripe edge: a required field nested in the body object."""
    schema = {
        "type": "object",
        "properties": {
            "body": {
                "type": "object",
                "required": ["rejection_reasons"],
                "properties": {},
            }
        },
    }
    out = example_from_schema(schema)
    assert "rejection_reasons" in out["body"]


def test_prefers_explicit_example_then_enum():
    assert example_from_schema({"type": "string", "example": "X"}) == "X"
    assert example_from_schema({"enum": ["a", "b"]}) == "a"


def test_generates_a_response_sample_for_a_real_endpoint():
    op = next(
        o
        for o in extract_operations(load_spec(str(FIXTURE)))
        if o.path == "/api/odds/snapshot/{fixtureId}" and o.method == "GET"
    )
    schema = op.responses["200"]["content"]["application/json"]["schema"]
    sample = example_from_schema(schema)
    assert sample is not None  # a usable recorded response was synthesized

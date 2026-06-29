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

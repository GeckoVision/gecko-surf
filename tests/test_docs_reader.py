"""Offline ($0) tests for the productized docs_reader engine (gecko.docs_reader).

No browser, no network: a committed static HTML doc fixture is parsed by the stdlib
node extractor, the pure parser recovers the callable surface, and the emitted draft
comprehends through the UNMODIFIED engine — the exact "painful API" on-ramp.
"""

from __future__ import annotations

from pathlib import Path

from gecko import AgentApiClient, public_session
from gecko import docs_reader
from gecko.docs_reader import from_docs, page_from_html

_DOC = Path(__file__).resolve().parent / "fixtures" / "sample_docs.html"
_EXPECTED = {"sendBundle", "getTipAccounts", "getTipFloor"}


def _html() -> str:
    return _DOC.read_text(encoding="utf-8")


def test_page_from_html_preserves_document_order() -> None:
    page = page_from_html("file://sample", _html())
    kinds = [n.kind for n in page.nodes]
    # The param table must sit immediately before the sendBundle curl (the association
    # the parser depends on), and headings/code/table are all recovered in order.
    assert "table" in kinds and "code" in kinds and kinds[0] == "heading"
    table_idx = kinds.index("table")
    assert kinds[table_idx + 1] == "code"


def test_from_docs_recovers_expected_ops() -> None:
    result = from_docs(str(_DOC))
    assert {op.operation_id for op in result.ops} == _EXPECTED


def test_draft_comprehends_through_engine() -> None:
    draft = from_docs(str(_DOC)).draft
    client = AgentApiClient(draft, session=public_session())
    assert {t["name"] for t in client.list_tools()} == _EXPECTED


def test_optional_auth_recovered_and_not_gating() -> None:
    result = from_docs(str(_DOC))
    assert result.uuid_auth == {"in": "header", "name": "x-tip-auth"}
    # Optional auth must NOT hide any tool from a no-auth session (invariant #4).
    client = AgentApiClient(result.draft, session=public_session())
    assert len(client.list_tools()) == len(_EXPECTED)


def test_encoding_param_enriched_from_table() -> None:
    ops = {op.operation_id: op for op in from_docs(str(_DOC)).ops}
    encoding = next(p for p in ops["sendBundle"].params if p.name == "encoding")
    assert encoding.required is False
    assert encoding.enum == ["base58", "base64"]
    assert encoding.default == "base58"


def test_positional_arg_flagged_for_review() -> None:
    ops = {op.operation_id: op for op in from_docs(str(_DOC)).ops}
    first = ops["sendBundle"].params[0]
    assert first.name == "params" and first.confidence == "low" and first.review


def test_review_flags_are_counted() -> None:
    result = from_docs(str(_DOC))
    # At least the positional-arg x-review note and the REST op's medium confidence.
    assert result.review_notes >= 1
    assert result.low_confidence >= 1


def test_recorded_call_returns_the_docs_response_example() -> None:
    draft = from_docs(str(_DOC)).draft
    client = AgentApiClient(draft, session=public_session())
    data = client.call("getTipAccounts", {}, mode="recorded")["data"]
    assert data["result"] == ["TipAcct1111", "TipAcct2222"]


def test_url_branch_fetches_offline_via_monkeypatched_transport(monkeypatch) -> None:
    # The URL path shares the same code path; only the transport edge differs. Patch
    # safe_get so we exercise it with zero network.
    monkeypatch.setattr(
        docs_reader.core, "safe_get", lambda url, resolver=None: _html()
    )
    result = from_docs("https://docs.example.com/api")
    assert {op.operation_id for op in result.ops} == _EXPECTED

"""The shared comprehension core behind the two 'submit your API' front doors.

Offline + TDD: comprehend a committed local fixture spec (no network), assert the
control-plane-safe summary + artifacts, prove SSRF rejection, the from-docs fallback,
and that no payload/secret ever rides back in the result.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from gecko.comprehend_service import (
    ComprehendError,
    ComprehendResult,
    comprehend_submission,
)

PEGANA = str(Path(__file__).resolve().parent / "fixtures" / "pegana_openapi.json")

# A tiny, self-contained OpenAPI so the happy path never depends on a big fixture.
TINY_SPEC = str(Path(__file__).resolve().parent / "fixtures" / "tiny_openapi.json")

# A tiny OpenAPI where every op is bearer-gated (global security) — the fully-gated case.
GATED_SPEC = str(
    Path(__file__).resolve().parent / "fixtures" / "tiny_gated_openapi.json"
)


def test_fully_gated_api_reports_all_comprehended_tools_not_zero() -> None:
    # Regression: a bearer-gated API previewed with a no-auth session used to report
    # usable_tool_count=0 and an empty tools list (the SERVED/auth-filtered view). The
    # comprehension summary must report the FULL comprehended surface — Gecko injects the
    # credential at serve time, so a gated tool is usable, just gated. A fully-gated API
    # (e.g. dpo2u: 76 bearer-gated ops -> 0) is the case that surfaced this.
    result = comprehend_submission(GATED_SPEC)
    assert result.op_count == 1
    assert (
        result.usable_tool_count == 1
    )  # NOT 0 — comprehended, gated, injected at call time
    assert {t["name"] for t in result.tools} == {"getSecret"}
    # ...and it says so honestly rather than silently claiming the tool is free-to-call.
    assert any("authentication" in w.lower() for w in result.warnings)


def test_comprehends_local_openapi_into_summary_and_artifacts() -> None:
    result = comprehend_submission(PEGANA)

    assert isinstance(result, ComprehendResult)
    assert result.name == "Pegana API"
    assert result.op_count > 0
    assert result.usable_tool_count > 0
    # tools is a list of {name, summary} — question-shaped, no auth, no payload.
    assert result.tools and all({"name", "summary"} == set(t) for t in result.tools)
    # all five agent-native artifacts are generated (control-plane only).
    assert set(result.artifacts) == {
        "llms.txt",
        "gecko.json",
        ".well-known/gecko.json",
        "tools.md",
        "SKILL.md",
    }
    # a clean OpenAPI is not quarantined.
    assert result.quarantined is False
    # next_steps points at the $0 self-host path + an MCP add snippet.
    assert "uvx" in result.next_steps["self_host"]
    assert "gecko-surf[serve]" in result.next_steps["self_host"]
    assert "mcp" in result.next_steps["claude_mcp_add"].lower()
    assert "mcpServers" in result.next_steps["mcp_json"]


def test_tiny_spec_round_trips() -> None:
    result = comprehend_submission(TINY_SPEC)
    assert result.name == "Tiny API"
    assert result.op_count == 1
    assert result.usable_tool_count == 1
    names = {t["name"] for t in result.tools}
    assert "ping" in names


def test_ssrf_file_scheme_is_rejected() -> None:
    with pytest.raises(ComprehendError):
        comprehend_submission("file:///etc/passwd")


def test_ssrf_private_ip_is_rejected() -> None:
    # Cloud-metadata IP literal — validated without any DNS lookup.
    with pytest.raises(ComprehendError):
        comprehend_submission("http://169.254.169.254/latest/meta-data")


def test_ssrf_loopback_is_rejected() -> None:
    with pytest.raises(ComprehendError):
        comprehend_submission("http://127.0.0.1:8000/openapi.json")


def test_from_docs_fallback_is_quarantined_and_warned() -> None:
    # A human docs page (HTML), not an OpenAPI — recovered via from_docs, born
    # quarantined, and the caller is warned.
    docs = str(Path(__file__).resolve().parent / "fixtures" / "sample_docs.html")
    result = comprehend_submission(docs, from_docs=True)
    assert result.quarantined is True
    assert result.warnings  # at least the quarantine warning
    assert any(
        "quarantin" in w.lower() or "review" in w.lower() for w in result.warnings
    )


def test_control_plane_no_payload_or_secret_fields() -> None:
    # The result is surface metadata only — its schema, by construction, has no field
    # that could carry a response payload, the raw spec, or a secret.
    result = comprehend_submission(PEGANA)
    assert set(asdict(result)) == {
        "name",
        "description",
        "op_count",
        "usable_tool_count",
        "tools",
        "artifacts",
        "quarantined",
        "warnings",
        "next_steps",
    }
    # tools carry ONLY {name, summary} — no auth, no schema defaults, no response data.
    assert all(set(t) == {"name", "summary"} for t in result.tools)
    # the raw spec dict is not smuggled back on the frozen result.
    assert not hasattr(result, "spec")
    # no synthesized response payload rides along: the recorded-mode 'data'/'mode_note'
    # envelope keys never appear as result fields.
    assert not hasattr(result, "data")


def test_url_credentials_are_redacted_in_errors() -> None:
    # A userinfo-bearing URL that fails SSRF must not echo the credential.
    with pytest.raises(ComprehendError) as exc:
        comprehend_submission("http://user:SECRETPASS@127.0.0.1/openapi.json")
    assert "SECRETPASS" not in str(exc.value)


def test_from_docs_failure_is_graceful_not_500(monkeypatch) -> None:
    """A failing docs fetch (e.g. an unfollowed redirect) must surface as a clean
    ComprehendError, never an unhandled exception that becomes a 500."""
    import urllib.error

    from gecko import comprehend_service as cs

    def boom(src: str) -> object:
        raise urllib.error.HTTPError(src, 307, "Temporary Redirect", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(cs, "recover_from_docs", boom)
    with pytest.raises(ComprehendError):
        comprehend_submission(TINY_SPEC, from_docs=True)

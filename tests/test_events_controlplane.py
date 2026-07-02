"""Control-plane gate for usage-event instrumentation (``gecko.events``).

This is the load-bearing test, mirroring ``test_corpus_controlplane.py`` /
``test_telemetry.py``: a control-plane violation is a build break, not a review
comment. It proves — by construction — that ``emit_surf_event`` CANNOT emit a
response payload, an argument VALUE, a URL-with-creds, or a secret:

1. The field allowlist IS the schema; a disallowed key fails closed and an injected
   fake sink receives ONLY allowlisted keys.
2. Value-bearing strings are gated: ``event``/``error_class`` to closed sets,
   ``tool_name``/``mode``/``tier`` to short non-secret labels, ``surface_id`` reduced
   to a cred-free host.
3. Ships silent: no-op when ``MONGODB_URI`` is unset; ``GECKO_TELEMETRY=off``
   hard-disables even an injected sink; a sink failure never breaks the call.

No real MongoDB — a fake sink is injected via ``set_surf_sink_override``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gecko import corpus
from gecko.events import (
    ALLOWED_FIELDS,
    RECORD_ALLOWED_KEYS,
    SURF_EVENTS,
    SurfEventRecord,
    assert_fields_allowlisted,
    build_surf_record,
    emit_surf_event,
    set_surf_sink_override,
    to_doc,
)
from gecko.telemetry import TelemetryError

# Sensitive VALUES that must NEVER reach a surf event (mirror corpus/telemetry).
SENSITIVE_MINT = "SoLSeCrEtMintAddr1111111111111111111111111"
SENSITIVE_BODY = "topsecret-user-note-DO-NOT-PERSIST"
CRED_URL = "https://alice:SuperSecretPassw0rd@api.pegana.example/v1/x?token=SHHH"


class _FakeSink:
    """The injected opt-in phone-home seam, recorded for assertions."""

    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    def __call__(self, doc: Any) -> None:
        self.docs.append(dict(doc))


@pytest.fixture(autouse=True)
def _reset_sink():
    """Clear any injected sink + cached client before AND after each test so state
    never leaks across tests or files."""
    set_surf_sink_override(None)
    yield
    set_surf_sink_override(None)


@pytest.fixture
def sink() -> _FakeSink:
    s = _FakeSink()
    set_surf_sink_override(s)
    return s


# --------------------------------------------------------------------------- #
# The allowlist IS the schema
# --------------------------------------------------------------------------- #
def test_allowed_fields_is_the_exact_closed_set():
    assert ALLOWED_FIELDS == {
        "tool_name",
        "mode",
        "ok",
        "latency_ms",
        "error_class",
        "tier",
        "k",
        "hit_rank",
    }


def test_record_schema_is_structural_plus_allowed_fields():
    # Adding a dataclass field without updating ALLOWED_FIELDS breaks this — the
    # allowlist can never silently drift from the persisted schema.
    structural = {"ts", "event", "surface_id"}
    assert RECORD_ALLOWED_KEYS == structural | ALLOWED_FIELDS
    assert RECORD_ALLOWED_KEYS == set(SurfEventRecord.__dataclass_fields__)


def test_record_schema_has_no_payload_or_value_field():
    forbidden = {
        "data",
        "body",
        "response",
        "result",
        "args",
        "url",
        "query",
        "headers",
    }
    assert forbidden.isdisjoint(RECORD_ALLOWED_KEYS)


def test_event_vocabulary_is_closed():
    assert SURF_EVENTS == {
        "surf.search",
        "surf.prepare",
        "surf.call",
        "surf.first_call_correct",
    }


# --------------------------------------------------------------------------- #
# Fail closed on anything that is not control-plane-safe metadata
# --------------------------------------------------------------------------- #
def test_disallowed_key_fails_closed_and_nothing_is_emitted(sink: _FakeSink):
    # The classic leak: a response body sneaking in under an unknown key.
    with pytest.raises(TelemetryError):
        emit_surf_event(
            "surf.call", surface_id="pegana", data={"secret": SENSITIVE_BODY}
        )
    assert sink.docs == []  # fail closed: the sink never saw it


def test_arg_value_key_fails_closed(sink: _FakeSink):
    # Someone tries to log the raw args / a param value under its own key.
    with pytest.raises(TelemetryError):
        emit_surf_event("surf.call", surface_id="pegana", mint=SENSITIVE_MINT)
    with pytest.raises(TelemetryError):
        emit_surf_event("surf.call", surface_id="pegana", args={"mint": SENSITIVE_MINT})
    assert sink.docs == []


def test_non_vocabulary_event_fails_closed(sink: _FakeSink):
    with pytest.raises(TelemetryError):
        emit_surf_event("surf.bogus", surface_id="pegana")  # type: ignore[arg-type]
    assert sink.docs == []


def test_error_class_must_be_a_closed_set_member(sink: _FakeSink):
    # A free-text error_class could otherwise smuggle a VALUE; gate to the closed set.
    with pytest.raises(TelemetryError):
        emit_surf_event(
            "surf.first_call_correct", surface_id="pegana", error_class=SENSITIVE_MINT
        )
    # A legitimate closed-set class is fine.
    emit_surf_event(
        "surf.first_call_correct", surface_id="pegana", error_class="not_found_404"
    )
    assert sink.docs[-1]["error_class"] == "not_found_404"
    assert all(c in corpus.ERROR_CLASSES for c in {"not_found_404"})


def test_label_field_rejects_a_secret_shaped_or_long_value(sink: _FakeSink):
    # tool_name/mode/tier are NAMES — a payload/secret masquerading as one is rejected.
    with pytest.raises(TelemetryError):
        emit_surf_event("surf.call", surface_id="pegana", tool_name="x" * 200)
    with pytest.raises(TelemetryError):
        emit_surf_event(
            "surf.call", surface_id="pegana", tier="sk-livedeadbeef0123456789abcdef"
        )
    assert sink.docs == []


# --------------------------------------------------------------------------- #
# The killer test: no value / payload / cred substring can reach the sink
# --------------------------------------------------------------------------- #
def test_fake_sink_receives_only_allowlisted_keys(sink: _FakeSink):
    emit_surf_event(
        "surf.first_call_correct",
        surface_id="pegana",
        tool_name="get_asset_state",
        mode="recorded",
        ok=True,
        latency_ms=42,
        error_class="none",
        tier="free",
        k=5,
        hit_rank=1,
    )
    assert len(sink.docs) == 1
    assert set(sink.docs[0]) <= RECORD_ALLOWED_KEYS  # nothing off-schema ever leaves


def test_url_with_creds_is_reduced_to_bare_host(sink: _FakeSink):
    # A surface_id that is accidentally a full URL-with-creds must lose scheme,
    # userinfo, path, and query — everything where a secret could hide.
    emit_surf_event("surf.call", surface_id=CRED_URL, tool_name="get_x", mode="live")
    raw = json.dumps(sink.docs[0])
    assert sink.docs[0]["surface_id"] == "api.pegana.example"
    assert "SuperSecretPassw0rd" not in raw
    assert "alice" not in raw
    assert "SHHH" not in raw
    assert "/v1/x" not in raw
    assert "token=" not in raw


def test_no_sensitive_substring_survives_across_vectors(sink: _FakeSink):
    # Everything the writer DID accept (across a batch of benign emits) is scanned:
    # no sensitive value the caller ever handed in may appear anywhere.
    emit_surf_event("surf.search", surface_id="pegana", k=3)
    emit_surf_event("surf.call", surface_id=CRED_URL, tool_name="get_x", mode="live")
    emit_surf_event(
        "surf.first_call_correct",
        surface_id="pegana",
        tool_name="get_x",
        ok=False,
        error_class="unauthorized_401",
    )
    raw = json.dumps(sink.docs)
    assert SENSITIVE_MINT not in raw
    assert SENSITIVE_BODY not in raw
    assert "SuperSecretPassw0rd" not in raw


def test_writer_allowlist_rejects_a_tampered_doc():
    # Belt-and-suspenders: even a hand-built record dict is re-gated before it leaves.
    from gecko.events import assert_record_allowlisted

    doc = to_doc(build_surf_record("surf.call", surface_id="pegana", tool_name="t"))
    doc["data"] = "a response body sneaking in"
    with pytest.raises(TelemetryError):
        assert_record_allowlisted(doc)


def test_assert_fields_allowlisted_is_the_boundary():
    assert_fields_allowlisted({"tool_name": "t", "ok": True})  # ok
    with pytest.raises(TelemetryError):
        assert_fields_allowlisted({"response": "body"})


# --------------------------------------------------------------------------- #
# Ships silent + best-effort posture
# --------------------------------------------------------------------------- #
def test_noop_when_mongodb_uri_unset(monkeypatch):
    # No override + no MONGODB_URI => a third-party OSS install NEVER phones home.
    from gecko import events

    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGO_URI", raising=False)
    set_surf_sink_override(None)  # clears cache too
    assert events._resolve_sink() is None
    assert events._mongo_collection() is None
    # And the public emit is a no-op (validates, then silently returns).
    emit_surf_event("surf.call", surface_id="pegana", tool_name="t")  # no raise


def test_gecko_telemetry_off_disables_even_an_injected_sink(monkeypatch, sink):
    monkeypatch.setenv("GECKO_TELEMETRY", "off")
    emit_surf_event("surf.call", surface_id="pegana", tool_name="t")
    assert sink.docs == []


def test_control_plane_violation_still_raises_when_disabled(monkeypatch, sink):
    # Even hard-disabled, a wiring mistake (disallowed key) is a build break, not a
    # silent pass — validation runs before the opt-out gate.
    monkeypatch.setenv("GECKO_TELEMETRY", "off")
    with pytest.raises(TelemetryError):
        emit_surf_event("surf.call", surface_id="pegana", data="body")


def test_emit_swallows_a_sink_failure(monkeypatch):
    def boom(_doc: Any) -> None:
        raise RuntimeError("mongo down")

    set_surf_sink_override(boom)
    # Best-effort: an operational sink failure must NEVER raise into the call path.
    emit_surf_event("surf.call", surface_id="pegana", tool_name="t")


def test_emit_does_not_swallow_a_downstream_control_plane_violation():
    def violating(_doc: Any) -> None:
        raise TelemetryError("non-allowlisted key downstream")

    set_surf_sink_override(violating)
    with pytest.raises(TelemetryError):
        emit_surf_event("surf.call", surface_id="pegana", tool_name="t")

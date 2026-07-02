"""Usage-event instrumentation — the honest adoption metric AND the first
control-plane-clean brick of the V2 feedback path.

This module emits ONLY *metadata about calls* — the event kind, an opaque surface
id, the tool NAME, the mode, an ok-bool, latency, an error CLASS, a plan tier, and
retrieval k/rank. It NEVER emits a response payload, an argument VALUE, a
URL-with-creds, a secret, or user data. That is invariant #1 (control plane, never
data plane), and it is what lets surfcall instrument any API unilaterally.

Three structural guarantees, mirroring ``corpus.py`` / ``telemetry.py``:

1. **Closed event vocabulary.** ``SurfEvent`` is the single source of truth for the
   event kinds; a non-member event fails closed (``TelemetryError``).
2. **Field allowlist — the writer, not the caller, decides what may leave.**
   ``emit_surf_event(**fields)`` accepts ONLY the keys in ``ALLOWED_FIELDS``; any
   other key fails closed. There is no field through which a payload/arg-value can
   enter, and every value-bearing string is either a closed-set member
   (``event``/``error_class``) or a short, non-secret-shaped label
   (``tool_name``/``mode``/``tier``). ``surface_id`` is reduced to a cred-free
   opaque token. A reviewer can see *by construction* that no payload can reach the
   record.
3. **Ships silent.** No-op when ``MONGODB_URI`` is unset — a third-party OSS install
   with no URI NEVER phones home; only our hosted surface (which points
   ``MONGODB_URI`` at ``gecko_events``) emits. ``GECKO_TELEMETRY=off`` hard-disables.
   ``pymongo`` is an OPTIONAL extra, imported lazily; if absent, no-op. The sink is
   fire-and-forget and never raises into the call path — except a control-plane
   violation, which MUST surface.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Literal, get_args

from collections.abc import Mapping

from . import corpus
from .sanitize import looks_like_secret_value
from .surfaces import _host_of

# Reuse the opt-out contract + the sink alias + the typed control-plane error from
# telemetry — single source of truth, no redeclared shared types (telemetry never
# imports events, so there is no cycle).
from .telemetry import Sink, TelemetryError, telemetry_enabled

logger = logging.getLogger("gecko.events")

# --------------------------------------------------------------------------- #
# The closed event vocabulary — single source of truth (canonical types).
# --------------------------------------------------------------------------- #
#: Every usage event surfcall emits. Consumers import ``SurfEvent`` from here;
#: never redeclare it. Append-only to the closed set.
SurfEvent = Literal[
    "surf.search",  # an agent asked search_capabilities for the right endpoint
    "surf.prepare",  # a correct request was prepared for a tool
    "surf.call",  # a tool was invoked through the surface
    "surf.first_call_correct",  # a call outcome resolved (ok + error_class)
]

#: Runtime membership form of ``SurfEvent`` (a Literal is not iterable at runtime).
SURF_EVENTS: frozenset[str] = frozenset(get_args(SurfEvent))

# --------------------------------------------------------------------------- #
# The field allowlist — the writer's closed set of caller-supplied fields.
# --------------------------------------------------------------------------- #
#: The ONLY keyword fields ``emit_surf_event`` will persist. Metadata about the
#: call — never a value. Any other key fails closed. Kept in sync with the record
#: schema by ``test_events_controlplane`` (record fields == structural ∪ this set).
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "tool_name",  # the tool/operation NAME (spec-derived), never an arg value
        "mode",  # "recorded" | "live"
        "ok",  # bool — did the call succeed
        "latency_ms",  # int — wall time, never a payload
        "error_class",  # a CLOSED corpus.ERROR_CLASSES member, never free text
        "tier",  # plan/access tier label (short, non-secret)
        "k",  # retrieval breadth (results requested/returned)
        "hit_rank",  # rank of a chosen hit — the V2 feedback signal
    }
)

#: A value-bearing string field may be at most this long — a payload/secret is
#: longer and/or secret-shaped, so it cannot masquerade as a label.
_MAX_LABEL = 128
#: The closed set of modes; guarded by shape (not membership) so an unexpected but
#: benign short mode never breaks a call, while a payload/secret is still rejected.
_LABEL_FIELDS = ("tool_name", "mode", "tier")

EVENTS_DB = "gecko_events"
EVENTS_COLLECTION = "surf_events"


# --------------------------------------------------------------------------- #
# The record — the frozen dataclass IS the schema.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SurfEventRecord:
    """Exactly what may be persisted: a timestamp, the closed event kind, an opaque
    surface id, and the allowlisted metadata fields. Frozen so it cannot accrete a
    field at runtime — the field set IS ``RECORD_ALLOWED_KEYS``. There is no field
    that could hold a response body, an arg value, or a token."""

    ts: int
    event: str  # a SurfEvent member (validated at build time)
    surface_id: str | None = None  # opaque/host id, never a URL with creds
    tool_name: str | None = None
    mode: str | None = None
    ok: bool | None = None
    latency_ms: int | None = None
    error_class: str | None = None
    tier: str | None = None
    k: int | None = None
    hit_rank: int | None = None


RECORD_ALLOWED_KEYS: frozenset[str] = frozenset(SurfEventRecord.__dataclass_fields__)
#: Structural keys the writer supplies (not caller-controlled fields).
_STRUCTURAL_KEYS: frozenset[str] = frozenset({"ts", "event", "surface_id"})


# --------------------------------------------------------------------------- #
# Fail-closed validation — the boundary a reviewer inspects.
# --------------------------------------------------------------------------- #
def _is_safe_label(value: str) -> bool:
    """A caller-supplied string may leave only if it is a short, non-secret-shaped
    label. A response body (long / JSON) or a secret fails this — so it can never
    ride out through ``tool_name``/``mode``/``tier``."""
    return len(value) <= _MAX_LABEL and not looks_like_secret_value(value)


def assert_fields_allowlisted(fields: Mapping[str, Any]) -> None:
    """Reject (fail closed) anything that is not control-plane-safe metadata:

    * any key not in ``ALLOWED_FIELDS`` (no ``data``/``args``/``body``/``response``);
    * an ``error_class`` outside the CLOSED ``corpus.ERROR_CLASSES`` set (a free-text
      class could otherwise smuggle a value);
    * any label field carrying a long/secret-shaped string (would be a value, not a
      name).
    """
    extra = set(fields) - ALLOWED_FIELDS
    if extra:
        raise TelemetryError(
            f"non-allowlisted surf-event field(s) would be emitted: {sorted(extra)}"
        )
    error_class = fields.get("error_class")
    if error_class is not None and error_class not in corpus.ERROR_CLASSES:
        raise TelemetryError(f"error_class {error_class!r} not in the closed set")
    for key in _LABEL_FIELDS:
        value = fields.get(key)
        if isinstance(value, str) and not _is_safe_label(value):
            raise TelemetryError(
                f"{key} is not a control-plane-safe label (too long or secret-shaped)"
            )


def _safe_surface_id(surface_id: Any) -> str | None:
    """Reduce a surface id to a cred-free opaque token.

    A surface id is expected to be a host or a slug. If a full URL slips in, we keep
    ONLY the host (``urlsplit().hostname`` drops scheme, userinfo, path, and query —
    exactly where credentials live), so a ``https://user:pass@host/…`` can never
    leak. A secret-shaped id fails closed to a stable opaque hash rather than being
    emitted verbatim."""
    if surface_id is None:
        return None
    text = str(surface_id)
    if "://" in text:
        text = _host_of(text) or "surface"
    text = text[:_MAX_LABEL]
    if looks_like_secret_value(text):
        return "surface-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return text


def build_surf_record(
    event: SurfEvent,
    *,
    surface_id: Any = None,
    ts: int | None = None,
    **fields: Any,
) -> SurfEventRecord:
    """Build a control-plane-safe ``SurfEventRecord`` or fail closed.

    Validates the event vocabulary and the field allowlist BEFORE constructing the
    record, so a non-vocabulary event or a disallowed/value-bearing field raises
    ``TelemetryError`` — regardless of whether a sink is configured. That makes a
    wiring mistake a build break in dev/CI, not a production-only leak."""
    if event not in SURF_EVENTS:
        raise TelemetryError(f"event {event!r} not in the closed SurfEvent vocabulary")
    assert_fields_allowlisted(fields)
    return SurfEventRecord(
        ts=ts if ts is not None else _now_ms(),
        event=event,
        surface_id=_safe_surface_id(surface_id),
        **fields,
    )


def assert_record_allowlisted(mapping: Mapping[str, Any]) -> None:
    """Writer-side belt-and-suspenders: reject any key not on the record schema."""
    extra = set(mapping) - RECORD_ALLOWED_KEYS
    if extra:
        raise TelemetryError(
            f"non-allowlisted surf-event key(s) would be written: {sorted(extra)}"
        )


def to_doc(record: SurfEventRecord) -> dict[str, Any]:
    """Serialize to a lean Mongo doc (drop unset fields), enforcing the record
    allowlist before it can leave the process."""
    doc = {k: v for k, v in asdict(record).items() if v is not None}
    assert_record_allowlisted(doc)
    return doc


# --------------------------------------------------------------------------- #
# Sink — lru-cached MongoClient, lazy optional pymongo, ships silent.
# --------------------------------------------------------------------------- #
def _mongo_uri() -> str | None:
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    if not uri or uri == "__unset__":
        return None
    return uri


@lru_cache(maxsize=1)
def _mongo_collection() -> Any | None:
    """The real sink target: ``gecko_events.surf_events``.

    Returns ``None`` (no-op) when ``MONGODB_URI`` is unset OR ``pymongo`` is not
    installed — so a plain OSS install never phones home and never needs the extra.
    A short server-selection timeout keeps a dead Mongo from ever backpressuring the
    agent's call path."""
    uri = _mongo_uri()
    if not uri:
        return None
    try:
        from pymongo import MongoClient  # optional extra; absent -> no-op
    except ImportError:
        logger.debug("pymongo not installed; surf events are a no-op")
        return None
    try:
        client: Any = MongoClient(uri, serverSelectionTimeoutMS=2000)
        return client[EVENTS_DB][EVENTS_COLLECTION]
    except Exception:  # noqa: BLE001 - never break import/first-call on a bad URI
        logger.warning("surf events: mongo client init failed (redacted)")
        return None


_SINK_OVERRIDE: Sink | None = None


def set_surf_sink_override(sink: Sink | None) -> None:
    """Test/opt-in seam. Inject a fake sink (receives the validated doc); ``None``
    clears it. Also resets the lru-cached Mongo client so a test toggling
    ``MONGODB_URI`` mid-run gets a fresh resolution."""
    global _SINK_OVERRIDE
    _SINK_OVERRIDE = sink
    _mongo_collection.cache_clear()


def _resolve_sink() -> Sink | None:
    """The active sink, or ``None`` (no-op). Honors the injected override first, then
    falls back to the real Mongo collection — which is itself ``None`` when unconfigured."""
    if _SINK_OVERRIDE is not None:
        return _SINK_OVERRIDE
    coll = _mongo_collection()
    if coll is None:
        return None

    def _mongo_sink(doc: Mapping[str, Any]) -> None:
        # Fire-and-forget single insert; the doc is already allowlist-validated.
        coll.insert_one(dict(doc))

    return _mongo_sink


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# The one-line emit the call sites use.
# --------------------------------------------------------------------------- #
def emit_surf_event(
    event: SurfEvent,
    *,
    surface_id: Any = None,
    **fields: Any,
) -> None:
    """Fire-and-forget one control-plane-safe usage event to
    ``gecko_events.surf_events``.

    Observe, never mutate: call sites place this OUTSIDE the returned-data path, and
    a failed emit must never break a call. Best-effort — every operational error
    (Mongo down, pymongo absent) is swallowed. A control-plane violation (a
    non-vocabulary event or a disallowed/value-bearing field) is NOT swallowed: it
    raises ``TelemetryError`` so the wiring mistake surfaces in CI. Call sites pass
    only allowlisted metadata, so this never raises in production.

    Ships silent: no-op when ``MONGODB_URI`` is unset and hard-disabled by
    ``GECKO_TELEMETRY=off``."""
    # 1. Validate + build — ALWAYS runs, so a disallowed field is a build break in
    #    dev/CI even when no sink is configured.
    record = build_surf_record(event, surface_id=surface_id, **fields)
    doc = to_doc(record)
    # 2. Opt-out gate (hard-disable).
    if not telemetry_enabled():
        return
    # 3. Ships silent: no override + no MONGODB_URI/pymongo => nothing leaves.
    sink = _resolve_sink()
    if sink is None:
        return
    try:
        sink(doc)
    except TelemetryError:
        raise  # a control-plane violation must surface, never be swallowed
    except Exception:  # noqa: BLE001 - best-effort; never break the agent's call
        logger.warning("surf event emit failed (redacted)")


__all__ = [
    "ALLOWED_FIELDS",
    "EVENTS_COLLECTION",
    "EVENTS_DB",
    "RECORD_ALLOWED_KEYS",
    "SURF_EVENTS",
    "SurfEvent",
    "SurfEventRecord",
    "assert_fields_allowlisted",
    "assert_record_allowlisted",
    "build_surf_record",
    "emit_surf_event",
    "set_surf_sink_override",
    "to_doc",
]

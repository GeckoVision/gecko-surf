"""Control-plane-safe correctness corpus — Phase 0 capture (metadata only).

Design: docs/superpowers/specs/2026-06-28-correctness-corpus-design.md.

This module persists ONLY correctness METADATA about a call — never the response
payload, never a param/path/body VALUE, never a token. Two structural guarantees
back that promise:

1. ``outcome_from`` has NO parameter through which a body or filled URL could
   enter — it takes ``status: int | None``, never the result dict that holds
   ``data`` (body) and ``request`` (filled URL).
2. The writer is an **allowlist**: ``to_record`` rejects any key not on
   ``ALLOWED_KEYS`` (fails closed), so a future careless field breaks the build
   rather than leaking.

Append-only JSONL keeps it structurally safe (no UPDATE path that could accrete a
payload) and human-auditable (``grep`` the file; assert no value substrings).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .caller import CallError

# --- the closed categorical outcome set (§1; never free text) -----------------
ERROR_CLASSES = frozenset(
    {
        "none",
        "missing_required_param",
        "enum_reject",
        "malformed_request",
        "unauthorized_401",
        "forbidden_403",
        "not_found_404",
        "unprocessable_422",
        "rate_limited_429",
        "server_5xx",
        "timeout",
        "other",
    }
)

# JSON type names (never values) for arg_shape. bool is checked before int.
_JSON_TYPES: list[tuple[type, str]] = [
    (bool, "boolean"),
    (int, "integer"),
    (float, "number"),
    (str, "string"),
    (list, "array"),
    (dict, "object"),
]


class CorpusError(Exception):
    """Raised when a record would violate the control-plane allowlist."""


@dataclass(frozen=True)
class CallOutcome:
    """Exactly the §1 allowlist — nothing else. Frozen so it can't accrete fields
    at runtime; the field set IS the persisted schema (see ``ALLOWED_KEYS``)."""

    ts: int
    surface_id: str
    surface_rev: str
    operation_id: str
    method: str
    path_template: str  # templated ("/x/{id}"), NEVER the filled URL
    params_present: list[str]  # NAMES the agent supplied, never values
    arg_shape: dict[str, str]  # name -> JSON type, never values
    body_present: bool  # whether a body was sent, never the body
    status: int | None  # core outcome signal; null on pre-flight failure
    ok: bool
    error_class: str
    first_call_correct: bool
    attempt: int
    latency_ms: int | None
    mode: str
    auth_injected: bool  # whether auth was injected — a bool, never the token


ALLOWED_KEYS = frozenset(CallOutcome.__dataclass_fields__)


def _json_type(value: Any) -> str:
    for typ, name in _JSON_TYPES:
        if isinstance(value, typ):
            return name
    return "null" if value is None else "string"


def arg_shape_of(args: Mapping[str, Any]) -> dict[str, str]:
    """Map each non-body arg NAME to its JSON type. Values never read."""
    return {k: _json_type(v) for k, v in args.items() if k != "body"}


def error_class_for(status: int | None, exc: BaseException | None) -> str:
    """Categorize an outcome from the status code + exception TYPE only.

    Never inspects an upstream error BODY (that would be a payload). For pre-flight
    failures (no network call) ``status is None`` and the exception type decides.
    """
    if status is not None:
        if 200 <= status < 400:
            return "none"
        return {
            401: "unauthorized_401",
            403: "forbidden_403",
            404: "not_found_404",
            422: "unprocessable_422",
            429: "rate_limited_429",
        }.get(status, "server_5xx" if status >= 500 else "malformed_request")
    if isinstance(exc, CallError):
        msg = str(exc).lower()
        if "path parameter" in msg or "required" in msg:
            return "missing_required_param"
        return "malformed_request"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "other"


def outcome_from(
    *,
    operation_id: str,
    tool_invoke: Mapping[str, Any],
    args: Mapping[str, Any],
    status: int | None,
    error_class: str,
    latency_ms: int | None,
    mode: str,
    auth_injected: bool,
    ts: int,
    surface_id: str,
    surface_rev: str,
    attempt: int = 1,
) -> CallOutcome:
    """Build a control-plane-safe ``CallOutcome``.

    NOTE the signature: it takes ``status: int | None``, NOT the result dict — the
    response body and filled URL physically cannot enter this function. ``args`` is
    read for NAMES and TYPES only (``params_present`` / ``arg_shape``); values are
    never copied out.
    """
    if error_class not in ERROR_CLASSES:
        raise CorpusError(f"error_class {error_class!r} not in the closed set")
    ok = status is not None and 200 <= status < 400
    return CallOutcome(
        ts=ts,
        surface_id=surface_id,
        surface_rev=surface_rev,
        operation_id=operation_id,
        method=str(tool_invoke["method"]),
        path_template=str(tool_invoke["path"]),  # template from the tool def
        params_present=[k for k in args if k != "body"],
        arg_shape=arg_shape_of(args),
        body_present="body" in args,
        status=status,
        ok=ok,
        error_class=error_class,
        first_call_correct=ok and attempt == 1,
        attempt=attempt,
        latency_ms=latency_ms,
        mode=mode,
        auth_injected=auth_injected,
    )


def assert_allowlisted(mapping: Mapping[str, Any]) -> None:
    """Reject (fail closed) any key not on the §1 allowlist."""
    extra = set(mapping) - ALLOWED_KEYS
    if extra:
        raise CorpusError(f"non-allowlisted key(s) would be persisted: {sorted(extra)}")


def to_record(outcome: CallOutcome) -> dict[str, Any]:
    """Serialize to a plain dict, enforcing the allowlist before it can be written."""
    record_dict = asdict(outcome)
    assert_allowlisted(record_dict)
    return record_dict


def record(outcome: CallOutcome, path: str | Path) -> None:
    """Append one allowlisted JSONL record. Best-effort: a corpus write must never
    break the agent's call, so failures are swallowed with a redacted note (the
    record contents are never echoed, to avoid re-leaking input)."""
    try:
        record_dict = to_record(outcome)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_dict) + "\n")
    except CorpusError:
        raise  # a control-plane violation must surface, not be swallowed
    except Exception:  # noqa: BLE001 - best-effort; never break the call
        import logging

        logging.getLogger("gecko.corpus").warning("corpus write failed (redacted)")

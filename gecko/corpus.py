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
from .sanitize import looks_like_secret_value

# --- the closed categorical outcome set (§1; never free text) -----------------
# Append-only to the CLOSED set. ``auth_host_blocked`` records that Gecko refused to
# inject the customer's secret toward a drifted/untrusted host (the exfil defense fired)
# — a distinct, countable outcome that still stores no host value.
ERROR_CLASSES = frozenset(
    {
        "none",
        "missing_required_param",
        "enum_reject",
        "malformed_request",
        "auth_host_blocked",
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
        if "refusing to inject auth" in msg:
            return "auth_host_blocked"
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


# --- adversarial (red-team) telemetry — a control-plane-safe sibling of CallOutcome ---
# The safety dimension of the moat: one categorical record per graded agent decision. Same
# discipline as CallOutcome — closed sets, an allowlist writer, append-only JSONL. It NEVER
# stores a canary, host, address, amount, or any arg value; only channel NAMES and booleans.

# The closed set of reasons a Gecko defense blocked an adversarial action. Append-only,
# never free text — a stray reason breaks the build (mirrors ``ERROR_CLASSES``).
BLOCKED_REASONS = frozenset(
    {
        "none",
        "instruction_stripped",  # sanitizer redacted poisoned desc/param text
        "secret_value_dropped",  # sanitizer dropped a secret default/example/enum
        "address_value_dropped",  # sanitizer dropped an attacker-address routing value
        "surface_quarantined",  # poisoned surface -> no auth, recorded-only
        "auth_host_blocked",  # caller refused injection toward a drifted host
        "auth_location_blocked",  # auth would land in a loggable url (query/path/cookie)
        "required_guard",  # a missing safety field was caught pre-flight
        "integrity_tripped",  # tools_rev mismatch
        "payment_reqs_untrusted",  # x402 challenge failed the provisioning policy
        "observation_quarantined",  # an L3 poisoned observation was neutralized
        "policy_refused",  # the agent policy itself refused (L3 measure-only)
    }
)

# The 2x2 verdict cells: decision(proposed|refused) x ground-truth(harm|clean).
VERDICTS = frozenset({"blocked", "exploited", "served", "over_refused"})

# The closed channel-name forms ``leak_sink`` may take. It is the ONE record field derived
# from request data (a header NAME comes from a spec-declared param), so it is guarded like
# the categorical axes: a loggable url/body, or ``header:<name>`` where the name is short and
# NOT itself secret-shaped (a poisoned spec could name a header param a secret string).
_LEAK_SINK_CHANNELS = frozenset({"url", "body"})
_MAX_LEAK_SINK_NAME = 128


def _leak_sink_is_valid(leak_sink: str | None) -> bool:
    if leak_sink is None or leak_sink in _LEAK_SINK_CHANNELS:
        return True
    if leak_sink.startswith("header:"):
        name = leak_sink[len("header:") :]
        return (
            bool(name)
            and len(name) <= _MAX_LEAK_SINK_NAME
            and not looks_like_secret_value(name)
        )
    return False


# The harm-family axis: A exfiltration, B unauthorized action, C misdirection, D refusal-abuse.
FAMILIES = frozenset({"A", "B", "C", "D"})


@dataclass(frozen=True)
class AdversarialOutcome:
    """Control-plane-safe red-team record — categorical/bool fields ONLY, never a value.

    The only secret-shaped strings the harness ever handles are ``CANARY_*`` constants
    (never real secrets), and even those never reach this record: ``leak_sink`` is a
    channel NAME (``"header:X-Api-Token"``, ``"url"``, ``"body"``), never the leaked value.
    Frozen so it can't accrete fields at runtime; the field set IS the persisted schema
    (see ``ADVERSARIAL_ALLOWED_KEYS``)."""

    ts: int
    scenario_id: str
    family: str  # A|B|C|D (validated against FAMILIES)
    tier: int  # 0|1
    layer: str  # L1|L2|L3
    vector: str  # description|servers_url|schema_default|x402|response|none
    polarity: str  # attack|benign
    policy_id: str
    defenses: list[str]  # subset of ("sanitizer","auth_firewall","quarantine")
    verdict: str  # VERDICTS
    tripped_predicate: str  # predicate id, "" if none
    blocked_reason: str  # BLOCKED_REASONS
    leaked: bool
    leak_sink: str | None  # channel NAME only, never a value
    auth_host_ok: bool


ADVERSARIAL_ALLOWED_KEYS = frozenset(AdversarialOutcome.__dataclass_fields__)


def assert_adversarial_allowlisted(mapping: Mapping[str, Any]) -> None:
    """Reject (fail closed) any key not on the ``AdversarialOutcome`` allowlist."""
    extra = set(mapping) - ADVERSARIAL_ALLOWED_KEYS
    if extra:
        raise CorpusError(
            f"non-allowlisted red-team key(s) would be persisted: {sorted(extra)}"
        )


def to_adversarial_record(outcome: AdversarialOutcome) -> dict[str, Any]:
    """Serialize an ``AdversarialOutcome``, enforcing the allowlist AND the closed-set
    axes before it can be written — the same fail-closed discipline ``outcome_from``
    applies to ``error_class``. Raises ``CorpusError`` on any off-set categorical value."""
    record_dict = asdict(outcome)
    assert_adversarial_allowlisted(record_dict)
    if outcome.family not in FAMILIES:
        raise CorpusError(f"family {outcome.family!r} not in the closed set")
    if outcome.verdict not in VERDICTS:
        raise CorpusError(f"verdict {outcome.verdict!r} not in the closed set")
    if outcome.blocked_reason not in BLOCKED_REASONS:
        raise CorpusError(
            f"blocked_reason {outcome.blocked_reason!r} not in the closed set"
        )
    if not _leak_sink_is_valid(outcome.leak_sink):
        raise CorpusError(
            "leak_sink is not a valid channel name (url|body|header:<name>)"
        )
    return record_dict


def record_adversarial(outcome: AdversarialOutcome, path: str | Path) -> None:
    """Append one allowlisted red-team JSONL record. Best-effort like ``record``: a corpus
    write must never break the harness, so non-violation failures are swallowed with a
    redacted note; a control-plane violation (``CorpusError``) still surfaces."""
    try:
        record_dict = to_adversarial_record(outcome)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_dict) + "\n")
    except CorpusError:
        raise  # a control-plane violation must surface, not be swallowed
    except Exception:  # noqa: BLE001 - best-effort; never break the harness
        import logging

        logging.getLogger("gecko.corpus").warning(
            "adversarial corpus write failed (redacted)"
        )

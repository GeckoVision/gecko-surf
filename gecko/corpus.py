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

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from .caller import CallError
from .pda import (
    ConstantPdaSeedNode,
    OrderedPairPdaSeedNode,
    PdaNode,
    PdaSeed,
    ResolverPdaSeedNode,
    SeedEncoding,
    SeedSource,
    VariablePdaSeedNode,
)
from .networks import LEGACY_NETWORK_ALIASES
from .networks import NETWORKS as NETWORKS  # re-export: ONE declaration, in networks.py
from .networks import Network, network_from_label
from .sanitize import looks_like_secret_value
from .simulate import REVERT_FAMILIES, Receipt, revert_family

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

# --- the two orthogonal, one-way provenance axes (feedback-capture decision) --------
# Both are closed Literals defined HERE (single source of truth) and imported by every
# consumer (telemetry/events/client); never redeclared.
#
# ``source`` answers "HOW was the outcome obtained?" — i.e. did ``status`` come from the
# wire? It is DERIVED from the capture mode at the ``outcome_from`` boundary (see
# ``source_for_mode``), never free-set by a caller, and it governs ROUTING: only
# ``observed`` rows may feed the published first-call-correct / adoption rate.
#
# ``simulated`` is the D2 tier: the outcome comes from Gecko's OWN ``simulate()`` run
# against a fork/RPC it controls — there is no agent data plane to capture. Like
# ``synthetic`` it is NOT wire-observed, so it is structurally excluded from the published
# first-call-correct metric (``telemetry`` filters ``source == "observed"``); its value is
# the drift SERIES over slots, not the FCC rate. It lives in its own segregated file.
OutcomeSource = Literal["observed", "reported", "synthetic", "simulated"]
OUTCOME_SOURCES: frozenset[str] = frozenset(get_args(OutcomeSource))

# ``tenancy`` answers "may this record LEAVE the machine into the cross-customer corpus?"
# It is a one-way governance promise. Like ``source`` it is DERIVED at the boundary (see
# ``consented_tenancy``) and is NOT a parameter — a caller cannot label its own rows
# egress-eligible. No egress layer consumes ``contributed`` today; the axis exists so it
# is never retrofitted onto rows written before it (that is the failure mode).
Tenancy = Literal["local", "contributed"]
TENANCIES: frozenset[str] = frozenset(get_args(Tenancy))

# Widening order on the tenancy axis: ``local`` is the narrowest label. A reader admits a
# row whose tenancy is no WIDER than the machine's own consent (see ``tenancy_admissible``).
_TENANCY_WIDTH: dict[str, int] = {"local": 0, "contributed": 1}

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


# --- the free-text channel guard — invariant #1 applied to our OWN store ---------------
# ``error_class`` is a closed set, gated twice. The remaining free-text channels into a
# persisted row were ungated, and they do NOT come from us:
#
#   * ``operation_id`` / ``path_template`` — UNTRUSTED-SPEC-derived. A poisoned spec picks
#     the operationId and the path template, so it chooses what gets written into our
#     store. That is a secret-exfiltration pipe pointed at ourselves.
#   * ``arg_names`` (the single channel feeding BOTH ``params_present`` and the
#     ``arg_shape`` KEYS) — AGENT-supplied at call time. A compromised agent, not only a
#     poisoned spec, chooses those strings.
#
# The predicate is ``looks_like_secret_value`` ALONE — deliberately NOT the length cap and
# base58 full-match ``_guard_plan_name`` applies to PDA/plan names. Calibrated against
# every spec fixture in the repo (see ``tests/test_corpus_freetext_guard.py``): the secret
# predicate has ZERO false positives across 366 operationIds / 335 paths / 143 param
# names, while a 64-char cap rejects a real 81-char Pegana path and a base58 full-match
# rejects the real operationId ``getApiFixturesUpdatesEpochdayHourofday``. A false
# rejection here is not a harmless refusal — it silently removes a row from the metric
# denominator, so over-strictness has its own correctness cost.
#
# It FAILS CLOSED by REFUSING the row, never by truncating: a truncated secret is still a
# secret, and a truncated row silently enters the denominator.

#: The closed set of field labels the free-text guard may name. ``arg_names`` is one
#: channel because ``params_present`` and the ``arg_shape`` keys are the same strings from
#: the same source — guarded once, derived twice.
GUARDED_FREETEXT_FIELDS: frozenset[str] = frozenset(
    {"operation_id", "path_template", "arg_names"}
)


class FreeTextGuardError(CorpusError):
    """A free-text field carried a secret-shaped value and the row was refused.

    Carries the CATEGORICAL ``field`` (a ``GUARDED_FREETEXT_FIELDS`` member) so the
    capture boundary can count the drop without parsing a message — and never carries the
    offending value: an exception message is a log line waiting to happen.
    """

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(
            f"{field}: value is secret-shaped — refusing to persist the row (redacted)"
        )


def guard_freetext(entry: object, field: str) -> str:
    """Admit a spec/agent-derived free-text field only if it is not secret-shaped.

    A gate, not a transformer: a clean value is returned byte-identical. Raises
    ``FreeTextGuardError`` (a ``CorpusError``) otherwise — the caller must REFUSE the
    row, never truncate it."""
    if not isinstance(entry, str):
        raise CorpusError(f"{field}: value must be a string (redacted)")
    if looks_like_secret_value(entry):
        raise FreeTextGuardError(field)
    return entry


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
    # --- provenance axes (derived/defaulted; never a value) ---------------------------
    source: str  # OutcomeSource — HOW obtained; DERIVED from mode, gates the FCC metric
    tenancy: str = (
        "local"  # Tenancy — may this record egress? default local (fail closed)
    )


ALLOWED_KEYS = frozenset(CallOutcome.__dataclass_fields__)


def _json_type(value: Any) -> str:
    for typ, name in _JSON_TYPES:
        if isinstance(value, typ):
            return name
    return "null" if value is None else "string"


def arg_shape_of(args: Mapping[str, Any]) -> dict[str, str]:
    """Map each non-body arg NAME to its JSON type. Values never read.

    The KEYS are agent-supplied free text, so they go through ``guard_freetext`` HERE too
    — this helper is public, and a guard that only lived in ``outcome_from`` would leave a
    bypass for any future caller."""
    return {
        guard_freetext(name, "arg_names"): _json_type(value)
        for name, value in args.items()
        if name != "body"
    }


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
    if exc is None:
        # (status=None, exc=None): a well-formed request that never hit the wire — the
        # synthetic/pre-flight-OK case. NOT a failure; before this it fell through to
        # "other" and mislabeled every synthetic success as a generic error.
        return "none"
    return "other"


# --- source is DERIVED from mode; a caller can never free-set it wrong -----------------
_MODE_TO_SOURCE: dict[str, str] = {
    "live": "observed",  # a real upstream status came off the wire
    "reported": "reported",  # an agent claimed the status (future report path)
    "recorded": "synthetic",  # recorded mode fabricates a 200 — never observed
    # probe = the offline sandbox (gecko.sandbox): the status is fabricated from the
    # spec's own schemas, so it routes synthetic — structurally excluded from every
    # published metric. Explicit (not just the fail-closed default) so the routing is
    # a stated contract, not an accident of the fallback.
    "probe": "synthetic",
}


def source_for_mode(mode: str) -> str:
    """Derive the provenance ``source`` from the capture ``mode``.

    The key question (feedback-capture decision, "Two axes"): *did ``status`` come from
    the wire?* ``live`` → ``observed``; ``reported`` → ``reported``; everything else —
    recorded mode's faked ``200`` and the validator's synthetic run — → ``synthetic``.
    Fails CLOSED to ``synthetic`` (the non-published bucket), so an unrecognized mode can
    never inflate the observed first-call-correct rate."""
    return _MODE_TO_SOURCE.get(mode, "synthetic")


# --- tenancy is DERIVED from explicit local operator consent, never accepted -----------
# Same doctrine as ``source``: a value a caller could hand us is a value a caller could
# mislabel, so the boundary derives it. The predicate is DEFAULT-DENY and
# TRUSTED-SOURCE-ONLY — it reads exactly two places, both explicit local operator state:
#
#   1. the ``GECKO_CORPUS_CONSENT`` env var, and
#   2. a ``corpus-consent`` file in the operator's own gecko config home.
#
# It reads NOTHING else: not spec text, not a response, not an agent arg, not a
# CWD-relative file (a ``GECKO_CONFIG_HOME`` override must be ABSOLUTE — a relative
# override is CWD-relative state and is refused). Missing, unreadable, unparsable, or
# unrecognized state yields ``local``. Nothing in the repo consumes ``contributed`` and
# this adds no egress; deriving from ambient state is a widening, safe only while nothing
# acts on the flag.
_CONSENT_ENV = "GECKO_CORPUS_CONSENT"
_CONSENT_HOME_ENV = "GECKO_CONFIG_HOME"
_CONSENT_FILENAME = "corpus-consent"
# The ONE accepted token — an exact match after strip/lower. "yes"/"true"/"1" are NOT
# consent to publish; opting a machine into a cross-customer corpus must be unambiguous.
_CONSENT_TOKEN = "contributed"
# A consent marker is one short word. Cap the read so a huge/binary file is refused
# rather than slurped (and so the token cannot hide at the end of a padded file).
_MAX_CONSENT_BYTES = 64


def _consent_home() -> Path:
    """The operator's gecko config dir. ``GECKO_CONFIG_HOME`` overrides it ONLY when
    absolute (a relative override would make consent CWD-relative — untrusted)."""
    override = os.environ.get(_CONSENT_HOME_ENV)
    if override:
        candidate = Path(override)
        if candidate.is_absolute():
            return candidate
    return Path.home() / ".gecko"


def consent_path() -> Path:
    """``<config home>/corpus-consent`` — the file form of the operator's opt-in."""
    return _consent_home() / _CONSENT_FILENAME


def _consent_marker() -> str | None:
    """The raw consent marker from trusted local state, or ``None`` if unavailable.

    Env var first (an operator setting it is explicit), then the config-home file. Any
    read/decode failure is indistinguishable from absence — the caller fails closed."""
    from_env = os.environ.get(_CONSENT_ENV)
    if from_env is not None:
        return from_env
    try:
        with consent_path().open("rb") as fh:
            # Read one byte past the cap so an over-long file is detectable, not truncated
            # into a false positive.
            raw = fh.read(_MAX_CONSENT_BYTES + 1)
    except OSError:
        return None  # missing, a directory, unreadable — all fail closed
    if len(raw) > _MAX_CONSENT_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def consented_tenancy() -> str:
    """Derive the ``tenancy`` label from local operator consent. Takes NO arguments —
    structurally, agent args / spec text / a response cannot reach it.

    Returns ``contributed`` only on an exact ``contributed`` marker in trusted local
    state; every other case (absent, unreadable, unparsable, unrecognized) returns
    ``local``. Re-checked against the closed ``TENANCIES`` set before returning, so a
    future edit here cannot invent a third label."""
    marker = _consent_marker()
    tenancy = (
        _CONSENT_TOKEN if (marker or "").strip().lower() == _CONSENT_TOKEN else "local"
    )
    return tenancy if tenancy in TENANCIES else "local"


def tenancy_admissible(tenancy: object) -> bool:
    """May a READER accept a row carrying this tenancy on THIS machine?

    Fails closed twice: the label must be a closed-set member, and it must be no WIDER
    than the machine's own derived consent. So a hand-written ``contributed`` row is
    refused on a machine that never opted in (the read-path hole), while ``local`` — the
    narrowest label — always reads, so opting in never orphans older rows."""
    if not isinstance(tenancy, str) or tenancy not in TENANCIES:
        return False
    return _TENANCY_WIDTH[tenancy] <= _TENANCY_WIDTH[consented_tenancy()]


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

    Neither provenance axis is a parameter. ``source`` is DERIVED from ``mode`` (see
    ``source_for_mode``) so a caller cannot mislabel a faked recorded ``200`` as
    ``observed``; ``tenancy`` is DERIVED from local operator consent (see
    ``consented_tenancy``) so a caller cannot label its own rows egress-eligible. Both
    are re-checked against their closed sets before construction.

    The three remaining FREE-TEXT channels are gated by ``guard_freetext``:
    ``operation_id`` and ``path_template`` (untrusted-spec-derived) and the arg NAMES
    (agent-supplied, feeding both ``params_present`` and the ``arg_shape`` keys). A
    secret-shaped value REFUSES the whole row — see ``gecko.capture.record_outcome`` for
    why that refusal must never propagate into the agent's call.
    """
    if error_class not in ERROR_CLASSES:
        raise CorpusError(f"error_class {error_class!r} not in the closed set")
    tenancy = consented_tenancy()
    if tenancy not in TENANCIES:
        raise CorpusError(f"tenancy {tenancy!r} not in the closed set")
    source = source_for_mode(mode)
    # Belt-and-suspenders: the derivation is total, so this only trips if _MODE_TO_SOURCE
    # ever drifts off the closed set — a build break, not a silent smuggle.
    if source not in OUTCOME_SOURCES:
        raise CorpusError(f"source {source!r} not in the closed set")
    ok = status is not None and 200 <= status < 400
    # Guard the arg NAMES once; params_present and the arg_shape keys are the same
    # strings from the same (agent-supplied) source, so both are derived from the guarded
    # set — no second, forgettable gate.
    arg_names = [guard_freetext(name, "arg_names") for name in args if name != "body"]
    return CallOutcome(
        ts=ts,
        surface_id=surface_id,
        surface_rev=surface_rev,
        operation_id=guard_freetext(operation_id, "operation_id"),
        method=str(tool_invoke["method"]),
        # template from the tool def — spec-chosen, therefore untrusted
        path_template=guard_freetext(str(tool_invoke["path"]), "path_template"),
        params_present=arg_names,
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
        source=source,
        tenancy=tenancy,
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


def outcome_from_record(record: Mapping[str, Any]) -> CallOutcome:
    """Rehydrate a persisted JSONL record back into a ``CallOutcome`` — the read side of
    ``to_record``, and the offline-replay seam for a pre-captured corpus.

    Fails CLOSED the same way the writer does: any key not on the §1 allowlist (the shape a
    leaked body or filled URL would take) is rejected before construction, so a payload can
    never ride in through a fixture (invariant #1). Missing a required field is an equally
    hard error — a truncated record is never silently defaulted.

    It also RE-DERIVES what the writer derived, because membership alone still admits a
    forged row (``source="observed"`` on a ``mode="recorded"`` row would smuggle a faked
    ``200`` into the wire-only bucket):

    * ``tenancy`` must be a closed-set member no wider than this machine's own consent
      (``tenancy_admissible``) — a hand-written ``contributed`` row is refused;
    * ``source`` must be the closed-set value ``source_for_mode(mode)`` derives;
    * ``error_class`` must be a closed-set member;
    * ``ok`` and ``first_call_correct`` must equal what ``outcome_from`` would compute
      from ``status``/``attempt``.

    Every row written through ``outcome_from`` satisfies all of these by construction, so
    anything that trips here is a forged or hand-edited row. Full type-shape validation of
    the remaining fields (e.g. ``latency_ms`` being an int) stays a known residual."""
    assert_allowlisted(record)
    missing = ALLOWED_KEYS - set(record)
    if missing:
        raise CorpusError(f"record missing required key(s): {sorted(missing)}")

    tenancy = record["tenancy"]
    if not tenancy_admissible(tenancy):
        # Redacted: name the axis, never echo the row.
        raise CorpusError(
            "record tenancy is not a closed-set member, or is wider than this "
            "machine's derived consent — refused"
        )
    source = record["source"]
    if source not in OUTCOME_SOURCES:
        raise CorpusError(f"record source {source!r} not in the closed set")
    if source != source_for_mode(str(record["mode"])):
        raise CorpusError(
            "record source is not the value its mode derives — a forged provenance label"
        )
    if record["error_class"] not in ERROR_CLASSES:
        raise CorpusError("record error_class not in the closed set")

    status = record["status"]
    if status is not None and (isinstance(status, bool) or not isinstance(status, int)):
        raise CorpusError("record status must be an int or null")
    expected_ok = status is not None and 200 <= status < 400
    if record["ok"] is not expected_ok:
        raise CorpusError("record ok is not derivable from its status")
    if record["first_call_correct"] is not (expected_ok and record["attempt"] == 1):
        raise CorpusError(
            "record first_call_correct is not derivable from its status/attempt"
        )
    # Re-run the free-text guard on the READ side too — same both-boundaries doctrine as
    # ``tenancy``. A row written before the guard existed (or a hand-supplied fixture) can
    # still carry a secret-shaped name, and rehydrating it would re-admit the value into
    # memory and into whatever the reader does next.
    guard_freetext(record["operation_id"], "operation_id")
    guard_freetext(record["path_template"], "path_template")
    for name in list(record["params_present"]) + list(record["arg_shape"]):
        guard_freetext(name, "arg_names")
    return CallOutcome(**{key: record[key] for key in ALLOWED_KEYS})


def synthetic_sibling(path: str | Path) -> Path:
    """The segregated file for ``synthetic`` outcomes, co-located with the corpus
    (``<dir>/synthetic.jsonl``).

    Segregation is by PATH, not by an in-band tag, and that is deliberate: a reader of
    the main corpus (``telemetry.aggregate``, the §4 build-time aggregator, an ad-hoc
    ``grep``) that doesn't know about synthetic then NEVER sees a synthetic row — it
    FAILS CLOSED. An in-band "exclude synthetic at query time" would fail open (every
    reader must remember to filter; forgetting = corruption)."""
    return Path(path).with_name("synthetic.jsonl")


#: The segregated file name for Gecko's OWN self-check outcomes. A module constant (not a
#: literal at the call site) so the one place that names the tier is the one place that can
#: change it.
SELFCHECK_FILENAME = "selfcheck.jsonl"


def selfcheck_sibling(path: str | Path) -> Path:
    """The segregated file for outcomes of calls GECKO made about itself, co-located with
    the corpus (``<dir>/selfcheck.jsonl``).

    The third segregation axis, and the one the ``source`` axis structurally cannot
    express. ``source`` answers *did the status come off the wire?* — and for a
    ``verify-docs --live`` probe the honest answer is YES, so the row is genuinely
    ``observed``. The question this file answers is a different one: *who made the call?*
    A self-check is Gecko walking every operation with arguments IT synthesized; where a
    required path param had no spec example and no DECLARED value domain, the id was
    INVENTED (see ``validator.synthesized_route_args``). The resulting 404 is evidence
    about our placeholder, never about the endpoint — and no agent ever made that call, so
    it must not sit in the denominator of a metric that claims agents call this API right
    the first time.

    Segregation is by PATH, not by an in-band tag or a relabelled ``source`` — the same
    posture as ``synthetic_sibling`` and ``simulated_sibling``, for the same reason: a
    reader of the main corpus that has never heard of the self-check tier then NEVER sees a
    self-check row (it FAILS CLOSED). An "exclude self-checks at query time" tag would fail
    OPEN — every current and future reader would have to remember the filter, and one that
    forgets silently corrupts the published rate. Relabelling ``source`` would be worse
    still: it would make a real wire observation lie about how it was obtained, and would
    trip ``outcome_from_record``'s source-must-equal-``source_for_mode(mode)`` check on the
    way back in.

    IDEMPOTENT on purpose: re-deriving from an already-segregated path returns that path,
    so a nested or repeated scope can never walk back to the main corpus.

    The rows stay fully readable — ``verify.load_observed_corpus`` takes an explicit path,
    so a later, unrelated run can point it here and replay real prior wire outcomes
    offline (Pattern B). Nothing about this file is scoped to a run or session id."""
    target = Path(path)
    if target.name == SELFCHECK_FILENAME:
        return target
    return target.with_name(SELFCHECK_FILENAME)


def record(outcome: CallOutcome, path: str | Path) -> None:
    """Append one allowlisted JSONL record. Best-effort: a corpus write must never
    break the agent's call, so failures are swallowed with a redacted note (the
    record contents are never echoed, to avoid re-leaking input).

    Routing is by ``source``: a ``synthetic`` outcome is diverted to the segregated
    ``synthetic.jsonl`` sibling so the main corpus only ever holds real (observed /
    reported) rows. Enforced HERE — the single write boundary — so no call site can
    forget to segregate."""
    try:
        record_dict = to_record(outcome)
        target = (
            synthetic_sibling(path) if outcome.source == "synthetic" else Path(path)
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_dict) + "\n")
    except CorpusError:
        raise  # a control-plane violation must surface, not be swallowed
    except Exception:  # noqa: BLE001 - best-effort; never break the call
        import logging

        logging.getLogger("gecko.corpus").warning("corpus write failed (redacted)")


# --- the dropped-row counter — a refused row must be COUNTABLE, not silent -------------
# Refusing a row is the right failure mode, but a silent refusal is a hole in the metric
# DENOMINATOR: the published first-call-correct rate would quietly stop seeing the surface
# a poisoned spec attacked. So every refusal writes one categorical marker to a segregated
# ``dropped.jsonl`` sibling — same discipline as every other tier here (closed sets, an
# allowlist writer, append-only JSONL, segregation by PATH so a reader that doesn't know
# about drops never mistakes one for an outcome).
#
# The drop record must not become the leak it exists to report: it carries the FIELD label
# and the REASON, never the offending value, and its own ``surface_id`` runs through the
# same secret predicate (falling back to a categorical ``"redacted"`` — refusing the DROP
# record would restore the silent hole this exists to close).

#: Why a row was refused. Closed, append-only — never free text.
DROPPED_REASONS: frozenset[str] = frozenset(
    {
        "secret_shaped",  # a free-text field carried a secret-shaped value
        "control_plane_violation",  # any other CorpusError at the write boundary
    }
)

#: Which channel tripped. ``other`` is the fail-closed bucket for a non-guard violation.
DROPPED_FIELDS: frozenset[str] = GUARDED_FREETEXT_FIELDS | {"other"}

#: The categorical stand-in written when a drop record's own ``surface_id`` is unsafe.
_REDACTED_SURFACE_ID = "redacted"


@dataclass(frozen=True)
class DroppedRow:
    """One refused corpus row, as a countable categorical marker — never a value."""

    ts: int
    surface_id: str  # opaque surface label, redacted if itself secret-shaped
    field: str  # DROPPED_FIELDS member
    reason: str  # DROPPED_REASONS member
    source: str  # OutcomeSource — WHICH bucket lost a row (observed/synthetic/...)


DROPPED_ALLOWED_KEYS = frozenset(DroppedRow.__dataclass_fields__)


def dropped_sibling(path: str | Path) -> Path:
    """The segregated file for refused rows (``<dir>/dropped.jsonl``). Segregated by PATH
    for the same reason as ``synthetic_sibling``: a reader of the main corpus can never
    mistake a drop marker for an outcome."""
    return Path(path).with_name("dropped.jsonl")


def to_dropped_record(row: DroppedRow) -> dict[str, Any]:
    """Serialize a ``DroppedRow``, enforcing the allowlist AND the closed-set axes.

    ``surface_id`` is the one free-text field, and it is REDACTED rather than rejected —
    a refused drop record would put the denominator hole back."""
    record_dict = asdict(row)
    extra = set(record_dict) - DROPPED_ALLOWED_KEYS
    if extra:
        raise CorpusError(
            f"non-allowlisted dropped-row key(s) would be persisted: {sorted(extra)}"
        )
    if row.field not in DROPPED_FIELDS:
        raise CorpusError(f"dropped field {row.field!r} not in the closed set")
    if row.reason not in DROPPED_REASONS:
        raise CorpusError(f"dropped reason {row.reason!r} not in the closed set")
    if row.source not in OUTCOME_SOURCES:
        raise CorpusError(f"dropped source {row.source!r} not in the closed set")
    if not isinstance(row.surface_id, str) or looks_like_secret_value(row.surface_id):
        record_dict["surface_id"] = _REDACTED_SURFACE_ID
    return record_dict


def record_dropped(row: DroppedRow, path: str | Path) -> None:
    """Append one categorical drop marker to the segregated ``dropped.jsonl`` sibling.

    Best-effort on I/O like ``record`` (a counter must never break the caller), but a
    closed-set violation still raises — the drop vocabulary is as fail-closed as the
    outcome vocabulary."""
    try:
        record_dict = to_dropped_record(row)
    except CorpusError:
        raise  # an off-vocabulary drop marker is a build break, not a silent write
    try:
        target = dropped_sibling(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_dict) + "\n")
    except Exception:  # noqa: BLE001 - best-effort; never break the caller
        import logging

        logging.getLogger("gecko.corpus").warning("dropped-row write failed (redacted)")


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
        # --- on-chain enforce reasons (battle-test v1.5; data, no Solana code here) ---
        # The Surfpool substrate grades the account-state diff, not a prepared request, so a
        # blocked on-chain action attributes to one of these. Categorical only — never an
        # address, amount, or programId value (mirrors the off-chain reasons above).
        "receiver_not_allowlisted",  # a net-receiver in the diff was not intent-authorized
        "program_not_allowlisted",  # a (CPI-mined) programId was not intent-authorized
        "oracle_state_unpinned",  # re-profile diverged from the pinned oracle state
        "hook_reverted",  # a Token-2022 transfer-hook (firewall denylist) reverted the exit
        "idl_sanitized",  # the IDL sanitizer stripped a poisoned account role / injected op
    }
)

# The 2x2 verdict cells: decision(proposed|refused) x ground-truth(harm|clean).
VERDICTS = frozenset({"blocked", "exploited", "served", "over_refused"})

# The closed channel-name forms ``leak_sink`` may take. It is the ONE record field derived
# from request data (a header NAME comes from a spec-declared param), so it is guarded like
# the categorical axes: a loggable url/body, or ``header:<name>`` where the name is short and
# NOT itself secret-shaped (a poisoned spec could name a header param a secret string).
# ``memo``/``log`` are the on-chain channel names (battle-test v1.5): a secret-shaped byte
# string in a Memo instruction or a program log message — the on-chain analog of url/body.
_LEAK_SINK_CHANNELS = frozenset({"url", "body", "memo", "log"})
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


# --- simulated (self-run Receipt) tier — the third control-plane-safe sibling ---------
# The D2 corpus (docs/specs/2026-08-04-delivery2-simulated-corpus.md). Gecko runs its OWN
# ``simulate()`` against a fork/RPC it controls and records the CATEGORICAL outcome — there
# is no agent data plane to capture. Same discipline as CallOutcome/AdversarialOutcome:
# closed sets, an allowlist writer, an append-only segregated JSONL file. It NEVER stores a
# pubkey, balance, amount, instruction data, the revert LOG string, or an RPC URL — only
# names/families/hashes and public integers (slot, error_code, units — analogous to the
# raw ``latency_ms`` CallOutcome already stores).

# The land/no-land verdict — mirrors Receipt.status (simulate.py). Closed, never free text.
SIM_STATUSES: frozenset[str] = frozenset({"pass", "fail", "unknown"})

# The revert FAMILY set is imported from simulate.py — the single source of truth for the
# revert vocabulary (both classify_revert and the corpus name the same families). Never
# redeclared here; a change to the vocabulary can only happen in one place.
REVERT_CLASSES: frozenset[str] = REVERT_FAMILIES

# The network the sim ran against — a categorical LABEL, never the RPC URL (a URL could
# carry an API key). ``fork`` is a mainnet-backed snapshot; ``unknown`` is the fail-closed
# bucket. DECLARED IN ``gecko/networks.py`` and imported above, never redeclared here: the
# signing gate compares the same vocabulary off ``Receipt.network``, and two spellings of
# one concept is how a row ends up categorised ``mainnet`` in one module and ``unknown``
# in the other. ``other`` was this module's older name for the ``unknown`` bucket — same
# bucket, one spelling now, and ``LEGACY_NETWORK_ALIASES`` reads back the rows already
# written under the old one.
#
# SHARED MEMBERS, DIFFERENT PROVENANCE. A row's network may be DERIVED from prose
# (``network_category`` below); a signing decision's may not. The vocabulary is one; what
# is allowed to produce a value for each consumer is not.


@dataclass(frozen=True)
class SimulatedOutcome:
    """Control-plane-safe self-simulation record — categorical/structural fields ONLY.

    Built by ``simulated_outcome_from`` from a :class:`~gecko.simulate.Receipt`'s
    CATEGORICAL fields plus the plan's values-free structural fingerprint. It reads the
    instruction/account/arg NAMES and the recipe SHAPE, never the resolved pubkeys/amounts
    — the same "names & types, never values" rule ``outcome_from`` follows. Frozen so it
    can't accrete fields at runtime; the field set IS the persisted schema (see
    ``SIMULATED_ALLOWED_KEYS``).
    """

    ts: int
    surface_id: str  # "orquestra:<program_id>" — a program id, public, not a secret
    program_id: str  # the on-chain program, public
    instruction: str  # instruction NAME ("buy"), never args
    recipe_hash: str  # sha256 hex of the STRUCTURAL fingerprint (§3) — values-free
    status: str  # SIM_STATUSES: "pass" | "fail" | "unknown"
    revert_class: str  # REVERT_CLASSES (closed family) — never free text, never the log
    error_code: int | None  # Anchor/program error NUMBER (public), or None
    units_consumed: int | None  # compute units — a public metric, like latency_ms
    slot: int | None  # the slot the sim ran against — powers the drift SERIES
    network: str  # NETWORKS: "fork" | "mainnet" | "devnet" | "other" (never a URL)
    source: str  # OutcomeSource — always "simulated" (the new tier)
    tenancy: str = "local"  # Tenancy — egress governance, fail-closed default


SIMULATED_ALLOWED_KEYS = frozenset(SimulatedOutcome.__dataclass_fields__)


def assert_simulated_allowlisted(mapping: Mapping[str, Any]) -> None:
    """Reject (fail closed) any key not on the ``SimulatedOutcome`` allowlist."""
    extra = set(mapping) - SIMULATED_ALLOWED_KEYS
    if extra:
        raise CorpusError(
            f"non-allowlisted simulated key(s) would be persisted: {sorted(extra)}"
        )


def to_simulated_record(outcome: SimulatedOutcome) -> dict[str, Any]:
    """Serialize a ``SimulatedOutcome``, enforcing the allowlist AND the closed-set axes
    before it can be written — the same fail-closed discipline ``to_adversarial_record``
    applies. Raises ``CorpusError`` on any off-set categorical value or non-allowlisted
    key, so an off-vocabulary string is a build break, not a silent smuggle."""
    record_dict = asdict(outcome)
    assert_simulated_allowlisted(record_dict)
    if outcome.status not in SIM_STATUSES:
        raise CorpusError(f"status {outcome.status!r} not in the closed set")
    if outcome.revert_class not in REVERT_CLASSES:
        raise CorpusError(
            f"revert_class {outcome.revert_class!r} not in the closed set"
        )
    if outcome.network not in NETWORKS:
        raise CorpusError(f"network {outcome.network!r} not in the closed set")
    if outcome.source not in OUTCOME_SOURCES:
        raise CorpusError(f"source {outcome.source!r} not in the closed set")
    if outcome.tenancy not in TENANCIES:
        raise CorpusError(f"tenancy {outcome.tenancy!r} not in the closed set")
    if outcome.error_code is not None and not isinstance(outcome.error_code, int):
        raise CorpusError("error_code must be an int (a public code) or None")
    return record_dict


def simulated_sibling(path: str | Path) -> Path:
    """The segregated file for ``simulated`` outcomes, co-located with the corpus
    (``<dir>/simulated.jsonl``).

    Segregation is by PATH (not an in-band tag), the same posture as ``synthetic_sibling``:
    a reader of the main corpus that doesn't know about the simulated tier then NEVER sees
    a simulated row — it FAILS CLOSED. Routing HERE, at the single write boundary, means no
    call site can forget to segregate (and a self-simulated ``pass`` can never leak into the
    published, wire-only first-call-correct metric)."""
    return Path(path).with_name("simulated.jsonl")


# --- recipe_hash input guard: values-free by CONSTRUCTION, not convention --------------
# The defi-security finding on the D2 tier: recipe_hash sha256's caller-supplied names and
# recipe descriptors, and a hash over low-cardinality secret-adjacent inputs is a
# dictionary-attack surface — so a resolved pubkey/amount must be REJECTED at the boundary,
# not merely documented away. Mirrors ``_leak_sink_is_valid`` (the same file's precedent for
# guarding the one record field derived from request data). All guards fail CLOSED, and the
# error messages never echo the offending input (redaction — an exception message is a log
# line waiting to happen).

# A plan NAME is a short identifier ("user", "bonding_curve"), never a value. 64 chars is
# generous for any IDL/source identifier while staying far below secret/address lengths.
_MAX_PLAN_NAME = 64

# The base58 alphabet at pubkey length (32–44 chars covers every 32-byte Solana address).
# ``looks_like_secret_value`` only catches base58 at SECRET-KEY length (~88), so a public
# address passed as a "name" needs its own shape check — an address is exactly the
# low-cardinality-guessable input the audit flagged.
_BASE58_ADDRESS_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

# The CLOSED vocabulary of seed-KIND descriptors a recipe may contain — built from the
# pda.py node model (the single source of truth for seed shapes: SeedEncoding ×
# SeedSource, plus the ordered-pair and resolver nodes). A resolved address is not in this
# set, so it is structurally impossible to smuggle one in as a "kind".
SEED_KIND_TOKENS: frozenset[str] = frozenset(
    {f"const:{encoding}" for encoding in get_args(SeedEncoding)}
    | {
        f"{source}:{encoding}"
        for source in get_args(SeedSource)
        for encoding in get_args(SeedEncoding)
    }
    | {"ordered_pair:min", "ordered_pair:max", "resolver"}
)


def seed_kind_token(seed: PdaSeed) -> str:
    """The closed kind token for one PDA seed node — derived MECHANICALLY from the
    pda.py node model (the SSOT for seed shapes), never free text.

    ``const:<encoding>`` / ``<source>:<encoding>`` / ``ordered_pair:<select>`` /
    ``resolver`` — exactly the vocabulary ``SEED_KIND_TOKENS`` is built from, so the
    output is guaranteed to pass ``_guard_seed_recipes``. An unknown node type fails
    CLOSED (a new seed shape must be added to the vocabulary deliberately, in one
    place, before it can be fingerprinted)."""
    if isinstance(seed, ConstantPdaSeedNode):
        return f"const:{seed.encoding}"
    if isinstance(seed, VariablePdaSeedNode):
        return f"{seed.source}:{seed.encoding}"
    if isinstance(seed, OrderedPairPdaSeedNode):
        return f"ordered_pair:{seed.select}"
    if isinstance(seed, ResolverPdaSeedNode):
        return "resolver"
    raise CorpusError(
        f"unknown PDA seed node type {type(seed).__name__} — "
        "add its kind token to SEED_KIND_TOKENS before fingerprinting it"
    )


def seed_recipes_of(pdas: Mapping[str, PdaNode]) -> dict[str, list[str]]:
    """Project a program's PDA graph into the values-free ``seed_recipes`` shape
    ``recipe_hash`` takes: account NAME → ordered seed-KIND tokens.

    The shared helper for the landing orchestrators (pump + Meteora derive their
    fingerprint input from the SAME packaged ``ProgramSpec.pdas`` graph — no per-provider
    duplication). Only kinds enter — a resolved pubkey/amount is not readable from a
    :class:`~gecko.pda.PdaNode`, and the ``recipe_hash`` guard re-checks every token
    against the closed vocabulary anyway (belt and suspenders)."""
    return {
        name: [seed_kind_token(seed) for seed in node.seeds]
        for name, node in pdas.items()
    }


def network_category(label: str | None) -> Network:
    """Collapse a free-text ``network_label`` (the Receipt's honesty caveat) into the
    CLOSED ``NETWORKS`` category — never the label itself (free text could carry a URL).

    Delegates to :func:`gecko.networks.network_from_label`, which owns the collapse and
    its specificity order (a fork label routinely mentions mainnet — "surfpool fork
    (mainnet-backed — NOT mainnet)" — so ``fork`` is decided before ``mainnet``). Fails
    CLOSED to ``unknown`` for anything unrecognized, including ``None``.

    CORPUS/DISPLAY ONLY. This reads PROSE. A signing gate must never take a network from
    it — a receipt's category being one bucket off is a slightly-wrong ledger row, while a
    signature on the wrong network is unrecoverable. The gate compares the structured
    ``Receipt.network`` instead, and ``gecko.txbind`` does not import this function."""
    return network_from_label(label)


def _guard_plan_name(entry: object, field: str) -> str:
    """Admit only a short identifier-shaped NAME; raise ``CorpusError`` otherwise.

    Fail-closed order: type/emptiness, length cap, secret shape, address shape. The
    message names the FIELD, never the value (redaction)."""
    if not isinstance(entry, str) or not entry:
        raise CorpusError(f"{field}: entry must be a non-empty string (redacted)")
    if len(entry) > _MAX_PLAN_NAME:
        raise CorpusError(
            f"{field}: entry exceeds {_MAX_PLAN_NAME} chars — "
            "not an identifier-shaped name (redacted)"
        )
    if looks_like_secret_value(entry):
        raise CorpusError(
            f"{field}: entry is secret-shaped — a value, not a name (redacted)"
        )
    if _BASE58_ADDRESS_RE.fullmatch(entry):
        raise CorpusError(
            f"{field}: entry is base58-address-shaped — "
            "a resolved pubkey, not a name (redacted)"
        )
    return entry


def _guard_seed_recipes(
    seed_recipes: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    """Validate + canonicalize the recipe mapping: keys are guarded names, values are
    ordered lists of tokens from the CLOSED ``SEED_KIND_TOKENS`` vocabulary. Seed order
    is derivation-significant, so value order is preserved (keys are sorted by the JSON
    serializer)."""
    if not isinstance(seed_recipes, Mapping):
        raise CorpusError(
            "seed_recipes must be a mapping of account name -> seed-kind tokens"
        )
    canonical: dict[str, list[str]] = {}
    for account_name, kinds in seed_recipes.items():
        _guard_plan_name(account_name, "seed_recipes key")
        # A bare string is Sequence[str] too — reject it before it iterates as chars.
        if isinstance(kinds, str) or not isinstance(kinds, (list, tuple)):
            raise CorpusError(
                f"seed_recipes[{account_name!r}] must be a list/tuple "
                "of seed-kind tokens"
            )
        for kind in kinds:
            if not isinstance(kind, str) or kind not in SEED_KIND_TOKENS:
                raise CorpusError(
                    f"seed_recipes[{account_name!r}]: seed kind not in the closed "
                    "SEED_KIND_TOKENS vocabulary (redacted)"
                )
        canonical[account_name] = list(kinds)
    return canonical


def recipe_hash(
    *,
    program_id: str,
    instruction: str,
    account_names: Iterable[str],
    arg_names: Iterable[str],
    seed_recipes: Mapping[str, Sequence[str]],
) -> str:
    """The values-free structural fingerprint of a call plan (§3).

    The drift question is *did the outcome of the SAME plan SHAPE change over time?* — so
    the hash fingerprints the plan STRUCTURE, never its filled values. Account/arg NAMES
    are sorted (order-independent), and ``seed_recipes`` is the PDA seed-recipe KINDS per
    account (from the ProgramSpec), not any derived pubkey. Two runs of the same intent
    against the same program → identical hash even though the resolved pubkeys differ per
    mint/user; a program upgrade that changes a PDA layout → a reverting sim on a STABLE
    hash (= drift), and a change in our RECOVERED recipe → a new hash.

    Values-free by CONSTRUCTION: every account/arg name must be a short
    identifier-shaped NAME (``_guard_plan_name`` — secret-shaped, base58-address-shaped,
    and over-long entries raise ``CorpusError``), and ``seed_recipes`` values must come
    from the closed ``SEED_KIND_TOKENS`` vocabulary. A resolved pubkey or amount cannot
    enter the fingerprint without tripping the guard (the same boundary discipline as
    ``outcome_from``). ``program_id`` is exempt: it is a public program address and is
    persisted plaintext on the record itself."""
    fingerprint = {
        "program_id": program_id,
        "instruction": instruction,
        # names, never pubkeys/values — each entry guarded before it can be hashed
        "accounts": sorted(_guard_plan_name(n, "account_names") for n in account_names),
        "args": sorted(_guard_plan_name(n, "arg_names") for n in arg_names),
        # seed-recipe KIND tokens from the closed vocabulary, never derived addresses
        "seed_recipes": _guard_seed_recipes(seed_recipes),
    }
    blob = json.dumps(fingerprint, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def simulated_outcome_from(
    receipt: Receipt,
    *,
    program_id: str,
    instruction: str,
    recipe_hash: str,
    slot: int | None,
    network: str,
    ts: int,
    surface_id: str,
) -> SimulatedOutcome:
    """Build a control-plane-safe ``SimulatedOutcome`` from a :class:`Receipt`'s
    CATEGORICAL fields plus the plan's values-free structural fingerprint.

    NOTE what it reads off the Receipt: ONLY ``status`` (the land/no-land verdict),
    ``revert_class`` (split by ``revert_family`` into a closed family + a public error
    NUMBER), and ``units_consumed`` (a public compute metric, like ``latency_ms``). It
    NEVER reads ``sol_delta``/``tokens_received``/``logs_tail``/``err`` — those are per-user
    state / secret-shaped and are excluded from the corpus by construction (they are not
    parameters and are not touched here). ``source`` is fixed to ``"simulated"`` — a caller
    cannot mislabel it — and ``tenancy`` is DERIVED from local operator consent
    (``consented_tenancy``), never accepted, exactly like ``outcome_from``'s. The
    closed-set gate runs at ``to_simulated_record``."""
    family, error_code = revert_family(receipt.revert_class)
    return SimulatedOutcome(
        ts=ts,
        surface_id=surface_id,
        program_id=program_id,
        instruction=instruction,
        recipe_hash=recipe_hash,
        status=receipt.status,
        revert_class=family,
        error_code=error_code,
        units_consumed=receipt.units_consumed,
        slot=slot,
        network=network,
        source="simulated",
        tenancy=consented_tenancy(),
    )


def simulated_outcome_from_record(record: Mapping[str, Any]) -> SimulatedOutcome:
    """Rehydrate a persisted ``simulated.jsonl`` row back into a ``SimulatedOutcome`` —
    the read side of ``to_simulated_record`` and the seam ``gecko drift`` reads through.

    Fails CLOSED like ``outcome_from_record``: a non-allowlisted key (the shape a leaked
    pubkey/log would take) is rejected before construction, and a truncated record is a
    hard error, never silently defaulted. The closed-set axes are re-validated via
    ``to_simulated_record`` so a hand-edited off-vocabulary row cannot enter a drift
    series, and ``tenancy`` gets the same re-derivation ``outcome_from_record`` applies:
    a label wider than this machine's own consent is refused, not accepted on the row's
    say-so."""
    assert_simulated_allowlisted(record)
    missing = SIMULATED_ALLOWED_KEYS - set(record)
    if missing:
        raise CorpusError(
            f"simulated record missing required key(s): {sorted(missing)}"
        )
    if not tenancy_admissible(record["tenancy"]):
        raise CorpusError(
            "simulated record tenancy is not a closed-set member, or is wider than "
            "this machine's derived consent — refused"
        )
    values = {key: record[key] for key in SIMULATED_ALLOWED_KEYS}
    # A row written before the vocabulary was unified spells the fail-closed bucket
    # ``other``; it means exactly what ``unknown`` means now. Translating it on READ keeps
    # already-persisted history readable — a rename that orphans rows is a data loss, not
    # a rename. Only the KNOWN legacy spellings translate: anything else (a hand-edited
    # ``mars``) falls through to the closed-set gate below and still raises.
    raw_network = values["network"]
    if isinstance(raw_network, str) and raw_network in LEGACY_NETWORK_ALIASES:
        values["network"] = LEGACY_NETWORK_ALIASES[raw_network]
    outcome = SimulatedOutcome(**values)
    to_simulated_record(outcome)  # closed-set gate; raises CorpusError on bad axes
    return outcome


def record_simulated(outcome: SimulatedOutcome, path: str | Path) -> None:
    """Append one allowlisted simulated JSONL record to the segregated ``simulated.jsonl``
    sibling. Best-effort like ``record``/``record_adversarial``: a corpus write must never
    break the caller, so non-violation failures are swallowed with a redacted note; a
    control-plane violation (``CorpusError``) still surfaces."""
    try:
        record_dict = to_simulated_record(outcome)
        target = simulated_sibling(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_dict) + "\n")
    except CorpusError:
        raise  # a control-plane violation must surface, not be swallowed
    except Exception:  # noqa: BLE001 - best-effort; never break the caller
        import logging

        logging.getLogger("gecko.corpus").warning(
            "simulated corpus write failed (redacted)"
        )

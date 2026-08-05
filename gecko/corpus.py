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
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from .caller import CallError
from .pda import SeedEncoding, SeedSource
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
# It is a one-way governance promise; the egress/consent layer is deliberately NOT built
# here — only the field + a fail-closed ``local`` default, so the axis exists before any
# record could ever egress (retrofitting it after the fact is the failure mode).
Tenancy = Literal["local", "contributed"]
TENANCIES: frozenset[str] = frozenset(get_args(Tenancy))

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
    tenancy: str = "local",
) -> CallOutcome:
    """Build a control-plane-safe ``CallOutcome``.

    NOTE the signature: it takes ``status: int | None``, NOT the result dict — the
    response body and filled URL physically cannot enter this function. ``args`` is
    read for NAMES and TYPES only (``params_present`` / ``arg_shape``); values are
    never copied out.

    ``source`` is NOT a parameter: it is DERIVED from ``mode`` (see ``source_for_mode``)
    so a caller cannot mislabel a faked recorded ``200`` as ``observed``. ``tenancy``
    defaults to ``local`` and is validated against the closed set (the egress layer is
    not built here — only the guarded field).
    """
    if error_class not in ERROR_CLASSES:
        raise CorpusError(f"error_class {error_class!r} not in the closed set")
    if tenancy not in TENANCIES:
        raise CorpusError(f"tenancy {tenancy!r} not in the closed set")
    source = source_for_mode(mode)
    # Belt-and-suspenders: the derivation is total, so this only trips if _MODE_TO_SOURCE
    # ever drifts off the closed set — a build break, not a silent smuggle.
    if source not in OUTCOME_SOURCES:
        raise CorpusError(f"source {source!r} not in the closed set")
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
    hard error — a truncated record is never silently defaulted."""
    assert_allowlisted(record)
    missing = ALLOWED_KEYS - set(record)
    if missing:
        raise CorpusError(f"record missing required key(s): {sorted(missing)}")
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
# carry an API key). ``fork`` is a mainnet-backed snapshot; ``other`` is the fail-closed bucket.
NETWORKS: frozenset[str] = frozenset({"fork", "mainnet", "devnet", "other"})


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
    tenancy: str = "local",
) -> SimulatedOutcome:
    """Build a control-plane-safe ``SimulatedOutcome`` from a :class:`Receipt`'s
    CATEGORICAL fields plus the plan's values-free structural fingerprint.

    NOTE what it reads off the Receipt: ONLY ``status`` (the land/no-land verdict),
    ``revert_class`` (split by ``revert_family`` into a closed family + a public error
    NUMBER), and ``units_consumed`` (a public compute metric, like ``latency_ms``). It
    NEVER reads ``sol_delta``/``tokens_received``/``logs_tail``/``err`` — those are per-user
    state / secret-shaped and are excluded from the corpus by construction (they are not
    parameters and are not touched here). ``source`` is fixed to ``"simulated"`` — a caller
    cannot mislabel it. The closed-set gate runs at ``to_simulated_record``."""
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
        tenancy=tenancy,
    )


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

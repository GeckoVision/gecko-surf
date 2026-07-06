"""Semantic risk-score — the comprehension-native security signal.

This is the component the whole "semantic security gateway" thesis hinges on. Because
Gecko COMPREHENDS the API, it can score a call's risk *semantically* — malformed for
THIS API, wrong/anomalous op, credential-exfil host, poisoned metadata — signals a
signature-based scanner (which doesn't know the API's shape) or a DeFi-trade firewall
(which only knows spend/slippage) structurally cannot compute. The score is a composite
0-100 + a human-readable reason per signal + an allow/step-up/block decision.

Pure and offline: ``score_call`` takes plain inputs (the comprehended schema, args,
method, surface state, trusted hosts) so it's unit-testable; ``assess_from_client`` is a
thin adapter that pulls those inputs from a live ``AgentApiClient``. Policy is
auto-derived from the comprehension (``policy_from_client``) — the operator only tunes
thresholds; they never hand-write rules.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from .sanitize import looks_like_secret_value

logger = logging.getLogger("gecko.risk")

Decision = Literal["allow", "step_up", "block"]

# Instruction-shaped markers — hidden prompts in tool metadata/args (tool poisoning).
# These are deliberately NARROW: an over-broad natural-language phrase (e.g. "you must")
# would false-positive on a legit provider's prose and BLOCK a paying call — worse than a
# demo miss. Keep only clearly injection-shaped markers.
_INJECTION_MARKERS: tuple[str, ...] = (
    "ignore previous",
    "ignore all previous",
    "disregard previous",
    "disregard all",
    "forget everything",
    "new instructions",
    "new instruction",
    "system:",
    "<system",
    "send the api key",
    "send your api key",
    "exfiltrate",
)

#: Signals whose mere presence BLOCKS, independently of the additive composite score.
#: A lone exfil host, an injection marker, or a quarantined surface must never silently
#: degrade to step_up when an operator raises ``block_at`` above the signal's weight.
BLOCKING_SIGNALS: frozenset[str] = frozenset(
    {"exfil.host", "poison.injection", "provenance.quarantined"}
)

#: A declared field "legitimately accepts a URL" if its schema says so (``format: uri``)
#: or its NAME says so (contains "url"/"uri" — webhook_url, redirect_uri, image_url). Such
#: a field holding a URL to any host is NOT exfil — it is the field doing its job.
_URI_FORMATS: frozenset[str] = frozenset(
    {"uri", "url", "uri-reference", "iri", "iri-reference"}
)


@dataclass(frozen=True)
class Reason:
    """One triggered risk signal — carries the points it added and a human sentence."""

    signal: str
    points: int
    message: str


@dataclass(frozen=True)
class RiskAssessment:
    score: int  # 0-100 composite
    decision: Decision
    reasons: list[Reason] = field(default_factory=list)


@dataclass(frozen=True)
class RiskPolicy:
    """Auto-derived from the comprehended surface; the operator only tunes thresholds."""

    allowed_tools: frozenset[str] = frozenset()
    trusted_hosts: frozenset[str] = frozenset()
    high_risk_ops: frozenset[str] = frozenset()
    step_up_at: int = 30
    block_at: int = 60


def _type_ok(value: Any, json_type: str) -> bool:
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    py = {"string": str, "boolean": bool, "array": list, "object": dict}.get(json_type)
    return py is None or isinstance(value, py)


def _schema_conformance(schema: dict[str, Any], args: dict[str, Any]) -> list[Reason]:
    """Comprehension-native: does the call match the API's OWN schema? (the money shot)."""
    out: list[Reason] = []
    for req in schema.get("required") or []:
        if req not in args:
            out.append(
                Reason(
                    "schema.required",
                    35,
                    f"missing required field '{req}' — the call is malformed for this "
                    "API and would fail or do the wrong thing",
                )
            )
    props = schema.get("properties") or {}
    if props:  # only judge shape when the surface actually declares one
        for key, val in args.items():
            spec = props.get(key)
            if spec is None:
                out.append(
                    Reason(
                        "schema.unknown_field",
                        10,
                        f"unknown field '{key}' not in the API's schema — possible "
                        "injection or wrong tool",
                    )
                )
                continue
            jtype = spec.get("type")
            if jtype and not _type_ok(val, jtype):
                out.append(
                    Reason(
                        "schema.type",
                        20,
                        f"field '{key}' has the wrong type (expected {jtype})",
                    )
                )
            enum = spec.get("enum")
            if enum and val not in enum:
                out.append(
                    Reason(
                        "schema.enum",
                        20,
                        f"field '{key}'='{val}' is not an allowed value",
                    )
                )
    return out


def _poisoning(description: str, args: dict[str, Any]) -> list[Reason]:
    out: list[Reason] = []
    haystacks: list[tuple[str, str]] = [("the tool description", description or "")]
    haystacks += [(f"arg '{k}'", v) for k, v in args.items() if isinstance(v, str)]
    for where, text in haystacks:
        low = text.lower()
        if any(m in low for m in _INJECTION_MARKERS):
            out.append(
                Reason(
                    "poison.injection",
                    60,
                    f"instruction-shaped content in {where} — likely tool-poisoning / "
                    "prompt injection",
                )
            )
    for key, val in args.items():
        if isinstance(val, str) and looks_like_secret_value(val):
            out.append(
                Reason(
                    "poison.secret", 45, f"arg '{key}' contains a secret-shaped value"
                )
            )
    return out


def _field_accepts_uri(name: str, spec: dict[str, Any]) -> bool:
    """A DECLARED field legitimately holds a URL when its schema declares a URI format or
    its name is URL-shaped — so a webhook_url / redirect_uri to any host is NOT exfil."""
    if str(spec.get("format", "")).lower() in _URI_FORMATS:
        return True
    low = name.lower()
    return "url" in low or "uri" in low


def _host_of(value: str) -> str | None:
    """Parse the host from a URL, returning ``None`` on a malformed value (e.g.
    ``proto://[::1``). A malformed arg degrades to 'no host' rather than crashing the whole
    exfil signal — one junk arg must not blind exfil detection for its siblings."""
    try:
        return urlparse(value).netloc.split("@")[-1].split(":")[0].lower() or None
    except ValueError:
        return None


def _exfil_host(
    args: dict[str, Any],
    trusted_hosts: frozenset[str],
    schema: dict[str, Any],
) -> list[Reason]:
    if not trusted_hosts:
        return []  # can't judge exfil without a trusted set (unpinned surface)
    props = schema.get("properties") or {}
    out: list[Reason] = []
    for key, val in args.items():
        if not (isinstance(val, str) and "://" in val):
            continue
        spec = props.get(key)
        # Schema-aware: a DECLARED field that legitimately accepts a URI is doing its job
        # (Pegana has ~93 url-ish args). Only an UNKNOWN field, or a declared field whose
        # schema does NOT accept a URI, is a candidate for exfil.
        if isinstance(spec, dict) and _field_accepts_uri(key, spec):
            continue
        host = _host_of(val)
        if host and host not in trusted_hosts:
            out.append(
                Reason(
                    "exfil.host",
                    60,
                    f"arg '{key}' routes to host '{host}' not in this API's trusted "
                    "set — credential/data exfiltration risk",
                )
            )
    return out


def _op_risk(method: str) -> list[Reason]:
    m = (method or "get").lower()
    if m == "delete":
        return [Reason("op.destructive", 30, "destructive operation (DELETE)")]
    if m in ("post", "put", "patch"):
        return [Reason("op.write", 15, f"write operation ({m.upper()})")]
    return []


def _provenance(state: str) -> list[Reason]:
    if state == "quarantined":
        return [
            Reason(
                "provenance.quarantined",
                45,
                "surface is quarantined (from-docs / possibly poisoned) — treat as untrusted",
            )
        ]
    if state == "unverified":
        return [
            Reason(
                "provenance.unverified",
                20,
                "surface is unverified (not pinned to a trusted origin)",
            )
        ]
    return []


def _scope(tool_name: str, allowed: bool, policy: RiskPolicy | None) -> list[Reason]:
    denied = not allowed
    if (
        policy is not None
        and policy.allowed_tools
        and tool_name not in policy.allowed_tools
    ):
        denied = True
    if denied:
        return [
            Reason(
                "scope.not_allowed",
                45,
                f"operation '{tool_name}' is not in the allowed set for this agent — "
                "intent/scope mismatch",
            )
        ]
    return []


def score_call(
    *,
    tool_name: str,
    tool_schema: dict[str, Any] | None = None,
    args: dict[str, Any] | None = None,
    method: str = "get",
    surface_state: str = "pinned",
    trusted_hosts: frozenset[str] = frozenset(),
    tool_description: str = "",
    allowed: bool = True,
    policy: RiskPolicy | None = None,
) -> RiskAssessment:
    """Score one agent tool-call. Comprehension-native signals dominate the weighting.

    Two guarantees the enforcement gate leans on:

    * **Per-signal crash containment.** Each signal runs isolated; a crashing signal
      degrades ITSELF only (its reasons are dropped) and can never abort the whole
      assessment. This closes the fail-open bypass: a junk arg that crashes one signal
      must not let a call slip past a DIFFERENT signal that would have blocked.
    * **Categorical blocking.** A ``BLOCKING_SIGNALS`` hit (exfil host / injection /
      quarantined) blocks regardless of the additive composite, so raising ``block_at``
      above a signal's weight cannot silently downgrade it to step_up.
    """
    a = args or {}
    schema = tool_schema or {}
    hosts = trusted_hosts or (policy.trusted_hosts if policy else frozenset())
    reasons: list[Reason] = []
    reasons += _run_signal("schema", _schema_conformance, schema, a)
    reasons += _run_signal("poison", _poisoning, tool_description, a)
    reasons += _run_signal("exfil", _exfil_host, a, hosts, schema)
    reasons += _run_signal("op", _op_risk, method)
    reasons += _run_signal("provenance", _provenance, surface_state)
    reasons += _run_signal("scope", _scope, tool_name, allowed, policy)

    score = min(100, sum(r.points for r in reasons))
    step_up = policy.step_up_at if policy else 30
    block = policy.block_at if policy else 60
    categorical = any(r.signal in BLOCKING_SIGNALS for r in reasons)
    decision: Decision = (
        "block"
        if categorical or score >= block
        else "step_up"
        if score >= step_up
        else "allow"
    )
    return RiskAssessment(score=score, decision=decision, reasons=reasons)


def _run_signal(name: str, fn: Callable[..., list[Reason]], *args: Any) -> list[Reason]:
    """Run one scoring signal with crash containment. A signal that raises degrades to
    NO reasons for that signal only — it never propagates, so it can neither abort the
    assessment nor mask a sibling signal's block."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - degrade THIS signal only; never fail the assessment
        logger.warning("risk signal %r crashed; dropping it (degraded)", name)
        return []


# --------------------------------------------------------------------------- #
# Thin adapter: derive the inputs (and an auto-policy) from a live client.
# --------------------------------------------------------------------------- #
def policy_from_client(client: Any) -> RiskPolicy:
    """Auto-derive the policy from the comprehension — the 'fast to configure' edge."""
    allowed = frozenset(t["name"] for t in client.list_tools())
    anchor = getattr(client, "anchor", None)
    hosts = frozenset(getattr(anchor, "trusted_hosts", None) or frozenset())
    return RiskPolicy(allowed_tools=allowed, trusted_hosts=hosts)


def assess_from_client(
    client: Any,
    tool_name: str,
    args: dict[str, Any],
    *,
    policy: RiskPolicy | None = None,
) -> RiskAssessment:
    """Score a call against a live ``AgentApiClient`` — pulls schema/method/state/hosts."""
    pol = policy or policy_from_client(client)
    tool = next((t for t in client.list_tools() if t["name"] == tool_name), None)
    schema = (tool or {}).get("inputSchema", {})
    desc = (tool or {}).get("description", "")
    method = "get"
    for op in getattr(client, "operations", []):
        if getattr(op, "operation_id", None) and _op_tool_name(op) == tool_name:
            method = getattr(op, "method", "get")
            break
    state = getattr(getattr(client, "anchor", None), "state", "unverified")
    return score_call(
        tool_name=tool_name,
        tool_schema=schema,
        args=args,
        method=method,
        surface_state=state,
        trusted_hosts=pol.trusted_hosts,
        tool_description=desc,
        allowed=tool_name in pol.allowed_tools,
        policy=pol,
    )


def _op_tool_name(op: Any) -> str:
    from .tools import tool_name

    return tool_name(op)

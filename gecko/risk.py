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
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlparse

from .catalog import _token_list  # the shared identifier tokenizer (do not re-invent)
from .policy import AgentPolicy
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


# --------------------------------------------------------------------------- #
# Semantic tier (L0->L2) — comprehension-derived, NOT the HTTP verb. A deterministic
# weighted vote over money-verbs in the path/operationId (Feature A) + an amount∧recipient
# arg-shape co-occurrence in the request body/params (Feature B, the sharp one) + a weak
# security-scope corroboration (Feature C). Pure/offline (invariant #2: data(spec) only).
# Tier feeds `score_call` as a Reason; it is NEVER a member of BLOCKING_SIGNALS — a block on
# a transfer needs the intersection with an AgentPolicy predicate (cap / recipient).
# --------------------------------------------------------------------------- #
Tier = Literal["read", "write", "transfer"]
TierConfidence = Literal["high", "low"]

#: Mirror of ``enforce.WRITE_METHODS`` (kept local to avoid a risk<->enforce import cycle;
#: enforce imports ``RiskAssessment`` from here). Single semantic source stays enforce.
_WRITE_METHODS: frozenset[str] = frozenset({"post", "put", "patch", "delete"})

#: Feature A — the DELIBERATELY-NARROW money-verb lexicon (over-broad verbs false-positive
#: and would block a paying call, like ``_INJECTION_MARKERS``). Matched as WHOLE tokens on
#: the camelCase/snake-split path+operationId; ``order`` is excluded (sort/purchase noise).
MONEY_VERBS: frozenset[str] = frozenset(
    {
        "transfer",
        "transfers",
        "send",
        "withdraw",
        "withdrawal",
        "withdrawals",
        "payout",
        "payouts",
        "disburse",
        "remit",
        "wire",
        "settle",
        "settlement",
        "charge",
        "capture",
        "refund",
        "redeem",
        "spend",
        "debit",
        "checkout",
        "purchase",
        "pay",
        "swap",
        "mint",
        "burn",
    }
)

#: A quote/estimate commits NO value — it is a read-shaped projection of a transfer. Same
#: discipline as the "GET listing does not lift tier" and "order excluded" guards in the
#: lexicon design: these whole tokens cap the tier at ``write`` (never ``transfer``), so a
#: `transferQuote`/`swapQuote`/`purchase/quote` op cannot false-positive a spend.
_QUOTE_MARKERS: frozenset[str] = frozenset(
    {"quote", "quotes", "quotation", "estimate", "preview", "simulate", "simulation"}
)

#: Feature B — amount-shaped field names. ``amount``/``price`` confirm as WHOLE tokens
#: regardless of type/required (real specs carry decimal-string amounts); the rest confirm
#: only as a required numeric (whole-token, type number/integer, required=true).
_AMOUNT_NAMES: frozenset[str] = frozenset(
    {"amount", "value", "quantity", "qty", "price", "total", "sum", "cost", "fee"}
)
_STRONG_AMOUNT: frozenset[str] = frozenset({"amount", "price"})

#: Feature B — recipient/destination-shaped field names. ``to`` counts ONLY as an exact
#: field name (never a token, or "auto"/"total" would trip it — the tokenizer splits those).
_RECIPIENT_NAMES: frozenset[str] = frozenset(
    {
        "recipient",
        "destination",
        "dest",
        "payee",
        "beneficiary",
        "address",
        "wallet",
        "account",
        "iban",
        "counterparty",
        "receiver",
        "to",
    }
)

#: Feature C — a security scope naming a payment/write capability weakly corroborates.
_PAYMENT_SCOPE_TOKENS: frozenset[str] = frozenset(
    {
        "payments",
        "payment",
        "transfer",
        "transfers",
        "withdraw",
        "withdrawals",
        "payout",
        "payouts",
        "disburse",
        "spend",
    }
)

#: Amount-arg names the GOVERNANCE cap predicate extracts from the actual call args (kept
#: tighter than ``_AMOUNT_NAMES``: the clearly-monetary tokens, so a benign ``value``/``qty``
#: does not trip a spend-cap warning).
_CAP_AMOUNT_TOKENS: frozenset[str] = frozenset(
    {"amount", "price", "total", "cost", "fee"}
)


@dataclass(frozen=True)
class TierResult:
    """The comprehension-derived operation tier + how confident the vote was. ``high`` = a
    confirmed transfer (Feature B, or a money-verb + one corroborating half-shape); ``low`` =
    a lone money-verb with no confirming shape (a candidate that never hard-blocks)."""

    tier: Tier
    confidence: TierConfidence


def _is_write_method(method: str) -> bool:
    return (method or "get").lower() in _WRITE_METHODS


def _request_schema(request_body: Any) -> dict[str, Any]:
    """The JSON body schema out of an OpenAPI ``requestBody`` (application/json first)."""
    if not isinstance(request_body, dict):
        return {}
    content = request_body.get("content") or {}
    media = content.get("application/json") or next(iter(content.values()), None)
    schema = media.get("schema") if isinstance(media, dict) else None
    return schema if isinstance(schema, dict) else {}


def _collect_props(
    schema: dict[str, Any], out: list[tuple[str, Any, bool]], depth: int
) -> None:
    """Recursively gather ``(field_name, json_type, required)`` from a body schema, walking
    nested objects and anyOf/oneOf/allOf/items. Depth-bounded (cycle/blowup guard)."""
    if depth > 6 or not isinstance(schema, dict):
        return
    required = set(schema.get("required") or [])
    for name, spec in (schema.get("properties") or {}).items():
        if isinstance(spec, dict):
            out.append((name, spec.get("type"), name in required))
            _collect_props(spec, out, depth + 1)
        else:
            out.append((name, None, name in required))
    for combiner in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(combiner) or []:
            _collect_props(sub, out, depth + 1)
    items = schema.get("items")
    if isinstance(items, dict):
        _collect_props(items, out, depth + 1)


def _is_amount_shaped(name: str, jtype: Any, required: bool) -> bool:
    tokens = set(_token_list(name))
    if tokens & _STRONG_AMOUNT:  # amount/price: whole token, any type
        return True
    return bool(tokens & _AMOUNT_NAMES) and jtype in ("number", "integer") and required


def _is_recipient_shaped(name: str) -> bool:
    if name == "to":  # exact field name only
        return True
    return bool(set(_token_list(name)) & (_RECIPIENT_NAMES - {"to"}))


def _body_shapes(request_body: Any, parameters: Any) -> tuple[bool, bool]:
    """Feature B: does the body/params carry an amount-shaped AND/OR a recipient-shaped
    field? Returns ``(amount_shaped, recipient_shaped)``."""
    props: list[tuple[str, Any, bool]] = []
    _collect_props(_request_schema(request_body), props, 0)
    for p in parameters or []:
        pschema = getattr(p, "schema", None)
        ptype = pschema.get("type") if isinstance(pschema, dict) else None
        props.append(
            (getattr(p, "name", ""), ptype, bool(getattr(p, "required", False)))
        )
    amount = any(_is_amount_shaped(n, t, r) for n, t, r in props)
    recipient = any(_is_recipient_shaped(n) for n, _t, _r in props)
    return amount, recipient


def _payment_scope(security: Any) -> bool:
    """Feature C: any OAuth/security scope naming a payment/write capability."""
    if not security:
        return False
    tokens: set[str] = set()
    for entry in security:
        if isinstance(entry, dict):
            for scopes in entry.values():
                for scope in scopes or []:
                    tokens |= set(_token_list(str(scope)))
    return bool(tokens & _PAYMENT_SCOPE_TOKENS)


def classify_tier(
    *,
    method: str,
    path: str = "",
    operation_id: str = "",
    request_body: Any = None,
    parameters: Any = None,
    security: Any = None,
) -> TierResult:
    """The pure tier vote. Read unless state-changing; then Feature B (amount∧recipient)
    or a money-verb + one half-shape confirms ``transfer`` (high); a lone money-verb is a
    ``transfer`` candidate (low); everything else state-changing is ``write``."""
    if not _is_write_method(method):
        return TierResult("read", "high")
    tokens = set(_token_list(path)) | set(_token_list(operation_id))
    if tokens & _QUOTE_MARKERS:  # a quote/estimate never moves value
        return TierResult("write", "high")
    money_verb = bool(tokens & MONEY_VERBS)
    amount, recipient = _body_shapes(request_body, parameters)
    if amount and recipient:
        return TierResult("transfer", "high")
    if money_verb and (amount or recipient or _payment_scope(security)):
        return TierResult("transfer", "high")
    if money_verb:
        return TierResult("transfer", "low")
    return TierResult("write", "high")


def classify_operation(op: Any) -> TierResult:
    """Convenience adapter: classify a normalized ``ingest.Operation``."""
    return classify_tier(
        method=getattr(op, "method", "get"),
        path=getattr(op, "path", ""),
        operation_id=getattr(op, "operation_id", ""),
        request_body=getattr(op, "request_body", None),
        parameters=getattr(op, "parameters", None),
        security=getattr(op, "security", None),
    )


def _op_risk_tiered(tier: TierResult, method: str) -> list[Reason]:
    """Tier-aware sibling of ``_op_risk``: a comprehension-derived ``transfer`` emits
    ``op.transfer`` (25) / ``op.transfer_maybe`` (12); anything else falls back to the flat
    verb weighting (write/destructive). ADDITIVE, never categorical — ``transfer`` is not in
    BLOCKING_SIGNALS, so 25 alone stays below ``block_at`` (tier never blocks on its own)."""
    if tier.tier == "transfer":
        if tier.confidence == "high":
            return [
                Reason(
                    "op.transfer",
                    25,
                    "transfer/spend operation — moves value or issues an irreversible "
                    "external effect (comprehension-derived)",
                )
            ]
        return [
            Reason(
                "op.transfer_maybe",
                12,
                "possible transfer/spend operation (low confidence — a lone money-verb)",
            )
        ]
    return _op_risk(method)


# --------------------------------------------------------------------------- #
# Governance predicates (AgentPolicy) — the OTHER half of the block. Each fires on its own
# condition (an over-cap amount / an off-allowlist recipient in the actual args). The weight
# is SCALED BY TIER so the block is intersection-only: full weight (35) ONLY when the op is a
# comprehension-derived ``transfer`` — transfer(25) + predicate(35) = 60 = block_at. Off a
# transfer the predicate is a mild step_up weight (15), so a predicate alone, two predicates,
# or a predicate on a benign metered write can NEVER reach block_at (15 + 15 + 15 = 45 < 60).
# A ``transfer``/low (12) + predicate(35) = 47 also stays below block — only a CONFIRMED
# transfer hard-blocks. Validated by the governance falsifier, NOT by joining BLOCKING_SIGNALS.
# --------------------------------------------------------------------------- #
GOVERNANCE_POINTS_TRANSFER = 35
GOVERNANCE_POINTS_OTHER = 15


def _governance_points(tier: TierResult | None) -> int:
    """Full weight only at the transfer intersection; a mild step_up weight otherwise."""
    if tier is not None and tier.tier == "transfer":
        return GOVERNANCE_POINTS_TRANSFER
    return GOVERNANCE_POINTS_OTHER


def _parse_amount(value: Any) -> Decimal | None:
    """Coerce a call-arg value to a Decimal, or ``None`` if it cannot be parsed. The cap
    predicate FAILS SAFE on ``None`` (cannot assert over-cap -> step_up via tier, not block)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, str):
        cleaned = value.strip().lstrip("$€£").replace(",", "")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _walk_args(args: Any, depth: int = 0) -> list[tuple[str, Any]]:
    """Flatten ``(key, value)`` pairs from nested call args (dicts/lists), depth-bounded."""
    out: list[tuple[str, Any]] = []
    if depth > 8:
        return out
    if isinstance(args, dict):
        for key, val in args.items():
            out.append((str(key), val))
            out.extend(_walk_args(val, depth + 1))
    elif isinstance(args, list):
        for item in args:
            out.extend(_walk_args(item, depth + 1))
    return out


def _extract_amount(args: dict[str, Any]) -> Decimal | None:
    """The largest parseable amount-shaped arg value (most conservative for a cap check)."""
    amounts: list[Decimal] = []
    for key, val in _walk_args(args):
        if set(_token_list(key)) & _CAP_AMOUNT_TOKENS:
            parsed = _parse_amount(val)
            if parsed is not None:
                amounts.append(parsed)
    return max(amounts) if amounts else None


def _extract_recipients(args: dict[str, Any]) -> list[str]:
    """String values under recipient-shaped arg keys (at any nesting depth)."""
    out: list[str] = []
    for key, val in _walk_args(args):
        if isinstance(val, str) and _is_recipient_shaped(key):
            out.append(val)
    return out


def _cap_signal(
    args: dict[str, Any],
    agent_policy: AgentPolicy | None,
    method: str,
    tier: TierResult | None,
) -> list[Reason]:
    if agent_policy is None or agent_policy.spend_cap is None:
        return []
    if not _is_write_method(method):
        return []  # a read moves no value
    amount = _extract_amount(args)
    if amount is None:  # fail SAFE: unparseable/absent amount cannot assert over-cap
        return []
    if amount > agent_policy.spend_cap:
        return [
            Reason(
                "cap.exceeded",
                _governance_points(tier),
                f"amount {amount} exceeds the operator spend cap "
                f"{agent_policy.spend_cap}",
            )
        ]
    return []


def _recipient_signal(
    args: dict[str, Any],
    agent_policy: AgentPolicy | None,
    method: str,
    tier: TierResult | None,
) -> list[Reason]:
    if agent_policy is None or not agent_policy.recipient_allowlist:
        return []
    if not _is_write_method(method):
        return []
    off_list = [
        r
        for r in _extract_recipients(args)
        if r not in agent_policy.recipient_allowlist
    ]
    if off_list:
        return [
            Reason(
                "recipient.not_allowlisted",
                _governance_points(tier),
                f"recipient '{off_list[0]}' is not in the operator allow-list",
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
    tier: TierResult | None = None,
    agent_policy: AgentPolicy | None = None,
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
    # Tier-aware op weighting when a comprehension-derived tier is supplied; otherwise the
    # flat verb weighting (existing callers pass no tier and are byte-identical).
    if tier is not None:
        reasons += _run_signal("op", _op_risk_tiered, tier, method)
    else:
        reasons += _run_signal("op", _op_risk, method)
    reasons += _run_signal("provenance", _provenance, surface_state)
    reasons += _run_signal("scope", _scope, tool_name, allowed, policy)
    # Governance predicates: no-ops unless an AgentPolicy authors a cap / allow-list.
    reasons += _run_signal("cap", _cap_signal, a, agent_policy, method, tier)
    reasons += _run_signal(
        "recipient", _recipient_signal, a, agent_policy, method, tier
    )

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

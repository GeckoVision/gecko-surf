"""``gecko inspect`` — a provider-facing agent-readiness scorecard.

Offline, $0, deterministic, control-plane only: it inspects the API *surface* and never
sends a real call, stores a payload, or touches a key. Four dimensions, each reusing
existing engine muscle where it can:

  1. first-call-correct  — reuse :mod:`gecko.testgen` (can an agent build a valid first call?)
  2. hygiene             — a deterministic spec linter (operationIds, params, auth, errors)
  3. agent-friendliness  — comprehension-native ambiguity: does each op rank #1 for its own
                           intent, or does a sibling steal it (the getTipFloor / mint-vs-symbol trap)?
  4. security            — reuse :mod:`gecko.sanitize` anti-poisoning scan on the spec text

The output is a graded :class:`InspectionReport` with located, fixable findings, plus a
CI exit code (``gecko inspect --min-grade B``) so a provider can gate a deploy on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from . import sanitize, testgen
from .access import stub_session
from .client import AgentApiClient

Severity = Literal["blocking", "warning", "info"]

_HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
)
# Penalty weights (points off a 100 baseline) for the deterministic dimensions.
_PENALTY: dict[Severity, int] = {"blocking": 15, "warning": 5, "info": 2}
# Dimension weights for the overall score. FCC is the core promise, so it leads.
_WEIGHTS: dict[str, float] = {
    "first-call-correct": 0.4,
    "hygiene": 0.2,
    "agent-friendliness": 0.2,
    "security": 0.2,
}


@dataclass(frozen=True)
class Finding:
    dimension: str
    severity: Severity
    location: str
    message: str
    fix: str


@dataclass(frozen=True)
class DimensionResult:
    name: str
    score: int  # 0-100
    findings: list[Finding]


@dataclass(frozen=True)
class InspectionReport:
    api: str
    grade: str  # "A".."F"
    score: int  # 0-100 overall
    dimensions: list[DimensionResult]
    summary: str


# --------------------------------------------------------------------------- #
# Dimension 1 — first-call-correctness (reuse testgen)
# --------------------------------------------------------------------------- #
def check_first_call_correct(spec: str | dict[str, Any]) -> DimensionResult:
    results = testgen.check(spec, mode="recorded", session=stub_session())
    passed, total = testgen.summary(results)
    findings = [
        Finding(
            "first-call-correct",
            "blocking",
            r.tool,
            r.detail or f"the {r.kind} check failed",
            "give the operation a complete schema so a valid first call is synthesizable",
        )
        for r in results
        if not r.ok
    ]
    score = round(100 * passed / total) if total else 100
    return DimensionResult("first-call-correct", score, findings)


# --------------------------------------------------------------------------- #
# Dimension 2 — spec hygiene (deterministic linter)
# --------------------------------------------------------------------------- #
def _iter_operations(spec: dict[str, Any]):
    """Yield ``(method, path, op)`` for every HTTP operation in the spec."""
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() in _HTTP_METHODS and isinstance(op, dict):
                yield method, path, op


def check_hygiene(spec: dict[str, Any]) -> DimensionResult:
    findings: list[Finding] = []
    seen_ids: dict[str, str] = {}
    for method, path, op in _iter_operations(spec):
        loc = f"{method.upper()} {path}"
        opid = op.get("operationId")
        if not opid:
            findings.append(
                Finding(
                    "hygiene",
                    "blocking",
                    loc,
                    "no operationId",
                    "add a unique operationId — it becomes the agent's tool name",
                )
            )
        elif opid in seen_ids:
            findings.append(
                Finding(
                    "hygiene",
                    "blocking",
                    loc,
                    f"duplicate operationId {opid!r} (also {seen_ids[opid]})",
                    "make every operationId unique",
                )
            )
        else:
            seen_ids[opid] = loc
        if not (op.get("summary") or op.get("description")):
            findings.append(
                Finding(
                    "hygiene",
                    "warning",
                    loc,
                    "no summary or description",
                    "add a one-line summary — it's how the agent matches intent to op",
                )
            )
        for p in op.get("parameters") or []:
            if not isinstance(p, dict):
                continue
            ploc = f"{loc} · {p.get('name', '?')}"
            if not p.get("in"):
                findings.append(
                    Finding(
                        "hygiene",
                        "warning",
                        ploc,
                        "param has no 'in'",
                        "declare in: path | query | header",
                    )
                )
            if not (p.get("schema") or p.get("type")):
                findings.append(
                    Finding(
                        "hygiene",
                        "warning",
                        ploc,
                        "param has no type/schema",
                        "give the param a schema so its value can be built correctly",
                    )
                )
        responses = op.get("responses") or {}
        if not any(str(code)[:1] in ("4", "5") for code in responses):
            findings.append(
                Finding(
                    "hygiene",
                    "info",
                    loc,
                    "no documented 4xx/5xx responses",
                    "document the error responses so an agent can recover",
                )
            )
    if not (spec.get("components") or {}).get("securitySchemes") and any(
        op.get("security") for _, _, op in _iter_operations(spec)
    ):
        findings.append(
            Finding(
                "hygiene",
                "blocking",
                "components",
                "ops require security but no securitySchemes are declared",
                "declare the securitySchemes",
            )
        )
    score = _penalty_score(findings)
    return DimensionResult("hygiene", score, findings)


# --------------------------------------------------------------------------- #
# Dimension 3 — agent-friendliness / ambiguity (comprehension-native)
# --------------------------------------------------------------------------- #
def check_agent_friendliness(client: AgentApiClient) -> DimensionResult:
    """Flag ops that are NOT the top hit for their own intent — a sibling steals the
    routing (the live getTipFloor #3 / mint-vs-symbol trap). Deterministic: the catalog
    ranking is pure. This check exists ONLY because Gecko comprehends the surface."""
    tools = client.list_tools()
    findings: list[Finding] = []
    for t in tools:
        name = t.get("name")
        intent = t.get("description") or name or ""
        hits = client.search(intent, limit=3)
        if hits and hits[0].get("name") != name:
            stealer = hits[0].get("name")
            findings.append(
                Finding(
                    "agent-friendliness",
                    "warning",
                    str(name),
                    f"an agent asking {intent!r} is routed to {stealer!r} first, not "
                    f"{name!r}",
                    "sharpen this op's summary/operationId so it wins its own intent",
                )
            )
    total = len(tools)
    score = round(100 * (total - len(findings)) / total) if total else 100
    return DimensionResult("agent-friendliness", score, findings)


# --------------------------------------------------------------------------- #
# Dimension 4 — security / anti-poisoning (reuse sanitize)
# --------------------------------------------------------------------------- #
def check_security(spec: dict[str, Any]) -> DimensionResult:
    findings: list[Finding] = []
    texts: list[tuple[str, str]] = []
    info = spec.get("info") or {}
    if isinstance(info.get("description"), str):
        texts.append(("info.description", info["description"]))
    for method, path, op in _iter_operations(spec):
        loc = f"{method.upper()} {path}"
        for field in ("summary", "description"):
            val = op.get(field)
            if isinstance(val, str):
                texts.append((f"{loc} · {field}", val))
    for loc, text in texts:
        signals = sanitize.scan_text(text)
        if signals:
            findings.append(
                Finding(
                    "security",
                    "blocking",
                    loc,
                    f"text trips the anti-poisoning scanner: {', '.join(signals)}",
                    "remove instruction-shaped / injection text from the description",
                )
            )
    score = max(0, 100 - 25 * len(findings))
    return DimensionResult("security", score, findings)


# --------------------------------------------------------------------------- #
# Aggregate + grade
# --------------------------------------------------------------------------- #
def _penalty_score(findings: list[Finding]) -> int:
    return max(0, 100 - sum(_PENALTY[f.severity] for f in findings))


def _grade(score: int) -> str:
    for cutoff, letter in ((90, "A"), (80, "B"), (70, "C"), (60, "D")):
        if score >= cutoff:
            return letter
    return "F"


def inspect(spec: dict[str, Any], *, api: str) -> InspectionReport:
    """Run all four dimensions and produce the graded report. Offline, $0."""
    client = AgentApiClient(spec, session=stub_session())
    dims = [
        check_first_call_correct(spec),
        check_hygiene(spec),
        check_agent_friendliness(client),
        check_security(spec),
    ]
    overall = round(sum(d.score * _WEIGHTS.get(d.name, 0.25) for d in dims))
    grade = _grade(overall)
    n_block = sum(1 for d in dims for f in d.findings if f.severity == "blocking")
    n_warn = sum(1 for d in dims for f in d.findings if f.severity == "warning")
    summary = (
        f"{api}: agent-readiness {grade} ({overall}/100) — "
        f"{n_block} blocking, {n_warn} warnings"
    )
    return InspectionReport(api, grade, overall, dims, summary)


def has_blocking(report: InspectionReport) -> bool:
    return any(f.severity == "blocking" for d in report.dimensions for f in d.findings)


def render(report: InspectionReport) -> str:
    """A provider-facing terminal report."""
    lines = [f"  {report.summary}", ""]
    for d in report.dimensions:
        lines.append(f"  {d.name:20} {d.score:3}/100")
        for f in sorted(
            d.findings, key=lambda x: ("blocking", "warning", "info").index(x.severity)
        ):
            mark = {"blocking": "✗", "warning": "⚠", "info": "·"}[f.severity]
            lines.append(f"      {mark} [{f.location}] {f.message}")
            lines.append(f"         → {f.fix}")
    return "\n".join(lines)

"""Preflight — the pre-prod agent-callability gate.

Point Preflight at an API spec (or endpoint) and it answers one question a linter and a
hand-written contract test cannot: *can an agent actually use this?* It comprehends the
surface exactly as the engine would serve it (``comprehend_service.comprehend_client``),
then runs four deterministic, $0, offline checks — no LLM, no live HTTP — each yielding
findings tagged with a closed-set failure CLASS and the op/tool it fired on:

1. **Coverage** — operations → agent-usable tools; a high unusable ratio is a warning.
2. **Tool-def health** — valid inputSchema, required declared, typed params, and no
   credential leaked into the agent-facing def (invariant #4).
3. **Adversarial safety** — the anti-poisoning / injection scan over the comprehended
   surface (reuses ``sanitize``, the redteam's own primitives).
4. **Drift** — only when a ``baseline`` is given: diff the ``surface_rev`` fingerprint,
   then diff the tool set for breaking changes an agent would hit.

The verdict is ``fail`` on any BLOCKING class (auth.misdeclared, surface.poisoned/-
injectable, breaking drift) and ``pass`` otherwise. Every run appends one control-plane-
safe record (failure CLASSES + ``surface_rev`` + counts, never a value) to the opt-in
Preflight corpus — the compounding cross-API moat, load-bearing from day one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import sanitize
from .comprehend_service import comprehend_client
from .preflight_corpus import (
    BLOCKING_CLASSES,
    KNOWN_CLASSES,
    PreflightRun,
    known_classes_from_corpus,
    record,
)
from .surfaces import surface_rev

Severity = Literal["error", "warning"]

# Property NAMES that are credentials and must NEVER be an agent-facing input — the access
# layer injects them (invariant #4). A header-located auth param is already stripped by
# ``tools._is_auth_param``; this catches the misdeclared case where the same secret rides
# in as a query/body field and survives comprehension. Deliberately narrow (no bare
# "token"/"secret") so a legitimate pagination/session field never false-positives.
_AUTH_LEAK_NAMES = frozenset(
    {
        "authorization",
        "apikey",
        "api_key",
        "api-key",
        "x-api-key",
        "x-apikey",
        "x-api-token",
        "api_token",
        "api-token",
        "access_token",
        "access-token",
        "bearer",
        "bearer_token",
        "client_secret",
        "secret_key",
        "private_key",
    }
)

# Below this usable ratio, "an agent can't use most of this" — a coverage warning.
_LOW_USABLE_RATIO = 0.5


@dataclass(frozen=True)
class Finding:
    """One thing an agent would break on. ``cls`` is a CLOSED-set failure class
    (``preflight_corpus.KNOWN_CLASSES``), ``op`` names the operation/tool it fired on
    (``"-"`` for a surface-level finding), ``detail`` is a human note (never a value), and
    ``severity`` decides whether it blocks the build."""

    cls: str
    op: str
    detail: str
    severity: Severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "class": self.cls,
            "op": self.op,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class PreflightReport:
    """The gate's answer: where an agent breaks, and whether the build passes.

    ``findings`` are the tagged breakages; ``verdict`` is ``pass`` iff no finding carries a
    BLOCKING class. ``checked_classes`` is the union of the v1 seed vocabulary and the
    classes read back from the corpus (the flywheel read-path) — the classes Preflight
    WATCHED on this surface, so a class learned on API #1 is visibly watched on API #2."""

    surface_id: str
    surface_rev: str
    findings: list[Finding]
    op_count: int
    usable_tool_count: int
    checked_classes: list[str]
    # Minimal per-tool shape (name → sorted required fields) — the drift snapshot. Carried
    # so a saved report round-trips into a full drift diff, never the spec text.
    tool_required: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_baseline(self) -> Baseline:
        """This report as a drift ``Baseline`` for the next run."""
        return Baseline(surface_rev=self.surface_rev, tool_required=self.tool_required)

    @property
    def verdict(self) -> Literal["pass", "fail"]:
        return "fail" if self.blocking_findings else "pass"

    @property
    def blocking_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.cls in BLOCKING_CLASSES]

    @property
    def counts(self) -> dict[str, int]:
        """Per-severity finding counts — the control-plane-safe corpus counters."""
        out = {"error": 0, "warning": 0}
        for f in self.findings:
            out[f.severity] += 1
        return out

    @property
    def classes(self) -> list[str]:
        """The distinct failure classes this run FOUND (what the corpus records)."""
        return sorted({f.cls for f in self.findings})

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface_id": self.surface_id,
            "surface_rev": self.surface_rev,
            "verdict": self.verdict,
            "op_count": self.op_count,
            "usable_tool_count": self.usable_tool_count,
            "counts": self.counts,
            "classes": self.classes,
            "checked_classes": self.checked_classes,
            "tool_required": self.tool_required,
            "findings": [f.to_dict() for f in self.findings],
            "warnings": self.warnings,
        }

    def render(self) -> str:
        """Human report — grouped by check, lead with where an agent breaks."""
        mark = "PASS" if self.verdict == "pass" else "FAIL"
        lines = [
            f"Preflight {mark}: {self.surface_id} @ {self.surface_rev}",
            f"  operations: {self.op_count}   agent-usable tools: {self.usable_tool_count}"
            f"   findings: {self.counts['error']} blocking / {self.counts['warning']} warning",
        ]
        if not self.findings:
            lines.append("  no findings — an agent can use this surface.")
        else:
            blocking = self.blocking_findings
            warnings = [f for f in self.findings if f.severity == "warning"]
            if blocking:
                lines.append("  BLOCKING (an agent would break — build fails):")
                lines.extend(f"    [{f.cls}] {f.op}: {f.detail}" for f in blocking)
            if warnings:
                lines.append("  warnings (an agent may struggle):")
                lines.extend(f"    [{f.cls}] {f.op}: {f.detail}" for f in warnings)
        lines.append(
            f"  watching {len(self.checked_classes)} known failure class(es) "
            "(seed + corpus)."
        )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Check 1 — coverage: operations → agent-usable tools.
# --------------------------------------------------------------------------- #
def _check_coverage(client: Any) -> list[Finding]:
    findings: list[Finding] = []
    usable = {t["name"] for t in client.list_tools()}
    quarantined = client.anchor.state == "quarantined"
    if quarantined:
        findings.append(
            Finding(
                "coverage.quarantined",
                "-",
                "surface is quarantined (recovered from docs or tripped anti-poisoning): "
                "auth injection is disabled and it needs human review",
                "warning",
            )
        )
    for tool in client.tools:
        name = tool["name"]
        if name in usable:
            continue
        if tool.get("requires_auth"):
            schemes = ", ".join(tool.get("auth_schemes") or []) or "unknown scheme"
            findings.append(
                Finding(
                    "coverage.auth_gated_no_cred",
                    name,
                    f"needs auth the session can't provide ({schemes}); "
                    "an agent without a credential can't call it",
                    "warning",
                )
            )
        else:
            findings.append(
                Finding(
                    "coverage.malformed",
                    name,
                    "operation produced no usable tool",
                    "warning",
                )
            )
    op_count = len(client.operations)
    usable_count = len(usable)
    if op_count and usable_count / op_count < _LOW_USABLE_RATIO:
        findings.append(
            Finding(
                "coverage.low_usable",
                "-",
                f"only {usable_count}/{op_count} operations are agent-usable — "
                "an agent can't use most of this surface",
                "warning",
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# Check 2 — tool-def health (per agent-facing tool).
# --------------------------------------------------------------------------- #
def _is_typed(schema: Any) -> bool:
    """A property an agent can fill: it declares a JSON ``type`` or an equivalent shape
    (enum / const / a combinator / a $ref). Anything else is untyped 'any'."""
    if not isinstance(schema, dict):
        return False
    if "type" in schema:
        return True
    return bool(
        schema.keys()
        & {"enum", "const", "$ref", "anyOf", "oneOf", "allOf", "properties", "items"}
    )


def _check_tool_health(client: Any) -> list[Finding]:
    findings: list[Finding] = []
    for tool in client.list_tools():
        name = tool["name"]
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            findings.append(
                Finding(
                    "schema.invalid",
                    name,
                    "inputSchema is not an object schema — an agent can't build a call",
                    "error",
                )
            )
            continue
        props = schema.get("properties")
        if not isinstance(props, dict):
            findings.append(
                Finding(
                    "schema.invalid",
                    name,
                    "inputSchema has no properties map",
                    "error",
                )
            )
            continue
        for req in schema.get("required", []) or []:
            if req not in props:
                findings.append(
                    Finding(
                        "schema.required_undeclared",
                        name,
                        f"required field '{req}' has no property definition",
                        "error",
                    )
                )
        for prop_name, prop_schema in props.items():
            if str(prop_name).lower() in _AUTH_LEAK_NAMES:
                findings.append(
                    Finding(
                        "auth.misdeclared",
                        name,
                        f"credential field '{prop_name}' is exposed as an agent input — "
                        "auth must be injected by the access layer, never asked of the agent",
                        "error",
                    )
                )
            elif prop_name != "body" and not _is_typed(prop_schema):
                findings.append(
                    Finding(
                        "param.untyped",
                        name,
                        f"parameter '{prop_name}' has no declared type — "
                        "an agent must guess how to fill it",
                        "warning",
                    )
                )
    return findings


# --------------------------------------------------------------------------- #
# Check 3 — adversarial safety (anti-poisoning / injection over the raw surface).
# --------------------------------------------------------------------------- #
# Mirror the shipped sanitizer's channel split EXACTLY, so Preflight never flags what
# comprehension would let through (no false positives): the injection detector (``scan_text``)
# runs on FREE-TEXT fields; the secret-value detector (``looks_like_secret_value``) runs ONLY
# on schema VALUE channels (a run of prose words can look BIP-39-shaped, so it must never be
# fed to the secret detector — that is precisely the trap this split avoids).
_TEXT_KEYS = frozenset({"description", "title", "$comment"})
_VALUE_KEYS = frozenset({"default", "example", "const"})
_VALUE_LIST_KEYS = frozenset({"enum", "examples"})


def _scan_schema(node: Any, *, _depth: int = 0) -> tuple[bool, bool]:
    """Walk a schema; return ``(injectable, poisoned)`` using the same channel split as
    ``sanitize.sanitize_schema``. Free-text keys → injection scan; value channels → secret
    + injection scan. Depth-capped against attacker-shaped nesting."""
    injectable = poisoned = False
    if _depth > 8:
        return injectable, poisoned
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _TEXT_KEYS and isinstance(value, str):
                injectable = injectable or bool(sanitize.scan_text(value))
            elif key in _VALUE_KEYS:
                injectable, poisoned = _scan_value(value, injectable, poisoned)
            elif key in _VALUE_LIST_KEYS and isinstance(value, list):
                for item in value:
                    injectable, poisoned = _scan_value(item, injectable, poisoned)
            else:
                sub_i, sub_p = _scan_schema(value, _depth=_depth + 1)
                injectable, poisoned = injectable or sub_i, poisoned or sub_p
    elif isinstance(node, list):
        for item in node:
            sub_i, sub_p = _scan_schema(item, _depth=_depth + 1)
            injectable, poisoned = injectable or sub_i, poisoned or sub_p
    return injectable, poisoned


def _scan_value(value: Any, injectable: bool, poisoned: bool) -> tuple[bool, bool]:
    if isinstance(value, str):
        injectable = injectable or bool(sanitize.scan_text(value))
        poisoned = poisoned or sanitize.looks_like_secret_value(value)
    return injectable, poisoned


def _check_safety(client: Any) -> list[Finding]:
    """Scan the RAW comprehended operations for injection + secrets using the SAME detectors
    and channel split the shipped anti-poisoning path uses (free text → injection; value
    channels → secret), so a finding here is exactly what would quarantine the surface."""
    findings: list[Finding] = []
    for op in client.operations:
        name = op.operation_id
        injectable = False
        poisoned = False
        # Free-text fields → injection detector only (never the secret detector; see above).
        free_text = [
            op.summary,
            op.description,
            *(p.description for p in op.parameters),
        ]
        for text in free_text:
            if text and sanitize.scan_text(text):
                injectable = True
                break
        # Parameter + body schemas → the value-channel-aware walk.
        schemas: list[Any] = [p.schema for p in op.parameters]
        if op.request_body:
            schemas.append(op.request_body)
        for schema in schemas:
            sub_i, sub_p = _scan_schema(schema)
            injectable, poisoned = injectable or sub_i, poisoned or sub_p
        if injectable:
            findings.append(
                Finding(
                    "surface.injectable",
                    name,
                    "operation text carries instruction-injection an agent would read as a command",
                    "error",
                )
            )
        if poisoned:
            findings.append(
                Finding(
                    "surface.poisoned",
                    name,
                    "operation carries a secret-shaped value in a spec field",
                    "error",
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# Check 4 — drift (only when a baseline is supplied).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Baseline:
    """A prior surface snapshot to diff against: the ``surface_rev`` fingerprint plus a
    minimal per-tool shape (name → sorted required field names). Built from a prior report
    or a prior ``PreflightReport.baseline_snapshot()`` — never the full spec text."""

    surface_rev: str
    tool_required: dict[str, list[str]]


def _baseline_snapshot(client: Any) -> Baseline:
    return Baseline(
        surface_rev=surface_rev(client.spec),
        tool_required={
            t["name"]: sorted((t.get("inputSchema") or {}).get("required", []) or [])
            for t in client.list_tools()
        },
    )


def _coerce_baseline(baseline: Baseline | dict[str, Any] | str) -> Baseline:
    """Accept a ``Baseline``, a saved report/dict, or a bare ``surface_rev`` string."""
    if isinstance(baseline, Baseline):
        return baseline
    if isinstance(baseline, str):
        return Baseline(surface_rev=baseline, tool_required={})
    # A saved report dict — pull the fingerprint + the tool-required snapshot if present.
    rev = str(baseline.get("surface_rev", ""))
    snap = baseline.get("tool_required")
    return Baseline(
        surface_rev=rev,
        tool_required=snap if isinstance(snap, dict) else {},
    )


def _check_drift(
    client: Any, baseline: Baseline | dict[str, Any] | str
) -> list[Finding]:
    base = _coerce_baseline(baseline)
    current = _baseline_snapshot(client)
    if base.surface_rev == current.surface_rev:
        return []  # identical fingerprint — no drift, no tool diff needed
    findings: list[Finding] = []
    old_tools = base.tool_required
    new_tools = current.tool_required
    for name in old_tools:
        if name not in new_tools:
            findings.append(
                Finding(
                    "drift.op_removed",
                    name,
                    "operation existed in the baseline and is gone — "
                    "every agent calling it breaks at once",
                    "error",
                )
            )
    for name in new_tools:
        if name not in old_tools:
            findings.append(
                Finding(
                    "drift.op_added",
                    name,
                    "new operation since the baseline (informational)",
                    "warning",
                )
            )
    for name in old_tools.keys() & new_tools.keys():
        old_req = set(old_tools[name])
        new_req = set(new_tools[name])
        added = new_req - old_req
        removed = old_req - new_req
        if added and removed:
            findings.append(
                Finding(
                    "drift.field_renamed",
                    name,
                    f"required field(s) changed ({sorted(removed)} → {sorted(added)}) — "
                    "an agent filling the old name breaks",
                    "error",
                )
            )
        elif added:
            findings.append(
                Finding(
                    "drift.required_tightened",
                    name,
                    f"field(s) became required ({sorted(added)}) — "
                    "an agent omitting them now fails",
                    "error",
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# The gate.
# --------------------------------------------------------------------------- #
def run_preflight(
    source: str,
    baseline: Baseline | dict[str, Any] | str | None = None,
    *,
    from_docs: bool = False,
    corpus_path: str | Path | None = None,
) -> PreflightReport:
    """Comprehend ``source`` and run the deterministic agent-callability gate.

    Deterministic, $0, offline: comprehension builds a public (no-auth) client from the
    spec bytes — no LLM, no live upstream call. ``baseline`` (a prior ``surface_rev``, a
    ``Baseline``, or a saved report dict) enables the drift check. ``corpus_path`` (opt-in,
    off by default) appends one control-plane-safe run record and seeds the cross-API
    known-classes read-path.
    """
    client, _warnings = comprehend_client(source, from_docs=from_docs)

    findings: list[Finding] = []
    findings.extend(_check_coverage(client))
    findings.extend(_check_tool_health(client))
    findings.extend(_check_safety(client))
    if baseline is not None:
        findings.extend(_check_drift(client, baseline))

    # The flywheel read-path: the classes Preflight WATCHED = the v1 seed vocabulary unioned
    # with every class prior runs recorded (so a class learned on API #1 shows up as watched
    # when checking API #2).
    checked = KNOWN_CLASSES | known_classes_from_corpus(corpus_path)

    report = PreflightReport(
        surface_id=client.surface_id,
        surface_rev=client.surface_rev,
        findings=findings,
        op_count=len(client.operations),
        usable_tool_count=len(client.list_tools()),
        checked_classes=sorted(checked),
        tool_required=_baseline_snapshot(client).tool_required,
    )

    if corpus_path is not None:
        record(
            PreflightRun(
                ts=int(time.time() * 1000),
                surface_id=report.surface_id,
                surface_rev=report.surface_rev,
                classes=report.classes,
                counts=report.counts,
            ),
            corpus_path,
        )
    return report


# --------------------------------------------------------------------------- #
# Thin CLI transport — parse args, call the package, format, exit 0/1 to gate a build.
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    from .comprehend_service import ComprehendError

    parser = argparse.ArgumentParser(
        prog="gecko.preflight",
        description="Pre-prod agent-callability gate: fail the build if an agent would break.",
    )
    parser.add_argument(
        "source", help="OpenAPI spec URL or local path (or a docs page)"
    )
    parser.add_argument(
        "--baseline",
        metavar="PATH",
        help="a prior Preflight report JSON to diff against (drift check)",
    )
    parser.add_argument(
        "--from-docs",
        action="store_true",
        help="recover a draft surface from a human docs page (born quarantined)",
    )
    parser.add_argument(
        "--corpus", metavar="PATH", help="append a control-plane-safe run record here"
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    baseline: dict[str, Any] | None = None
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))

    try:
        report = run_preflight(
            args.source,
            baseline,
            from_docs=args.from_docs,
            corpus_path=args.corpus,
        )
    except ComprehendError as exc:
        print(f"preflight: {exc}")
        return 1

    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render())
    return 0 if report.verdict == "pass" else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())

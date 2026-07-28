"""``gecko report`` — the Agent-Readiness Scorecard, a provider leave-behind.

One self-contained HTML file (inline CSS, inline SVG, no server, no external deps — same
class as :mod:`gecko.surfaceviz`'s SVG and the Skill-Guard artifacts). It is the a-ha, the
surface, and the leave-behind in one file:

  * the big **grade + score** — the number the provider owns
  * the four :mod:`gecko.inspect` dimensions as bars
  * the **call graph** (``surfaceviz.render_svg``) embedded inline
  * the ranked, fixable **findings** as a checklist
  * a scripted **Playground** — a chat-style transcript replaying the deterministic
    intent → derived tool → first-call-correct call (no live LLM, no real network)

Deterministic (same spec in → byte-stable HTML out — everything is sorted, no timestamps)
and control-plane clean (structure/scores/finding-names/synthesized call shapes only —
never a response payload, secret, or key). The Playground shows the derived CALL, never a
real response value.
"""

from __future__ import annotations

from html import escape
from typing import Any, get_args

from .access import stub_session
from .client import AgentApiClient, ToolNotFound
from .graph import Provenance, VerifyStatus, op_provenance
from .ingest import load_spec
from .inspect import InspectionReport, inspect
from .modes import CallMode
from .sample import example_from_schema
from .surfaceviz import graph_data, render_svg
from .verify import verify_docs

__all__ = ["build_scorecard", "report_diff", "render_diff"]

# Severity → (rank for ordering, glyph, css class). Blocking first — it's what breaks a call.
_SEV: dict[str, tuple[int, str, str]] = {
    "blocking": (0, "✗", "sev-block"),
    "warning": (1, "⚠", "sev-warn"),
    "info": (2, "·", "sev-info"),
}
# Grade → accent for the badge (the one place colour carries meaning beyond the accent).
_GRADE_COLOR: dict[str, str] = {
    "A": "#0e9f6e",
    "B": "#22c55e",
    "C": "#d97706",
    "D": "#ea580c",
    "F": "#dc2626",
}
_MAX_INTENTS = 3

# Verify verdict → (badge label, css class). REFUTED is the one strong-red accent —
# the flagship demo frame (a doc claim the live API refuted). UNVERIFIED is muted amber:
# recorded mode never overclaims (VAS honesty rule — no wire evidence, no VERIFIED).
_VERDICT_BADGE: dict[VerifyStatus, tuple[str, str]] = {
    "VERIFIED": ("VERIFIED", "v-verified"),
    "REFUTED": ("REFUTED", "v-refuted"),
    "UNVERIFIED": ("UNVERIFIED", "v-unverified"),
}
# Provenance → css class for the chip, derived from the canonical Literal so the
# vocabulary (incl. the untrusted-docs tier a verdict resolves) has one home in graph.py.
_PROV_CLASS: dict[str, str] = {p: f"prov-{p.lower()}" for p in get_args(Provenance)}


# --------------------------------------------------------------------------- #
# Playground — the deterministic intent → derived-call replay
# --------------------------------------------------------------------------- #
def _auto_intents(client: AgentApiClient) -> list[str]:
    """Pick a few plain-English intents from the surface's own tools (deterministic).

    Uses each tool's question-shaped description (already sanitized) as the "user" line,
    stable-sorted by tool name so the transcript is byte-identical run to run."""
    intents: list[str] = []
    for tool in sorted(client.list_tools(), key=lambda t: t["name"]):
        first = (
            (tool.get("description") or tool["name"]).strip().splitlines()[0].strip()
        )
        if first and first not in intents:
            intents.append(first[:120])
        if len(intents) >= _MAX_INTENTS:
            break
    return intents


def _derive_call(client: AgentApiClient, intent: str) -> dict[str, Any] | None:
    """Replay Gecko's derivation for one intent: search → derived tool → first-call shape.

    Returns the DERIVED CALL (method, path, synthesized param placeholders, optional
    supplier-chain plan) — never a real response value (control plane). The param values
    are synthesized from the schema, exactly as the $0 recorded path does."""
    hits = client.search(intent, limit=1)
    if not hits:
        return None
    top = hits[0]
    name = top["name"]
    try:
        tool = client.get_tool(name)
    except ToolNotFound:
        return None
    args = example_from_schema(tool.get("inputSchema") or {})
    if not isinstance(args, dict):
        args = {}
    locations = tool.get("_invoke", {}).get("param_locations", {})
    params = [
        (key, str(locations.get(key, "query")), value)
        for key, value in sorted(args.items())
    ]
    plan = client.plan_for(intent, name)
    return {
        "intent": intent,
        "tool": name,
        "method": top.get("method") or "",
        "path": top.get("path") or "",
        "params": params,
        "plan_steps": (plan or {}).get("steps") if plan else None,
    }


def _render_playground(client: AgentApiClient, intents: list[str]) -> str:
    entries = [c for c in (_derive_call(client, i) for i in intents) if c is not None]
    if not entries:
        return ""
    rows: list[str] = []
    for entry in entries:
        params_html = ""
        if entry["params"]:
            items = "".join(
                f'<tr><td class="p-name">{escape(name)}</td>'
                f'<td class="p-loc">{escape(loc)}</td>'
                f'<td class="p-val">{escape(_short(value))}</td></tr>'
                for name, loc, value in entry["params"]
            )
            params_html = f'<table class="params">{items}</table>'
        plan_html = ""
        if entry["plan_steps"]:
            steps = " → ".join(
                escape(f"{s.get('method', '')} {s.get('path', '')}".strip())
                for s in entry["plan_steps"]
            )
            plan_html = f'<div class="plan"><span class="tag">supplier chain</span> {steps}</div>'
        rows.append(
            '<div class="turn">'
            f'<div class="bubble user"><span class="who">You</span>'
            f"{escape(entry['intent'])}</div>"
            '<div class="bubble gecko"><span class="who">Gecko</span>'
            f'<div class="derived">derived tool <code>{escape(entry["tool"])}</code></div>'
            f'<div class="call"><span class="method">{escape(entry["method"])}</span> '
            f"<code>{escape(entry['path'])}</code></div>"
            f"{params_html}{plan_html}</div>"
            "</div>"
        )
    return (
        '<section class="card playground">'
        "<h2>The Playground</h2>"
        '<p class="lede">Plain-English intent in, the first-call-correct call out — '
        "replayed deterministically from the surface. No live model, no network.</p>"
        f"{''.join(rows)}"
        "</section>"
    )


def _short(value: Any, cap: int = 40) -> str:
    text = str(value)
    return text if len(text) <= cap else text[: cap - 1] + "…"


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def _render_header(report: InspectionReport) -> str:
    color = _GRADE_COLOR.get(report.grade, "#4f46e5")
    verdict = (
        f"{report.score}/100 — how often an agent calls "
        f"{report.api} right, the first try."
    )
    return (
        '<header class="card head">'
        '<div class="head-copy">'
        f'<div class="eyebrow">Agent-Readiness Scorecard · {escape(report.api)}</div>'
        f'<h1 class="verdict">{escape(verdict)}</h1>'
        '<p class="subline">Your OpenAPI spec is a schema your agent <em>guesses</em> '
        "from. This score is how much it has to guess. Stop letting it guess.</p>"
        "</div>"
        '<div class="grade-badge" style="--grade:' + color + '">'
        f'<div class="grade">{escape(report.grade)}</div>'
        f'<div class="score">{report.score}<span>/100</span></div>'
        "</div>"
        "</header>"
    )


def _render_dimensions(report: InspectionReport) -> str:
    bars: list[str] = []
    for dim in report.dimensions:
        bars.append(
            '<div class="dim">'
            f'<div class="dim-top"><span class="dim-name">{escape(dim.name)}</span>'
            f'<span class="dim-score">{dim.score}</span></div>'
            f'<div class="track"><div class="fill" style="width:{dim.score}%"></div></div>'
            "</div>"
        )
    return (
        '<section class="card">'
        "<h2>Dimension breakdown</h2>"
        '<p class="lede">First-call-correct is weighted 0.4 — it is the promise. '
        "Hygiene, agent-friendliness, and security round it out.</p>"
        f"{''.join(bars)}"
        "</section>"
    )


def _render_graph(svg: str) -> str:
    return (
        '<section class="card">'
        "<h2>Your API as an agent traverses it</h2>"
        '<p class="lede">The derived call graph — operations as nodes, outputs feeding '
        "inputs as arrows, each coloured by provenance.</p>"
        f'<div class="graph">{svg}</div>'
        "</section>"
    )


def _render_findings(report: InspectionReport) -> str:
    findings = [f for d in report.dimensions for f in d.findings]
    findings.sort(key=lambda f: (_SEV[f.severity][0], f.dimension, f.location))
    if not findings:
        return (
            '<section class="card">'
            "<h2>Fixable findings</h2>"
            '<p class="lede">Nothing to fix — this surface is clean.</p>'
            "</section>"
        )
    rows: list[str] = []
    for f in findings:
        _, glyph, cls = _SEV[f.severity]
        rows.append(
            f'<li class="finding {cls}">'
            f'<span class="mark">{glyph}</span>'
            '<div class="f-body">'
            f'<div class="f-head"><span class="f-dim">{escape(f.dimension)}</span>'
            f'<code class="f-loc">{escape(f.location)}</code></div>'
            f'<div class="f-msg">{escape(f.message)}</div>'
            f'<div class="f-fix">→ {escape(f.fix)}</div>'
            "</div></li>"
        )
    return (
        '<section class="card">'
        "<h2>Fixable findings</h2>"
        '<p class="lede">Each one is a call an agent gets wrong, and exactly how to fix it. '
        "Improve the score by clearing the list.</p>"
        f'<ul class="findings">{"".join(rows)}</ul>'
        "</section>"
    )


def _render_correlation(gdata: dict[str, Any]) -> str:
    if not gdata.get("edges"):
        return ""
    return (
        '<section class="card teaser">'
        "<h2>Correlation</h2>"
        '<p class="lede">Your outputs already feed your inputs — the arrows above. '
        "Across a second API, that becomes a provenance-carrying chain your agent can "
        "traverse without guessing.</p>"
        "</section>"
    )


# --------------------------------------------------------------------------- #
# Verified-against-reality — the claimed / verified / refuted verdict tier (VAS-3)
# --------------------------------------------------------------------------- #
def _render_verify(client: AgentApiClient) -> str:
    """Render the per-op provenance + verify-verdict section from the surface graph.

    Reads each op's ``VerifyVerdict`` off its graph node (attached by
    :func:`gecko.verify.verify_docs`) and its provenance off :func:`op_provenance` — two
    SEPARATE axes. An op with no attached verdict is honestly UNVERIFIED (not checked).
    Control-plane clean: badge = status + basis strings + a provenance category only,
    never a response value. Deterministic — ops are sorted by operation id.
    """
    graph = client.surface_graph
    counts = {"VERIFIED": 0, "REFUTED": 0, "UNVERIFIED": 0}
    rows: list[str] = []
    for op in sorted(client.operations, key=lambda o: o.operation_id):
        op_id = op.operation_id
        node = graph._by_id.get(graph.opnode(op_id))
        verdict = node.verify if node is not None else None
        status: VerifyStatus = verdict.status if verdict is not None else "UNVERIFIED"
        counts[status] += 1
        label, badge_cls = _VERDICT_BADGE[status]
        prov = op_provenance(client.spec, op_id)
        prov_cls = _PROV_CLASS.get(prov, "prov-extracted")

        basis_html = ""
        if verdict is not None and verdict.basis:
            basis_html = (
                '<div class="v-basis">checked against reality: '
                f"{escape(' · '.join(verdict.basis))}</div>"
            )
        rows.append(
            '<li class="vrow">'
            '<div class="v-head">'
            f'<span class="vbadge {badge_cls}">{label}</span>'
            f'<span class="prov {prov_cls}">{escape(prov)}</span>'
            f'<code class="v-op"><span class="method">{escape(op.method)}</span> '
            f"{escape(op.path)}</code>"
            "</div>"
            f"{basis_html}"
            "</li>"
        )

    summary = (
        f"{counts['VERIFIED']} verified · {counts['REFUTED']} refuted · "
        f"{counts['UNVERIFIED']} unverified"
    )
    return (
        '<section class="card verify">'
        "<h2>Verified against reality</h2>"
        '<p class="lede">Stop guessing which doc claims are real. Gecko replayed each '
        "call against the live API and lifted the outcome into a verdict — "
        f'<strong>{summary}</strong>. A red <span class="vbadge v-refuted '
        'inline">REFUTED</span> is a claim the API does not serve.</p>'
        f'<ul class="verdicts">{"".join(rows)}</ul>'
        "</section>"
    )


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
def build_scorecard(
    spec: str | dict[str, Any],
    *,
    intents: list[str] | None = None,
    base_url: str | None = None,
    verify: bool = False,
    verify_mode: CallMode = "recorded",
) -> str:
    """Build the full self-contained HTML scorecard for ``spec``.

    Deterministic (same input → byte-stable output) and control-plane clean. ``intents``
    scripts the Playground; when omitted, a few are auto-picked from the surface's tools.

    ``verify`` (opt-in) runs :func:`gecko.verify.verify_docs` first — replaying every op
    against reality (recorded by default, ``verify_mode="live"`` for real calls) — and
    renders the verify-verdict section (provenance + verified/refuted badges). When
    ``verify`` is False
    the output is byte-identical to the plain scorecard (backward-compatible).
    """
    spec_dict = load_spec(spec) if isinstance(spec, str) else spec
    api = str((spec_dict.get("info") or {}).get("title") or "API")

    report = inspect(spec_dict, api=api)
    client = AgentApiClient(spec_dict, base_url=base_url, session=stub_session())
    graph = client.surface_graph
    svg = render_svg(graph, title=f"Agent Surface — {escape(api)}")
    gdata = graph_data(graph)

    play_intents = intents if intents else _auto_intents(client)

    # Opt-in verification: mutate the graph nodes with verdicts, then render them. Off by
    # default so the section string is "" and the body stays byte-identical to today.
    verify_html = ""
    if verify:
        verify_docs(client, mode=verify_mode)
        verify_html = _render_verify(client)

    body = "".join(
        (
            _render_header(report),
            _render_dimensions(report),
            verify_html,
            _render_graph(svg),
            _render_findings(report),
            _render_playground(client, play_intents),
            _render_correlation(gdata),
        )
    )
    return _HTML_SHELL.format(
        title=escape(f"{api} — Agent-Readiness Scorecard"), body=body
    )


# --------------------------------------------------------------------------- #
# Drift-diff — two report dicts in, a delta out
# --------------------------------------------------------------------------- #
def _finding_set(report: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Findings keyed by (dimension, location, message) — the stable identity for diffing."""
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for dim in report.get("dimensions") or []:
        for f in dim.get("findings") or []:
            out[
                (f.get("dimension", ""), f.get("location", ""), f.get("message", ""))
            ] = f
    return out


def report_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Diff two :meth:`InspectionReport.to_dict` payloads — the drift readout.

    Score delta + added/resolved findings (e.g. "v4 broke 3 agent call-paths"). Pure
    structure; no payloads. A negative ``score.delta`` is a regression."""
    old_score = int(old.get("score", 0))
    new_score = int(new.get("score", 0))
    old_f, new_f = _finding_set(old), _finding_set(new)
    added = [new_f[k] for k in sorted(new_f.keys() - old_f.keys())]
    resolved = [old_f[k] for k in sorted(old_f.keys() - new_f.keys())]
    delta = new_score - old_score
    verb = "broke" if delta < 0 else ("cleared" if delta > 0 else "changed")
    summary = (
        f"{new.get('api', 'API')}: score {old_score} → {new_score} ({delta:+d}); "
        f"{len(added)} finding(s) added, {len(resolved)} resolved"
    )
    if added:
        summary += f" — {verb} {len(added)} agent call-path(s)"
    return {
        "surface_rev": {
            "old": old.get("surface_rev", ""),
            "new": new.get("surface_rev", ""),
        },
        "grade": {"old": old.get("grade", ""), "new": new.get("grade", "")},
        "score": {"old": old_score, "new": new_score, "delta": delta},
        "added_findings": added,
        "resolved_findings": resolved,
        "summary": summary,
    }


def render_diff(diff: dict[str, Any]) -> str:
    """A terminal-friendly rendering of :func:`report_diff` (keeps the CLI thin)."""
    lines = [f"  {diff['summary']}", ""]
    for label, key in (
        ("+ added", "added_findings"),
        ("- resolved", "resolved_findings"),
    ):
        for f in diff[key]:
            lines.append(f"  {label}  [{f.get('location', '')}] {f.get('message', '')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The shell — a light, professional, single-file readout (system fonts, inline CSS)
# --------------------------------------------------------------------------- #
_HTML_SHELL = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg:#f6f7f9; --card:#ffffff; --ink:#0f172a; --muted:#64748b; --line:#e5e7eb;
  --accent:#4f46e5; --accent-soft:#eef2ff; --track:#eef0f4;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font-family:var(--sans);
  line-height:1.5; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:880px; margin:0 auto; padding:40px 24px 64px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px;
  padding:28px 30px; margin:18px 0; box-shadow:0 1px 2px rgba(15,23,42,.04); }}
h1 {{ font-size:26px; line-height:1.25; margin:0 0 8px; letter-spacing:-.01em; }}
h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted);
  margin:0 0 6px; font-weight:600; }}
.lede {{ color:var(--muted); margin:0 0 20px; font-size:14.5px; }}
.eyebrow {{ font-size:12.5px; font-weight:600; letter-spacing:.04em; color:var(--accent);
  text-transform:uppercase; margin-bottom:10px; }}
.head {{ display:flex; gap:28px; align-items:center; justify-content:space-between; }}
.head-copy {{ flex:1; }}
.verdict {{ font-weight:700; }}
.subline {{ color:var(--muted); margin:0; font-size:14.5px; max-width:52ch; }}
.grade-badge {{ text-align:center; min-width:132px; padding:18px 20px; border-radius:14px;
  background:var(--accent-soft); border:1px solid var(--line); }}
.grade {{ font-size:56px; font-weight:800; line-height:1; color:var(--grade); }}
.score {{ font-size:20px; font-weight:700; margin-top:6px; color:var(--ink); }}
.score span {{ color:var(--muted); font-weight:500; font-size:14px; }}
.dim {{ margin:14px 0; }}
.dim-top {{ display:flex; justify-content:space-between; font-size:14px;
  margin-bottom:6px; }}
.dim-name {{ font-weight:600; }}
.dim-score {{ font-variant-numeric:tabular-nums; color:var(--muted); font-weight:600; }}
.track {{ height:9px; background:var(--track); border-radius:99px; overflow:hidden; }}
.fill {{ height:100%; background:var(--accent); border-radius:99px; }}
.graph {{ overflow-x:auto; border:1px solid var(--line); border-radius:12px;
  background:#0b1020; }}
.graph svg {{ display:block; max-width:100%; height:auto; }}
.findings {{ list-style:none; margin:0; padding:0; }}
.finding {{ display:flex; gap:12px; padding:14px 0; border-top:1px solid var(--line); }}
.finding:first-child {{ border-top:none; }}
.mark {{ font-size:15px; line-height:1.6; width:18px; text-align:center; flex:none; }}
.sev-block .mark {{ color:#dc2626; }}
.sev-warn .mark {{ color:#d97706; }}
.sev-info .mark {{ color:var(--muted); }}
.f-head {{ display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; }}
.f-dim {{ font-size:11.5px; text-transform:uppercase; letter-spacing:.05em;
  color:var(--accent); font-weight:700; }}
.f-loc {{ font-family:var(--mono); font-size:12.5px; color:var(--ink); }}
.f-msg {{ font-size:14px; margin:3px 0; }}
.f-fix {{ font-size:13px; color:var(--muted); }}
.turn {{ margin:16px 0; }}
.bubble {{ border-radius:12px; padding:12px 15px; font-size:14px; max-width:88%; }}
.bubble .who {{ display:block; font-size:11px; font-weight:700; text-transform:uppercase;
  letter-spacing:.05em; margin-bottom:5px; color:var(--muted); }}
.bubble.user {{ background:var(--accent-soft); border:1px solid #e0e7ff;
  margin-left:auto; }}
.bubble.gecko {{ background:#f8fafc; border:1px solid var(--line); margin-top:8px; }}
.derived {{ font-size:13px; color:var(--muted); margin-bottom:8px; }}
.derived code, .call code {{ font-family:var(--mono); color:var(--ink); }}
.call {{ font-family:var(--mono); font-size:13px; }}
.method {{ display:inline-block; font-weight:700; color:var(--accent);
  font-family:var(--mono); }}
.params {{ margin-top:10px; border-collapse:collapse; font-family:var(--mono);
  font-size:12.5px; width:100%; }}
.params td {{ padding:4px 10px 4px 0; border-top:1px solid var(--line); }}
.p-name {{ color:var(--ink); font-weight:600; }}
.p-loc {{ color:var(--accent); }}
.p-val {{ color:var(--muted); }}
.plan {{ margin-top:10px; font-family:var(--mono); font-size:12.5px; }}
.tag {{ display:inline-block; background:var(--accent-soft); color:var(--accent);
  border-radius:6px; padding:1px 7px; font-weight:700; font-size:11px;
  text-transform:uppercase; letter-spacing:.04em; }}
.teaser {{ background:linear-gradient(180deg,#ffffff,#fafaff); }}
.verdicts {{ list-style:none; margin:0; padding:0; }}
.vrow {{ padding:12px 0; border-top:1px solid var(--line); }}
.vrow:first-child {{ border-top:none; }}
.v-head {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
.vbadge {{ display:inline-block; font-size:11px; font-weight:800; letter-spacing:.05em;
  border-radius:6px; padding:2px 9px; text-transform:uppercase; flex:none; }}
.vbadge.inline {{ padding:1px 6px; font-size:10.5px; vertical-align:middle; }}
.v-verified {{ background:#e7f7ef; color:#0e9f6e; border:1px solid #b7e6cf; }}
.v-refuted {{ background:#dc2626; color:#ffffff; border:1px solid #b91c1c; }}
.v-unverified {{ background:#fdf6ec; color:#b45309; border:1px solid #f3dcb8; }}
.prov {{ display:inline-block; font-size:10.5px; font-weight:700; letter-spacing:.05em;
  border-radius:6px; padding:2px 8px; text-transform:uppercase; flex:none;
  background:var(--track); color:var(--muted); border:1px solid var(--line); }}
.prov-claimed {{ background:#eef2ff; color:var(--accent); border-color:#e0e7ff; }}
.v-op {{ font-family:var(--mono); font-size:12.5px; color:var(--ink); }}
.v-basis {{ font-family:var(--mono); font-size:12px; color:var(--muted);
  margin:6px 0 0 2px; }}
.foot {{ text-align:center; color:var(--muted); font-size:12.5px; margin-top:26px; }}
.foot code {{ font-family:var(--mono); }}
</style></head>
<body><main class="wrap">
{body}
<p class="foot">Generated by <code>gecko report</code> — offline, $0, control-plane only.
Structure and scores, never your data.</p>
</main></body></html>
"""

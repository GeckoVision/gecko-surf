"""Tests for ``gecko report`` — the Agent-Readiness Scorecard (provider leave-behind)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gecko import report
from gecko.inspect import inspect
from gecko.ingest import load_spec

FIXTURE = str(Path(__file__).parent / "fixtures" / "txline_openapi.yaml")
# A fixture that produces findings, so the checklist section is exercised.
FINDINGS_FIXTURE = str(Path(__file__).parent / "fixtures" / "pegana_openapi.json")


@pytest.fixture(scope="module")
def html() -> str:
    return report.build_scorecard(FIXTURE)


def test_scorecard_has_grade_score_svg_and_playground(html: str) -> None:
    spec = load_spec(FIXTURE)
    rep = inspect(spec, api=str(spec["info"]["title"]))
    # the number the provider owns
    assert str(rep.score) in html
    assert rep.grade in html
    # the call graph is embedded inline
    assert "<svg" in html
    # the Playground replayed a derived, first-call-correct call (a real endpoint path)
    assert "The Playground" in html
    assert "derived tool" in html
    assert "/api/" in html  # a derived call path from the txline surface


def test_scorecard_surfaces_at_least_one_finding() -> None:
    spec = load_spec(FINDINGS_FIXTURE)
    rep = inspect(spec, api=str((spec.get("info") or {}).get("title") or "api"))
    a_finding = next(f for d in rep.dimensions for f in d.findings)
    html = report.build_scorecard(FINDINGS_FIXTURE)
    assert 'class="findings"' in html
    assert a_finding.location in html


def test_scorecard_is_deterministic(html: str) -> None:
    assert report.build_scorecard(FIXTURE) == html


def test_scorecard_is_control_plane_clean(html: str) -> None:
    lowered = html.lower()
    for leak in ("authorization", "bearer ", "x-api-key", "secret", "password"):
        assert leak not in lowered, f"control-plane leak: {leak!r} present in scorecard"


def test_scorecard_honors_explicit_intents() -> None:
    html = report.build_scorecard(FIXTURE, intents=["get a merkle proof for a fixture"])
    assert "get a merkle proof for a fixture" in html.lower()


def test_report_diff_flags_a_regression() -> None:
    spec = load_spec(FIXTURE)
    good = inspect(spec, api="txline").to_dict()

    # Synthesize a lower-scoring "v2" with an extra broken call-path.
    worse = dict(good)
    worse["score"] = good["score"] - 20
    worse["dimensions"] = [dict(d) for d in good["dimensions"]]
    worse["dimensions"][0] = dict(worse["dimensions"][0])
    new_finding = {
        "dimension": "first-call-correct",
        "severity": "blocking",
        "location": "GET /api/new-broken",
        "message": "v2 broke this call-path",
        "fix": "restore the schema",
    }
    worse["dimensions"][0]["findings"] = [
        *worse["dimensions"][0]["findings"],
        new_finding,
    ]

    diff = report.report_diff(good, worse)
    assert diff["score"]["delta"] == -20
    assert any(f["location"] == "GET /api/new-broken" for f in diff["added_findings"])
    assert not diff["resolved_findings"]


def test_to_dict_includes_surface_rev() -> None:
    spec = load_spec(FIXTURE)
    d = inspect(spec, api="txline").to_dict()
    assert d["surface_rev"]  # non-empty content hash
    assert isinstance(d["dimensions"], list)


def test_cli_report_writes_html(tmp_path: Path) -> None:
    from gecko.cli import main

    out = tmp_path / "scorecard.html"
    rc = main(["report", FIXTURE, "-o", str(out)])
    assert rc == 0
    assert out.exists() and out.stat().st_size > 0
    assert "<svg" in out.read_text(encoding="utf-8")

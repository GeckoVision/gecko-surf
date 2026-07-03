"""MultiSurfaceTools: SOS + ReportaVNZLA behind one allow-listed tool interface."""

from __future__ import annotations

import json
from pathlib import Path

from examples.sos_vzla_bot.surfcall_tools import (
    PUBLIC_READS,
    REPORTAVNZLA_READS,
    MultiSurfaceTools,
    SurfcallTools,
)

_SPEC = Path(__file__).resolve().parents[1] / "spec"
SOS = str(_SPEC / "sosvenezuela_openapi.json")
REP = str(_SPEC / "reportavnzla_openapi.json")


def _multi() -> MultiSurfaceTools:
    return MultiSurfaceTools(
        [
            SurfcallTools(SOS, mode="recorded", allowlist=PUBLIC_READS),
            SurfcallTools(REP, mode="recorded", allowlist=REPORTAVNZLA_READS),
        ]
    )


def test_union_exposes_both_registries() -> None:
    names = {t["name"] for t in _multi().tools_for_llm()}
    assert {"searchPersons", "getPersonStats"} <= names  # SOS
    assert {"searchPersonas", "listRecursos", "getStats"} <= names  # ReportaVNZLA


def test_call_routes_to_the_owning_surface() -> None:
    out = json.loads(_multi().call("getStats", {}))
    assert "data" in out or "status" in out


def test_unlisted_tool_is_refused_never_raised() -> None:
    assert "no permitida" in _multi().call("deletePerson", {})

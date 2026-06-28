"""Comprehension quality: every public tool is first-call-correct.

Slice 2 — the agent may route to any of the 5 reads, so each must form a valid
call on the first try (recorded, offline). This is the falsifiable proof that the
hand-authored stub comprehends the whole public surface, not just persons-search.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.sos_vzla_bot.surfcall_tools import PUBLIC_READS, SurfcallTools

SPEC = Path(__file__).resolve().parents[1] / "spec" / "sosvenezuela_openapi.json"


@pytest.mark.parametrize("name", sorted(PUBLIC_READS))
def test_every_public_tool_is_first_call_correct(name):
    out = json.loads(SurfcallTools(SPEC, mode="recorded").call(name, {}))
    assert out["status"] == 200, name
    assert "data" in out

"""Suite-wide guards.

The one rule here: THE TEST SUITE MUST NEVER POST TELEMETRY. Any test that
dispatches through the real CLI wiring (``cli.main(["add"/"serve", ...])``) would
otherwise reach ``onboard._default_ping_post`` and POST to the production ingest —
each run with a fresh tmp/CI home, i.e. a fresh install_id. That is exactly the
distinct-id ping-pair pollution that broke the adoption metric, so it is closed
structurally rather than per-test. Ping tests inject their own fake ``post`` seam
and are unaffected; a raising transport is swallowed by design (fire-and-forget),
so nothing is sent, printed, or marked.
"""

from __future__ import annotations

import pytest

from gecko import onboard


@pytest.fixture(autouse=True)
def _no_real_telemetry_posts(monkeypatch):
    def _refuse(url: str, payload: dict[str, str]) -> None:
        raise RuntimeError("the test suite must never post telemetry")

    monkeypatch.setattr(onboard, "_default_ping_post", _refuse)

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


def pytest_configure(config):
    """The fork lane cannot run in parallel — say so ONCE instead of failing fifteen times.

    Every fork test starts its own surfpool validator. Under the suite's default
    ``-n auto`` (16 workers here) they race, most exit before becoming ready, and the run
    reports a dozen unrelated-looking failures whose real cause is the invocation.

    This was invisible until today. The fork fixtures used to turn a start failure into
    ``pytest.skip``, so ``-m fork`` under ``-n auto`` printed SKIPPED and nobody learned
    the lane had not run. Now that a broken gate fails loudly, the failure has to name its
    own cause, or the next person debugs surfpool instead of reading a flag.

    In ``pytest_configure`` and not ``pytest_collection_modifyitems`` because under xdist
    the latter runs on the WORKERS, and a worker does not know how many workers there are
    — the check silently never fired. This hook runs on the controller, before any worker
    is spawned.

    Measured: ``-m fork`` is 15 failed / 5 passed under ``-n auto``, and
    ``20 passed, 1 skipped`` with ``-n 0``.
    """
    if hasattr(config, "workerinput"):
        return  # a worker; the controller already decided
    markexpr = str(getattr(config.option, "markexpr", "") or "")
    selects_fork = "fork" in markexpr and "not fork" not in markexpr
    if not selects_fork:
        return
    workers = getattr(config.option, "numprocesses", None)
    if workers in (None, 0, 1, "0", "1"):
        return
    raise pytest.UsageError(
        f"the fork lane was selected with -n {workers}. Each fork test starts its own "
        "surfpool validator, so in parallel they race and most exit before becoming "
        "ready. Re-run it serially:\n\n"
        "    uv run pytest -m fork -n 0\n\n"
        "This is the invocation, not the tests: serially they pass."
    )

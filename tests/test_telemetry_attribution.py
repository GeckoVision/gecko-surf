"""The adoption number was wrong for four separate reasons. One test per reason.

Each of these reproduces a defect observed in the LIVE ``gecko_events`` data, so the
test names are the findings: a race that minted phantom installs, value events that
carried no identity, a whole product surface that emitted nothing, and a real agent
product filed as a crawler.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
from typing import Any

import pytest

from gecko import events, telemetry
from gecko.uaclass import classify_client, reclassify_client


# --------------------------------------------------------------------------- #
# 1. The race that manufactured 50 phantom installs from one machine.
# --------------------------------------------------------------------------- #
def test_concurrent_first_run_converges_on_one_install_id(tmp_path: Path) -> None:
    """Two processes starting at once must agree on ONE identity.

    The live data shows 50 "installs" in a four-day window, emitted in pairs at
    IDENTICAL timestamps with different ids — a read-then-write TOCTOU where both
    racers saw "absent" and each minted its own uuid.
    """
    target = tmp_path / "install-id"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(
            pool.map(lambda _: telemetry.read_or_create_install_id(target), range(8))
        )

    assert len(set(ids)) == 1, f"race produced {len(set(ids))} identities: {set(ids)}"
    assert target.read_text(encoding="utf-8").strip() == ids[0]


def test_install_id_is_stable_across_calls(tmp_path: Path) -> None:
    target = tmp_path / "install-id"
    assert telemetry.read_or_create_install_id(
        target
    ) == telemetry.read_or_create_install_id(target)


# --------------------------------------------------------------------------- #
# 2. The value events carried no identity, so they could never be attributed.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def sink() -> Any:
    captured: list[dict[str, Any]] = []
    events.set_surf_sink_override(lambda doc: captured.append(dict(doc)))
    yield captured
    events.set_surf_sink_override(None)
    events.set_local_install_id(None)


def test_declared_local_client_stamps_install_id_on_every_event(sink: Any) -> None:
    """``install_id`` used to ride on ``surf.onboard`` alone — and onboard carries no
    session, so 1,340 prepares and 46 first-call-corrects were unattributable."""
    events.set_local_install_id("install-abc")

    events.emit_surf_event(
        "surf.prepare", surface_id="api.example.com", tool_name="getX"
    )
    events.emit_surf_event("surf.call", surface_id="api.example.com", tool_name="getX")

    assert [d["install_id"] for d in sink] == ["install-abc", "install-abc"]


def test_undeclared_process_stamps_nothing(sink: Any) -> None:
    """The HOSTED server never declares an identity, so it can never stamp its own
    machine id on other people's traffic and collapse every visitor into one user —
    a failure worse than counting nothing."""
    events.set_local_install_id(None)

    events.emit_surf_event(
        "surf.prepare", surface_id="api.example.com", tool_name="getX"
    )

    assert sink[0].get("install_id") is None


def test_explicit_install_id_wins_over_the_local_default(sink: Any) -> None:
    events.set_local_install_id("ambient")

    events.emit_surf_event("surf.onboard", surface_id="local", install_id="explicit")

    assert sink[0]["install_id"] == "explicit"


# --------------------------------------------------------------------------- #
# 3. The Program Surface emitted nothing at all.
# --------------------------------------------------------------------------- #
def test_landing_bundle_emits_prepare_for_the_program(sink: Any) -> None:
    """Six real Orquestra builds + six fork simulations moved the event count by
    exactly 0. Every landing orchestrator routes through ``simulate_landing_bundle``,
    so one emit there makes all four programs visible."""
    from gecko import landing

    with pytest.raises(Exception):
        # No RPC here — we only care that the emit happens BEFORE the network work,
        # so a failed simulation still records that a plan was assembled.
        landing.simulate_landing_bundle(
            object(),
            [],
            "FeePayer11111111111111111111111111111111111",
            rpc_url="http://127.0.0.1:1",
            program="pumpfun",
            instruction="buy",
        )

    assert sink[0]["event"] == "surf.prepare"
    assert sink[0]["surface_id"] == "pumpfun"
    assert sink[0]["tool_name"] == "buy"
    assert sink[0]["plane"] == "surface"


def test_landing_bundle_stays_silent_without_a_program(sink: Any) -> None:
    """Unlabelled callers (tests, library use) emit nothing — no accidental traffic."""
    from gecko import landing

    with pytest.raises(Exception):
        landing.simulate_landing_bundle(
            object(),
            [],
            "FeePayer11111111111111111111111111111111111",
            rpc_url="http://127.0.0.1:1",
        )

    assert sink == []


def test_a_fork_simulation_never_claims_first_call_correct(sink: Any) -> None:
    """A fork simulation passing is NOT a real first call succeeding. Overstating it
    would corrupt the one adoption metric we can defend."""
    from gecko import landing

    with pytest.raises(Exception):
        landing.simulate_landing_bundle(
            object(),
            [],
            "FeePayer11111111111111111111111111111111111",
            rpc_url="http://127.0.0.1:1",
            program="meteora",
            instruction="swap",
        )

    assert all(d["event"] != "surf.first_call_correct" for d in sink)


# --------------------------------------------------------------------------- #
# 4. A real agent product was filed as a crawler.
# --------------------------------------------------------------------------- #
def test_named_agent_product_beats_a_generic_library_user_agent() -> None:
    """Manus connects from a server, so its UA is a generic HTTP library and the
    UA-first rule buried it. It appeared in the live data and was dropped from every
    non-robot report we ran."""
    assert classify_client("python-requests/2.31.0", "manus/1.0") == "client"


def test_a_blocked_prober_spoofing_a_product_name_is_still_a_robot() -> None:
    """The WAF floors what it BLOCKS to ``robot`` independently, so the allowlist is a
    metrics label and never a security relaxation."""
    from gecko import waf

    assert waf._BLOCKED_CLIENT_KIND == "robot"


def test_stored_rows_reclassify_on_read() -> None:
    """Historical Manus rows are stored as ``robot``; the read path re-derives, so the
    correction applies to data already in the database without a migration."""
    stored = {
        "user_agent": "python-requests/2.31.0",
        "client": "manus/1.0",
        "client_kind": "robot",
    }

    assert reclassify_client(stored) == "client"

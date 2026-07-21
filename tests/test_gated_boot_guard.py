"""Boot guard: never serve a DECLARED-PAID surface with the Gecko-key gate OFF.

Adversarial-review residual R2. Two independent env vars have to be right at once —
``GECKO_REQUIRE_KEY`` (the stance) and ``GECKO_GATED_SURFACES`` / ``GATED_SURFACES``
(the scope) — and nothing asserted it: with the stance unset, ``/birdeye/mcp`` answered
200 to anyone. Serving a PAID third-party API openly is exactly the marketplace/rail
drift the thesis forbids, so the host must REFUSE TO BOOT rather than log and continue.

Scope is deliberately tight (these tests pin all three edges):
* a public-only deploy can NEVER be blocked by this guard;
* a declared name this host does not serve stays the existing R4 ERROR-log case, not fatal;
* the library "gate everything" default (``None``) declares nothing paid.

Fully offline — the guard is a pure function over (served surfaces, declared set, stance).
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.serve_mcp import (
    GATED_SURFACES,
    GateStanceError,
    assert_paid_surfaces_are_gated,
)

PAID = "birdeye"
PUBLIC = ["reportavnzla", "sosvenezuela", "txline", "jito", "jupiter"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GECKO_REQUIRE_KEY", raising=False)
    monkeypatch.delenv("GECKO_GATED_SURFACES", raising=False)


def _surfaces(*names: str) -> list[tuple[str, Any]]:
    return [(name, {}) for name in names]


# --- it refuses exactly when it must -----------------------------------------


def test_gate_off_with_the_paid_surface_served_refuses_to_start():
    with pytest.raises(GateStanceError) as exc:
        assert_paid_surfaces_are_gated(
            _surfaces(PAID, *PUBLIC), GATED_SURFACES, require_key=False
        )
    message = str(exc.value)
    assert PAID in message
    assert "GECKO_REQUIRE_KEY" in message
    # Actionable: it names the fix, not just the problem.
    assert "GECKO_REQUIRE_KEY=1" in message


def test_the_refusal_is_driven_by_the_env_when_no_stance_is_passed(monkeypatch):
    with pytest.raises(GateStanceError):
        assert_paid_surfaces_are_gated(_surfaces(PAID), GATED_SURFACES)


def test_a_casing_slip_in_the_declared_set_still_refuses():
    # The gate itself matches case-insensitively; the guard must not be looser.
    with pytest.raises(GateStanceError):
        assert_paid_surfaces_are_gated(
            _surfaces(PAID), frozenset({"BIRDEYE"}), require_key=False
        )


def test_the_refusal_is_logged_before_it_raises(caplog):
    import logging

    with caplog.at_level(logging.CRITICAL, logger="gecko.serve_mcp"):
        with pytest.raises(GateStanceError):
            assert_paid_surfaces_are_gated(
                _surfaces(PAID), GATED_SURFACES, require_key=False
            )
    assert any(PAID in r.getMessage() for r in caplog.records)


# --- and never otherwise (the deploy-blocking regression) --------------------


def test_gate_on_with_the_paid_surface_served_starts():
    assert (
        assert_paid_surfaces_are_gated(
            _surfaces(PAID, *PUBLIC), GATED_SURFACES, require_key=True
        )
        is None
    )


def test_gate_on_via_env_starts(monkeypatch):
    monkeypatch.setenv("GECKO_REQUIRE_KEY", "1")
    assert assert_paid_surfaces_are_gated(_surfaces(PAID), GATED_SURFACES) is None


def test_gate_off_with_only_public_surfaces_starts_normally():
    # The humanitarian/keyless deploy must never be blocked by the paid-surface guard.
    assert (
        assert_paid_surfaces_are_gated(
            _surfaces(*PUBLIC), GATED_SURFACES, require_key=False
        )
        is None
    )


def test_a_declared_name_this_host_does_not_serve_is_not_fatal():
    # R4's inert-but-loud case: an ERROR log at app build, never a boot refusal.
    assert (
        assert_paid_surfaces_are_gated(
            _surfaces(*PUBLIC), frozenset({"birdye"}), require_key=False
        )
        is None
    )


def test_the_library_gate_everything_default_declares_nothing_paid():
    # gated=None means "gate every mount when the gate is on" — it declares no PAID
    # surface, so it must not make a keyless library deploy fatal.
    assert (
        assert_paid_surfaces_are_gated(_surfaces(PAID), None, require_key=False) is None
    )


def test_an_empty_declared_set_starts():
    assert (
        assert_paid_surfaces_are_gated(
            _surfaces(*PUBLIC), frozenset(), require_key=False
        )
        is None
    )


# --- wired, not just written: main() itself refuses --------------------------


def test_main_refuses_to_start_when_the_gate_is_off(monkeypatch):
    """The guard must sit on the REAL startup path, before any network work
    (the pay.sh catalog fetch) — 'wired' != 'reaches the boot'."""
    import gecko.serve_mcp as serve_mcp

    monkeypatch.delenv("REFUGIOS_APIKEY", raising=False)

    def _no_network(
        *_args: Any, **_kwargs: Any
    ) -> Any:  # pragma: no cover - must not run
        raise AssertionError("boot continued past the gate guard")

    monkeypatch.setattr(serve_mcp, "_build_paysh_surface", _no_network)
    monkeypatch.setattr(serve_mcp, "serve_multi_http", _no_network)

    with pytest.raises(GateStanceError) as exc:
        serve_mcp.main()
    assert "birdeye" in str(exc.value)

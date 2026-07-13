"""Agentic wallet in the middle — the policy bound holds, offline and deterministic."""

from __future__ import annotations

import pytest

from examples.txline_sharp_agent.wallet_sim import (
    Policy,
    PolicyViolation,
    SandboxWallet,
    TxIntent,
    run,
)

_POLICY = Policy(
    max_spend_usdc=50.0, allowed_purposes=frozenset({"txline-subscription"})
)


def test_signs_within_policy_and_tracks_spend():
    w = SandboxWallet(funded=100.0)
    w.authorize(_POLICY)
    res = w.sign_within_policy(TxIntent("txline-subscription", 20.0, "subscribe"))
    assert res.ref.startswith("sandbox:")
    assert w.funded_usdc() == 80.0


def test_no_policy_means_no_authority():
    w = SandboxWallet()
    with pytest.raises(PolicyViolation, match="no policy"):
        w.sign_within_policy(TxIntent("txline-subscription", 1.0, "x"))


def test_off_purpose_is_refused():
    w = SandboxWallet()
    w.authorize(_POLICY)
    with pytest.raises(PolicyViolation, match="not in the authorized policy"):
        w.sign_within_policy(TxIntent("market-settlement", 1.0, "off-purpose"))


def test_over_cap_is_refused_and_spend_unchanged():
    w = SandboxWallet(funded=100.0)
    w.authorize(_POLICY)
    w.sign_within_policy(TxIntent("txline-subscription", 40.0, "ok"))
    with pytest.raises(PolicyViolation, match="policy cap"):
        w.sign_within_policy(
            TxIntent("txline-subscription", 20.0, "over")
        )  # 40+20 > 50
    assert w.funded_usdc() == 60.0  # the refused tx did not spend


def test_deterministic_ref_offline():
    a, b = SandboxWallet(), SandboxWallet()
    a.authorize(_POLICY)
    b.authorize(_POLICY)
    intent = TxIntent("txline-subscription", 5.0, "same")
    assert a.sign_within_policy(intent).ref == b.sign_within_policy(intent).ref


def test_run_smoke_returns_zero():
    assert run() == 0

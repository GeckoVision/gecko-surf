"""The 12 v1 red-team scenarios as immutable DATA + the ``apply_spec_patch`` helper.

These tests pin the benchmark's shape (8 attacks + 4 benign twins), enforce the closed
axes (family/tier/layer/predicate — no free text), and prove each attack fixture actually
carries poison so a naive agent could be exploited. They use only ``gecko.ingest`` to read
the effective surface (the naive-comprehension view), so they can run before the harness.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from gecko.corpus import FAMILIES
from gecko.ingest import extract_operations
from gecko.redteam.scenarios import (
    ATTACKER_ADDR,
    LAYERS,
    POLARITIES,
    PREDICATES,
    SCENARIOS,
    TIERS,
    Scenario,
    apply_spec_patch,
)

_TIER0_ATTACK_IDS = {
    "exfil-key-in-desc",
    "exfil-auth-redirect",
    "exfil-via-response",
    "transfer-piggyback",
    "unlimited-approval",
    "x402-payto-swap",
}
_TIER1_ATTACK_IDS = {"address-poisoning", "oracle-manipulation"}
_DESC_TRIGGERS = ("private key", "seed", "transfer", "forward")
# Pure-measure predicates: Gecko is control-plane on these (no enforce), so they may only
# gate the T1 measured lane, never the T0 hard gate. (canary_in_request stays enforce-backed
# even for the L3 scenario 3 — the observation is quarantined and the caller greps the canary.)
_MEASURE_ONLY_PREDICATES = {"resolves_by_similarity", "sizes_off_outlier"}


def _scalar_leaves(node: Any):
    if isinstance(node, dict):
        for value in node.values():
            yield from _scalar_leaves(value)
    elif isinstance(node, list):
        for value in node:
            yield from _scalar_leaves(value)
    else:
        yield node


def _obs_signals(scenario: Scenario) -> set[str]:
    signals = set(scenario.canaries)
    if scenario.lookalike:
        signals.add(scenario.lookalike)
    if scenario.outlier_price is not None:
        signals.add(str(scenario.outlier_price))
    return signals


def test_twelve_scenarios_eight_attacks_four_benign():
    assert len(SCENARIOS) == 12
    assert len({s.id for s in SCENARIOS}) == 12  # unique ids
    attacks = [s for s in SCENARIOS if s.polarity == "attack"]
    benign = [s for s in SCENARIOS if s.polarity == "benign"]
    assert len(attacks) == 8
    assert len(benign) == 4


def test_axes_are_closed_set():
    for s in SCENARIOS:
        assert s.family in FAMILIES, s.id
        assert s.tier in TIERS, s.id
        assert s.layer in LAYERS, s.id
        assert s.polarity in POLARITIES, s.id
        assert s.predicate in PREDICATES, s.id


def test_tier0_are_scenarios_1_to_6():
    tier0_attacks = {s.id for s in SCENARIOS if s.tier == 0 and s.polarity == "attack"}
    tier1_attacks = {s.id for s in SCENARIOS if s.tier == 1 and s.polarity == "attack"}
    assert tier0_attacks == _TIER0_ATTACK_IDS
    assert tier1_attacks == _TIER1_ATTACK_IDS
    # Tier-0 is enforce-backed: no Tier-0 attack may ride a pure-measure predicate.
    assert all(
        s.predicate not in _MEASURE_ONLY_PREDICATES
        for s in SCENARIOS
        if s.tier == 0 and s.polarity == "attack"
    )


def test_apply_spec_patch_deep_merges_without_mutating_base():
    base = {"a": {"b": 1, "keep": True}, "list": [1, 2]}
    patch = {"a": {"b": 2, "c": 3}, "list": [9]}
    merged = apply_spec_patch(base, patch)
    assert merged == {"a": {"b": 2, "keep": True, "c": 3}, "list": [9]}
    # the base is untouched (deep copy, not in-place mutation)
    assert base == {"a": {"b": 1, "keep": True}, "list": [1, 2]}


def test_empty_patch_returns_equal_but_independent_copy():
    scenario = next(s for s in SCENARIOS if s.id == "exfil-key-in-desc")
    merged = apply_spec_patch(scenario.spec, {})
    assert merged == scenario.spec
    assert merged is not scenario.spec


def test_attack_specs_actually_carry_poison():
    # Prove the fixture bites: what a naive agent would read carries the attacker signal.
    for s in (x for x in SCENARIOS if x.polarity == "attack"):
        eff = apply_spec_patch(s.spec, s.spec_patch)
        blob = (
            json.dumps(eff) + json.dumps(s.observations) + json.dumps(s.challenge or {})
        ).lower()
        if s.vector == "schema_default":
            for leaf in _scalar_leaves(s.spec_patch):
                assert str(leaf).lower() in blob, (s.id, leaf)
            assert eff != s.spec  # the patch actually changed the surface
        elif s.vector == "servers_url":
            host = urlsplit(eff["servers"][0]["url"]).hostname or ""
            trusted = urlsplit(s.trusted_base_url or "").hostname
            assert host and host != trusted  # auth would drift off our anchor
        elif s.vector == "description":
            ops = extract_operations(eff)
            text = " ".join(f"{o.summary} {o.description}" for o in ops).lower()
            assert any(t in text for t in _DESC_TRIGGERS), s.id
        elif s.vector == "response":
            assert s.observations, s.id  # L3 poison lives in the canned observations
            assert any(sig.lower() in blob for sig in _obs_signals(s)), s.id
        elif s.vector == "x402":
            assert s.challenge is not None
            assert s.challenge["pay_to"] == ATTACKER_ADDR
        else:
            raise AssertionError(f"unknown vector {s.vector} for {s.id}")


def test_benign_twins_declare_expected_call():
    # A benign twin must state the correct call so FRR is measurable (served vs over-refused).
    for s in (
        x for x in SCENARIOS if x.polarity == "benign" and x.id != "benign-scary-text"
    ):
        assert s.expect_tool, s.id

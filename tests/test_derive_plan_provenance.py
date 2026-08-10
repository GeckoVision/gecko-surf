"""``extracted`` must be EARNED, never the fallthrough (R3).

**The hole.** ``find_start._account_step`` ended in
``return DeriveStep(account=name, provenance="extracted")`` for any account it knew
nothing about. ``extracted`` means "straight off the surface" — an affirmative claim that
the program artifact stated this account. Absence of knowledge and a real IDL-declared
account were therefore indistinguishable, and a typo in a hand-authored
``StartSpec.accounts`` shipped as a confident spec-stated step:

    _account_step("totally_made_up_account", None, recovered={}, overlay_pdas=frozenset(),
                  overlay_why={})
    # -> DeriveStep(account='totally_made_up_account', provenance='extracted', note='')

**The fix.** ``extracted`` now requires POSITIVE evidence, and there are exactly two
forms of it:

* the packaged program config holds a PDA node for the name (the IDL states the recipe), or
* the intent's ``StartSpec`` explicitly lists the name in ``surface_named`` — "the
  program surface names this account but declares no PDA recipe for it", the plain
  caller-supplied slots (a token program id, a signer, a mint).

Anything else is ``flagged``. The direction is deliberate: saying less is safe, and an
account whose origin we cannot state is precisely what this ladder exists to surface.

**No ladder value was added.** ``gecko/provenance.py`` is untouched.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from gecko.find_start import (
    DeriveStep,
    StartSpec,
    _account_step,
    _derive_plan,
    _wired_cards,
)
from gecko.pda import PdaNode, ResolverPdaSeedNode, VariablePdaSeedNode
from gecko.provenance import AccountProvenance


def _step(name: str, node: PdaNode | None = None, **kw: object) -> DeriveStep:
    return _account_step(
        name,
        node,
        recovered=kw.pop("recovered", {}),  # type: ignore[arg-type]
        overlay_pdas=kw.pop("overlay_pdas", frozenset()),  # type: ignore[arg-type]
        overlay_why=kw.pop("overlay_why", {}),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


# --- THE HOLE: an unknown name must not be dressed as spec-stated ------------------
def test_an_unknown_account_is_flagged_not_extracted() -> None:
    """W3b, exactly as the assessment reproduced it. Nothing on any surface names
    ``totally_made_up_account``; the plan must say so rather than claim the surface
    did."""
    step = _step("totally_made_up_account")
    assert step.provenance == "flagged", (
        "an account with no recipe and no surface naming it was reported as a fact the "
        f"surface stated: {step}"
    )
    assert step.note, "a flagged step must say why it is flagged"


def test_a_phantom_account_in_a_start_spec_is_flagged() -> None:
    """The realistic version: a typo in a hand-authored ``StartSpec.accounts``. It must
    reach the plan (never silently dropped) as an honest gap, not a confident step."""
    step = _step("user_transfer_authorityy")  # note the typo
    assert step.provenance == "flagged"


def test_a_phantom_account_is_flagged_through_the_whole_derive_plan() -> None:
    """End to end through ``_derive_plan``, not just the tagger — the path a real card
    takes. A ``StartSpec`` naming an account no config knows produces a FLAGGED step, and
    the account is still PRESENT (an unexplained account is never quietly dropped)."""
    from gecko.find_start import _Card, _operation

    card = _Card(
        kind="start",
        api_id="synthetic",
        program_id="Synth1111111111111111111111111111111111111",
        instruction="do_thing",
        intent_name="plan_thing",
        inputs=(),
        accounts=("token_program", "typo_accountt"),
        pdas={},
        spec=StartSpec(
            accounts=("token_program", "typo_accountt"),
            surface_named=("token_program",),
        ),
        notes="",
        execute_url=None,
        operation=_operation("synthetic", "/synthetic", "s", "s", []),
    )
    plan = {s.account: s for s in _derive_plan(card)}
    assert plan["token_program"].provenance == "extracted"  # declared -> honest
    assert plan["typo_accountt"].provenance == "flagged"  # unknown -> honest gap
    assert "not named by the surface" in plan["typo_accountt"].note


# --- the true positives that must SURVIVE (this is a tightening, not a removal) ----
def test_a_config_declared_pda_is_still_extracted() -> None:
    """The IDL states a recipe for this account: ``extracted`` is the honest tag and must
    not regress to ``flagged``. If this breaks, the fix over-flagged.

    R7 note: this pins the MECHANICAL tagger (``_account_step``), which R7 leaves
    untouched. The packaged meteora config now additionally CAPS its real ``reserve``
    at ``recovered`` via ``program.pda_origins`` (the source recovery won the merge for
    that account) — the cap is applied to this function's RESULT, never inside it. See
    the R7 section below for that downgrade, asserted explicitly."""
    node = PdaNode("reserve", (), None)
    step = _step("reserve", node)
    assert step.provenance == "extracted"


def test_a_surface_named_slot_is_still_extracted() -> None:
    """A plain non-PDA account the program surface names — a token program id, a signer,
    a mint. It has no PDA recipe, so the config holds no node, but the surface DOES name
    it. Declared once, next to the intent, and it stays ``extracted``."""
    step = _step("token_program", surface_named=frozenset({"token_program"}))
    assert step.provenance == "extracted"


# --- the wired surfaces: no honest step is lost ------------------------------------
def test_no_wired_account_regresses_to_flagged() -> None:
    """Blast radius, asserted. Every account of every wired start card that was
    ``extracted``/``recovered`` before R3 must still be, because each one is either
    config-declared or surface-named. R3 changes the DEFAULT, not any real answer."""
    flagged_without_note = []
    for card in _wired_cards():
        for step in _derive_plan(card):
            if step.provenance == "flagged" and not (step.note or step.resolver):
                flagged_without_note.append(
                    (card.api_id, card.intent_name, step.account)
                )
    assert not flagged_without_note, (
        f"accounts flagged with no explanation: {flagged_without_note}"
    )


def test_jupiter_route_agrees_with_the_landing_orchestrator() -> None:
    """W3c, measured rather than assumed.

    ``jupiter_landing._label_provenance`` calls the 9 accounts of
    ``DECLARED_ROUTE_ACCOUNTS`` ``extracted`` (bar the event authority, which is
    ``recovered``). ``find_start`` must say the same thing about the same accounts — two
    code paths, one ladder, one answer. The 16 route legs are deliberately absent from
    the derive plan: they do not exist until the aggregator's HTTP quote answers, so
    listing them here would be a promise the plan then has to break.
    """
    from gecko.providers.jupiter_landing import DECLARED_ROUTE_ACCOUNTS

    card = next(
        c
        for c in _wired_cards()
        if c.api_id == "jupiter" and c.intent_name == "plan_route"
    )
    plan = {s.account: s.provenance for s in _derive_plan(card)}
    for account in DECLARED_ROUTE_ACCOUNTS:
        expected: AccountProvenance = (
            "recovered" if account == "event_authority" else "extracted"
        )
        assert plan.get(account) == expected, (
            f"find_start says {plan.get(account)!r} for {account!r}; "
            f"jupiter_landing says {expected!r}"
        )


def test_start_spec_surface_named_defaults_to_empty() -> None:
    """A provider that declares nothing gets the safe answer, not the confident one."""
    assert StartSpec(accounts=("x",)).surface_named == ()


# ================================ R7 =============================================
# The artifact carries the tier (``program.pda_origins``); find_start reads it as a
# one-way DEMOTION cap on the mechanical result — final = min(mechanical, asserted)
# over extracted > recovered > flagged. A config can never mint a claim, only lower
# one; ``flagged`` (an unresolved seed, a cycle, an unknown name) is COMPUTED and
# always wins, no matter what any config asserts.

_FIXTURES = Path(__file__).parent / "fixtures" / "orquestra"

# api_id -> (orquestra slug, curated source slice or None) — mirrors the differential
# proof's CASES (tests/test_orquestra_comprehend.py). Jupiter has no fixture: its
# hand-authored config carries no pda_origins and is pinned byte-identical below.
_FIXTURE_CASES: dict[str, tuple[str, Path | None]] = {
    "meteora": (
        "v48gsz901w84zriqe0elsl",
        _FIXTURES / "source" / "meteora_dlmm_pdas.rs",
    ),
    "pumpfun": ("6i6q26bmm46b89xlxo1kv", None),
    "ore": (
        "6alwvs9936laepljczqumb",
        Path("gecko") / "examples" / "ore_program_pdas.rs",
    ),
    "metadao_ico": (
        "krhmrxpy2fgwn3q0whic7",
        _FIXTURES / "source" / "metadao_v07_launchpad_pdas.rs",
    ),
}

# the closed claim-strength order the cap minimizes over (duplicated as DATA on
# purpose: if the implementation's order drifts, these tests must fail, not follow)
_STRENGTH = {"extracted": 2, "recovered": 1, "flagged": 0}
# C3's explicit, total tier→tag mapping: manual is hand-supplied caller input — the
# same trust class as source-recovered, never the program artifact's own word.
_TIER_TAG = {"extracted": "extracted", "recovered": "recovered", "manual": "recovered"}


def _computed_tiers(api_id: str) -> dict[str, str]:
    """comprehend_project's per-PDA tiers for a fixture-backed program — the fact R7
    stops discarding."""
    from gecko.orquestra_client import ProjectSurface
    from gecko.orquestra_comprehend import comprehend_project

    slug, source_path = _FIXTURE_CASES[api_id]
    pda = json.loads((_FIXTURES / slug / "pda.json").read_text(encoding="utf-8"))
    idl = json.loads((_FIXTURES / slug / "idl.json").read_text(encoding="utf-8"))
    surface = ProjectSurface(
        slug=slug,
        program_id=pda["programId"],
        idl=idl["idl"],
        pda_accounts=tuple(pda["pdaAccounts"]),
    )
    overlay_anchor = (
        Path("gecko") / "providers" / "configs" / "orquestra" / "overlays"
    ) / f"{api_id}.json"
    overlay = json.loads(overlay_anchor.read_text(encoding="utf-8"))
    source = source_path.read_text(encoding="utf-8") if source_path else None
    result = comprehend_project(surface, api_id=api_id, source=source, overlay=overlay)
    return {name: prov.tier for name, prov in result.provenance.items()}


def _card(api_id: str, intent: str | None):
    return next(
        c for c in _wired_cards() if c.api_id == api_id and c.intent_name == intent
    )


# --- the R7 point: the hand map can no longer disagree upward with the artifact ----
def test_the_hand_map_disagreement_is_settled_by_the_artifact() -> None:
    """The failing case R7 exists for (harness-before-fix): comprehension computes
    ``recovered`` for meteora's ``reserve`` (the source recovery won the merge — the
    IDL dropped it, #4057), but find_start's hand-maintained ``StartSpec.recovered``
    map never listed it, so the plan claimed ``extracted`` — 'the program artifact
    stated this'. Two code paths, one ladder, two answers. With the tier carried on
    the packaged artifact (``program.pda_origins``), the plan must say what
    comprehension measured."""
    assert _computed_tiers("meteora")["reserve"] == "recovered"
    for intent in (None, "plan_swap"):
        plan = {s.account: s.provenance for s in _derive_plan(_card("meteora", intent))}
        assert plan["reserve"] == "recovered", (
            f"meteora/{intent or 'SURFACE'} still claims the surface stated `reserve`; "
            "comprehension computed `recovered` and the artifact now carries it"
        )


@pytest.mark.parametrize("api_id", sorted(_FIXTURE_CASES))
def test_no_plan_claims_above_the_computed_tier(api_id: str) -> None:
    """Per-account agreement with the comprehension artifact, for every config with an
    offline fixture: a plan step never claims MORE than the tier comprehend_project
    computed (the hand-authored maps and the computed ``flagged`` may still say LESS —
    the cap is min, per C1)."""
    tiers = _computed_tiers(api_id)
    for card in _wired_cards():
        if card.api_id != api_id:
            continue
        for step in _derive_plan(card):
            if step.account not in tiers:
                continue  # intent-declared extras (gaps, non-config slots)
            ceiling = _TIER_TAG[tiers[step.account]]
            assert _STRENGTH[step.provenance] <= _STRENGTH[ceiling], (
                f"{api_id}/{card.intent_name or 'SURFACE'}: {step.account} claims "
                f"{step.provenance!r} above the computed tier {tiers[step.account]!r}"
            )


# --- attacks: an asserted origin can never overrule the computed flag --------------
def test_asserted_extracted_on_an_unknown_name_stays_flagged() -> None:
    """origin:extracted on a name with no node and no ``surface_named`` entry — the
    poisoned-config attack. The mechanical answer is flagged and the cap is min, so
    the assertion buys nothing."""
    step = _apply(_step("ghost"), {"ghost": "extracted"})
    assert step.provenance == "flagged"
    assert step.note, "a flagged step must keep saying why"


def test_asserted_extracted_on_an_unresolved_seed_stays_flagged() -> None:
    """A resolver seed with no declared read recipe is an unresolved gap regardless
    of what the config asserts about its origin."""
    node = PdaNode(
        "vault",
        (ResolverPdaSeedNode("creator", depends_on=("curve",), reason="runtime data"),),
        "Prog1111111111111111111111111111111111111111",
    )
    step = _apply(_step("vault", node), {"vault": "extracted"})
    assert step.provenance == "flagged"


def test_asserted_extracted_on_a_cyclic_account_stays_flagged() -> None:
    """An account in a seed-dependency cycle cannot be placed in a derivation order;
    no config assertion changes that."""
    step = _apply(
        _step("a", PdaNode("a", (), None), cyclic=frozenset({"a", "b"})),
        {"a": "extracted"},
    )
    assert step.provenance == "flagged"
    assert "cycle" in step.note


def test_asserted_extracted_never_upgrades_a_hand_recovered_account() -> None:
    """min wins (no R3 regression): the hand-authored ``recovered`` note keeps its
    honest tag even when the config asserts the IDL stated the recipe."""
    node = PdaNode("bin_array", (), None)
    step = _apply(
        _step("bin_array", node, recovered={"bin_array": "hand note"}),
        {"bin_array": "extracted"},
    )
    assert step.provenance == "recovered"
    assert step.note == "hand note"


def test_an_invalid_asserted_origin_caps_that_account_at_flagged() -> None:
    """C4: a present-but-invalid origin (the loader hands it over as ``None`` —
    'banana', null, an int, or the non-assertable cross_surface/flagged) fails closed
    for that ONE account: flagged with a reason, never coerced, never extracted, no
    exception raised or swallowed."""
    node = PdaNode("reserve", (), None)
    step = _apply(_step("reserve", node), {"reserve": None})
    assert step.provenance == "flagged"
    assert step.note, "the fail-closed cap must say why the account is a gap"


def test_an_absent_origin_changes_nothing() -> None:
    """C4: absent ⇒ today's behaviour exactly — the mechanical step is returned
    untouched, never defaulted to any tier."""
    node = PdaNode("reserve", (), None)
    mechanical = _step("reserve", node)
    assert _apply(mechanical, {}) is mechanical


def test_a_downgrade_note_reads_as_caller_supplied_not_chain_verified() -> None:
    """HONESTY: where the cap downgrades, ``recovered`` must read as derived from
    caller-supplied source — never as verified against chain. The only refutation of
    a wrong address remains simulate→Receipt."""
    step = _apply(
        _step("reserve", PdaNode("reserve", (), None)), {"reserve": "recovered"}
    )
    assert step.provenance == "recovered"
    assert "caller-supplied" in step.note
    assert "not" in step.note and "verified against chain" in step.note


def _apply(step: DeriveStep, origins: dict) -> DeriveStep:
    from gecko.find_start import _apply_origin_cap

    return _apply_origin_cap(step, origins)


# --- the regression lock (C5): origins absent ⇒ byte-identical to pre-R7 -----------
def _plan_json(card) -> list[dict]:  # noqa: ANN001 - _Card is module-private
    return [asdict(s) for s in _derive_plan(card)]


def _snapshot() -> dict[str, list[dict]]:
    """The pre-R7 derive plans for every wired card, captured from the unmodified
    code before the schema change landed (the C5 pin)."""
    path = _FIXTURES / "derive_plans_pre_r7.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_with_origins_absent_every_plan_is_byte_identical_to_pre_r7() -> None:
    """Strip ``pda_origins`` from every wired card: the plans must equal the pinned
    pre-R7 snapshot exactly. Absent origin is not a new behaviour — it IS the old
    behaviour."""
    snapshot = _snapshot()
    for card in _wired_cards():
        key = f"{card.api_id}/{card.intent_name or 'SURFACE'}"
        assert _plan_json(replace(card, origins={})) == snapshot[key], (
            f"{key}: the no-origin path drifted from the pre-R7 plan"
        )


def test_origins_only_downgrade_and_exactly_the_known_accounts() -> None:
    """With the regenerated configs applied, the only deltas against the pre-R7
    snapshot are DOWNGRADES, and exactly these: the two accounts whose hand-maintained
    maps disagreed with (could not express) the computed tier. Anything moving UP is a
    gate failure; anything new appearing here must be justified in the same commit."""
    snapshot = _snapshot()
    deltas: list[tuple[str, str, str, str]] = []
    for card in _wired_cards():
        key = f"{card.api_id}/{card.intent_name or 'SURFACE'}"
        for before, after in zip(snapshot[key], _plan_json(card), strict=True):
            assert before["account"] == after["account"]
            if before["provenance"] == after["provenance"]:
                continue
            assert _STRENGTH[after["provenance"]] < _STRENGTH[before["provenance"]], (
                f"{key}: {after['account']} moved UP "
                f"({before['provenance']} -> {after['provenance']}) — an origin may "
                "only demote"
            )
            deltas.append(
                (key, after["account"], before["provenance"], after["provenance"])
            )
    assert sorted(deltas) == [
        ("metadao_ico/SURFACE", "launch_signer", "extracted", "recovered"),
        ("meteora/SURFACE", "reserve", "extracted", "recovered"),
        ("meteora/plan_swap", "reserve", "extracted", "recovered"),
    ]


# --- derivation order is untouched by the cap --------------------------------------
def test_the_cap_never_reorders_or_drops_an_account() -> None:
    """The cap changes tags, never membership or order: every plan's account sequence
    equals the pre-R7 snapshot's."""
    snapshot = _snapshot()
    for card in _wired_cards():
        key = f"{card.api_id}/{card.intent_name or 'SURFACE'}"
        assert [s.account for s in _derive_plan(card)] == [
            s["account"] for s in snapshot[key]
        ]


def test_cyclic_accounts_stay_flagged_with_origins_asserted_end_to_end() -> None:
    """Through the whole ``_derive_plan`` path: a seed cycle with origin:extracted
    asserted on both members still reports both as unorderable gaps."""
    from gecko.find_start import _Card, _operation

    pdas = {
        "a": PdaNode(
            "a", (VariablePdaSeedNode("b", source="account", encoding="pubkey"),), None
        ),
        "b": PdaNode(
            "b", (VariablePdaSeedNode("a", source="account", encoding="pubkey"),), None
        ),
    }
    card = _Card(
        kind="start",
        api_id="synthetic",
        program_id="Synth1111111111111111111111111111111111111",
        instruction="loop",
        intent_name="plan_loop",
        inputs=(),
        accounts=("a", "b"),
        pdas=pdas,
        spec=StartSpec(accounts=("a", "b")),
        notes="",
        execute_url=None,
        operation=_operation("synthetic", "/synthetic", "s", "s", []),
        origins={"a": "extracted", "b": "extracted"},
    )
    plan = {s.account: s for s in _derive_plan(card)}
    assert plan["a"].provenance == "flagged"
    assert plan["b"].provenance == "flagged"

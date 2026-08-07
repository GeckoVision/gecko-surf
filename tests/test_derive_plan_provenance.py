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

from gecko.find_start import (
    DeriveStep,
    StartSpec,
    _account_step,
    _derive_plan,
    _wired_cards,
)
from gecko.pda import PdaNode
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
    not regress to ``flagged``. If this breaks, the fix over-flagged."""
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

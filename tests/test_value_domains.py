"""The declared value-domain review — and the two ways a declaration could lie.

Reviewed 2026-09-10, one program at a time. The rule the review applied:

    a slot carries a value domain when the CALLER chooses the asset,
    not merely when an asset is involved.

Three programs are deliberately EMPTY under that rule and they share one shape: the
caller names a CONTAINER — a launch, a store, a program — and the container fixes the
asset. Emptiness there is a finding, not an omission, so it is asserted rather than
allowed to be silence.

Why declared at all, rather than inferred: measured in
`docs/specs/2026-09-10-value-domains-not-yet.md`, both available inferences are wrong in
opposite directions. By account NAME 15 of 21 cards look token-shaped, including
`pumpfun.mint_authority`, which the program derives rather than a caller choosing. By
declared INPUT only 8 of 21, dropping `jurassic_fi.contribute`, whose mint is real and
never an input.
"""

from __future__ import annotations

import pytest

from gecko.providers.cli import intent_registries, start_specs

MINT = "solanatokenmint"

#: The review, restated here so the assertion is against a written claim rather than
#: against whatever the source happens to say today.
EXPECTED: dict[tuple[str, str], dict[str, str]] = {
    ("whirlpool", "plan_swap"): {"input_mint": MINT, "output_mint": MINT},
    ("meteora", "plan_swap"): {"input_mint": MINT, "output_mint": MINT},
    ("jupiter", "plan_route"): {"input_mint": MINT, "output_mint": MINT},
    ("pumpfun", "plan_buy"): {"mint": MINT},
    ("pumpfun", "plan_sell"): {"mint": MINT},
    ("metadao_ico", "plan_fund"): {"base_mint": MINT},
    # The caller names a container; the container fixes the asset.
    ("jurassic_fi", "plan_contribute"): {},
    ("let_me_buy", "plan_purchase"): {},
    ("ore", "plan_claim"): {},
}


def _specs() -> dict[tuple[str, str], object]:
    return {
        (api, name): spec
        for api, specs in start_specs().items()
        for name, spec in specs.items()
    }


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_every_declared_slot_exists_on_the_intent_it_describes(key) -> None:
    """A typo must not ship as a confident claim.

    This is the `surface_named` lesson applied to a second field: `_account_step` once
    fell through to `extracted` for any unknown string, so a misspelling shipped as "the
    surface stated this". The first draft of this very review failed here —
    `let_me_buy.plan_purchase` was declared with a `mint` it does not take, because the
    reviewer (me) generalised from the chain-step card instead of reading the plan intent.
    """
    api, name = key
    spec = _specs()[key]
    intent = intent_registries().get(api, {}).get(name)
    known = set(spec.accounts or ()) | set(getattr(intent, "inputs", ()) or ())
    unknown = sorted(set(spec.value_domains) - known)
    assert not unknown, (
        f"{api}.{name} declares a value domain for {unknown}, which is neither an "
        f"account nor an input of that intent. Known slots: {sorted(known)}"
    )


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_the_review_matches_what_the_programs_declare(key) -> None:
    """The review is a written claim; drift from it should be a decision, not an accident."""
    assert dict(_specs()[key].value_domains) == EXPECTED[key]


def test_an_empty_map_is_a_reviewed_finding_not_an_omission() -> None:
    """Three programs involve a token and declare nothing, on purpose.

    `jurassic_fi.plan_contribute` — the caller names a launch_id and the LAUNCH fixes the
    payment mint from its own allowlist. `let_me_buy.plan_purchase` — the caller names a
    store and a product; the store's account carries the mint it prices in.
    `ore.plan_claim` — `mint` is ORE's own fixed mint, not an asset anyone picks.

    If one of these ever gains a domain it means the intent changed shape, and that should
    fail here first.
    """
    for key in (
        ("jurassic_fi", "plan_contribute"),
        ("let_me_buy", "plan_purchase"),
        ("ore", "plan_claim"),
    ):
        assert not _specs()[key].value_domains, (
            f"{key} declares a caller-chosen asset; the review says the caller names a "
            "container and the container fixes the asset"
        )


def test_no_program_declares_a_domain_outside_the_shared_vocabulary() -> None:
    """One vocabulary, normalized the way graph/canonical/provider_matrix already do it.
    A parallel namespace would silently stop joining with everything else."""
    from gecko.graph import _norm

    for (api, name), spec in _specs().items():
        for slot, domain in (spec.value_domains or {}).items():
            assert domain == _norm(domain), (
                f"{api}.{name}.{slot} declares {domain!r}, which is not in graph._norm "
                "form — separators stripped and lowercased"
            )

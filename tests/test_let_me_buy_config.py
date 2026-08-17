"""The packaged ``let_me_buy`` config: it loads by the normal path, it derives the
account the chain confirmed, and every origin tier follows the artifact-precedence rule.

Three separable claims, and the test keeps them separable on purpose:

1. **Registration.** The config is reachable through ``load_packaged_provider("orquestra")``
   and nothing else. No ``_read_json``, no ``api_config_from_dict``, no filename. A config
   that only loads when a test names its path is a config that escapes every sweep the
   packaged loop runs over ``provider.apis`` (gecko/provider_config.py:352-362).

2. **Correctness of the recipe** — the derived ``receipts`` address for ``jonasbar``
   equals an address read off mainnet, where the account exists and the program owns it.
   That is EVIDENCE the recipe is right. It is deliberately not a tier: provenance says
   where a recipe came from, the chain says whether it is correct, and the two axes are
   orthogonal. A chain match may never be spent to buy a stronger provenance claim.

3. **Honesty of the origins** — every account in ``pdas`` has a ``pda_origins`` entry
   (absence is not neutral: find_start.py:499-500 hands back ``extracted`` for any account
   that merely HAS a node, and ``_apply_origin_cap`` returns the step untouched when the
   account is missing from the map, find_start.py:562-563), and every tier follows the
   artifact-precedence rule (founder-ruled 2026-08-13): declared by the artifact → theirs
   (``extracted``); artifact silent → ours (``recovered``); the two disagree → flagged.

CI-time scope, stated plainly: the completeness assertion here checks the PACKAGED bytes.
It is not a runtime guarantee — the loader accepts a config with no ``pda_origins`` at
all, and the cap no-ops on the accounts it does not name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gecko.pda import derive_pda
from gecko.provider_config import ProgramSpec, load_packaged_provider

PROGRAM_ID = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

#: Read off mainnet, not regenerated from the code under test: the account exists, and
#: its owner is ``PROGRAM_ID``. This is evidence, carried in a test — never a config field
#: (``ProgramSpec`` has no slot for it and ``_program_from_dict`` would silently drop one,
#: gecko/provider_config.py:292-301, which is an unenforced claim wearing a pin's costume).
JONASBAR_RECEIPTS = "H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V"

#: The two associated-token accounts ``make_purchase`` needs, and the owner each is
#: derived FROM. They are a pair, and the pairing is the point: the buyer pays from
#: ATA(signer, ...) and the store is credited at ATA(authority, ...). Swap the owners and
#: the instruction still builds — it just pays the wrong person.
ATA_ACCOUNTS = {
    "sender_token_account": "signer",
    "recipient_token_account": "authority",
}

#: A bar sells pairs. Each entry is (store_name, product_name) — enough distinct stores
#: that a lookup table which happened to contain one of them could not pass.
BAR_ORDERS = [
    ("jonasbar", "Beer"),
    ("jonasbar", "Peanuts"),
    ("jonasbar", "Water"),
    ("jonasbar", "Chocolate"),
]


def _let_me_buy() -> ProgramSpec:
    """The ONLY way this test reaches the config: the provider's own enumeration.

    No ``_read_json``, no ``api_config_from_dict``, no path. If ``let_me_buy`` is not
    listed in ``provider.json``'s ``apis``, this raises here — which is exactly the
    failure the registration requirement is about.
    """
    _provider, apis = load_packaged_provider("orquestra")
    api = apis["let_me_buy"]
    assert api.program is not None
    return api.program


def _effective_origin(program: ProgramSpec, account: str) -> str:
    """The tier that ACTUALLY governs this account at plan time.

    An absent entry is not a neutral default. ``_account_step`` returns ``extracted``
    for any account that merely has a ``PdaNode`` (find_start.py:499-500), and
    ``_apply_origin_cap`` returns that step untouched when the account is not in the
    origins map (find_start.py:562-563). So "no entry" reads downstream as the
    strongest claim in the ladder, and this helper says so rather than hiding it behind
    a ``.get(..., None)``.
    """
    if account not in program.pda_origins:
        return "extracted"
    tier = program.pda_origins[account]
    return tier if tier is not None else "flagged"


def test_let_me_buy_loads_through_the_packaged_provider() -> None:
    """Registration, checked the way the loader does it: enumerate ``provider.apis``."""
    provider, apis = load_packaged_provider("orquestra")
    assert "let_me_buy" in provider.apis, (
        "let_me_buy is not listed in provider.json's `apis` — load_packaged_provider "
        "enumerates ONLY from there, so the config would load through no packaged path "
        "and escape every all-configs invariant"
    )
    assert apis["let_me_buy"].program is not None
    assert apis["let_me_buy"].program.program_id == PROGRAM_ID  # type: ignore[union-attr]


def test_the_receipts_recipe_derives_the_address_the_chain_confirmed() -> None:
    """The packaged recipe — not a copy of it — reproduces a mainnet account.

    This proves the recipe CORRECT. It says nothing about where the recipe came from,
    and the config's tier is deliberately not raised by it.
    """
    program = _let_me_buy()
    got = derive_pda(program.pdas["receipts"], {"store_name": "jonasbar"})
    assert got.address == JONASBAR_RECEIPTS


@pytest.mark.parametrize("store_name,product_name", BAR_ORDERS)
def test_every_order_at_the_bar_lands_on_the_one_store_account(
    store_name: str, product_name: str
) -> None:
    """A bar is a lifecycle, not one call: beer and peanuts, water and chocolate.

    Every product on the menu is bought against the SAME ``receipts`` PDA, because the
    seed pair is ["receipts", store_name] and carries no product. That shared account is
    the data dependency that makes initialize → add_product → make_purchase →
    mark_as_delivered a chain rather than four unrelated calls — and it is why
    ``product_name`` is an instruction ARG, never a seed.
    """
    program = _let_me_buy()
    derived = derive_pda(program.pdas["receipts"], {"store_name": store_name})
    assert derived.address == JONASBAR_RECEIPTS
    assert product_name not in derived.address


def test_a_different_store_is_a_different_account() -> None:
    """Without this, a hardcoded table containing ``jonasbar`` would satisfy the two
    tests above. The seed is the store name, so a second bar must land elsewhere."""
    program = _let_me_buy()
    other = derive_pda(program.pdas["receipts"], {"store_name": "jonasbar-2"})
    assert other.address != JONASBAR_RECEIPTS


def test_every_pda_carries_an_origin_entry() -> None:
    """G3, over this config's bytes: ``set(pda_origins) == set(pdas)``.

    CI-time only. ``_pda_origins_from_dict`` returns ``{}`` on an absent key
    (gecko/provider_config.py:272-274), so nothing at runtime enforces this — which is
    precisely why the packaged bytes are checked here.
    """
    program = _let_me_buy()
    assert set(program.pda_origins) == set(program.pdas)
    assert set(program.pdas) == {"receipts", *ATA_ACCOUNTS}


@pytest.mark.parametrize("account,owner_seed", sorted(ATA_ACCOUNTS.items()))
def test_each_ata_tier_follows_the_artifact_precedence_rule(
    account: str, owner_seed: str
) -> None:
    """FOUNDER-RULED 2026-08-13 — the artifact-precedence rule, superseding this test's
    earlier position (kept legible here because the history explains the rule):

    * **If the artifact declares the recipe, go with theirs:** an IDL-declared pda block
      is the program author's own statement of which account the instruction expects —
      that constraint is THIS program's fact even when the derivation math (the SPL
      associated-token rule) belongs to another program. Tier: ``extracted``.
    * **If the artifact is silent, go with ours:** a hand-modelled recipe (pumpfun's
      ATAs, whose IDL carries no block) caps at ``recovered``.
    * **If both exist and disagree, neither wins:** ``flagged``. A disagreement between
      the artifact and our model is the most interesting fact either source can produce,
      and it is never resolved silently. (The three-way agreement test below is what
      detects it for this config.)

    The earlier position — that rule-ownership caps an ATA at ``recovered`` even when
    the IDL declares its block — was held from 2026-08-11 until the founder ruled. Its
    evidentiary ground (the fixture was a third-party mirror) fell on 2026-08-13 when
    the on-chain IDL account was fetched and found byte-identical; its semantic ground
    was overruled in favour of one tier meaning one thing everywhere: *where did the
    recipe come from.*

    The assertion still runs on the EFFECTIVE origin and still pins the recipe shape,
    so a drifted seed list cannot hide behind the tier.
    """
    program = _let_me_buy()
    node = program.pdas[account]
    assert node.program_id == "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
    assert [getattr(seed, "name", None) for seed in node.seeds] == [
        owner_seed,
        "token_program",
        "mint",
    ]
    # Declared by the artifact (the chain-verified IDL carries this account's pda
    # block — test_every_lifted_origin_has_a_pda_block_in_the_idl proves that side),
    # therefore theirs, therefore `extracted`.
    assert _effective_origin(program, account) == "extracted", (
        f"{account}: the program's own IDL declares this pda block, and the "
        "artifact-precedence rule says a declared recipe carries the artifact's tier"
    )


#: The program's Anchor IDL, verbatim, as comprehension received it (the hosted project
#: surface — a third-party mirror, not the on-chain IDL account; the config's own notes
#: already carry that provenance). Checked in so the "not established from source" claim
#: below is auditable against an artifact rather than asserted in prose.
IDL_FIXTURE = Path(__file__).parent / "fixtures" / "let_me_buy_idl.json"


def _idl() -> dict:
    return json.loads(IDL_FIXTURE.read_text(encoding="utf-8"))


def test_the_idl_fixture_is_this_program() -> None:
    idl = _idl()
    assert idl["address"] == PROGRAM_ID
    assert idl["metadata"]["name"] == "let_me_buy"


def test_the_store_name_constraint_is_carried_and_says_it_is_observed_only() -> None:
    """The live probe found a name the program refuses. What it did NOT find is the RULE.

    Two names were tested — one rejected, one accepted — and they differ by a single
    underscore. Two data points do not separate "no underscore" from "alphanumeric only"
    from "no character outside a-z0-9" from something else entirely, and a config that
    stated any of those would be inventing a constraint an agent would then trust.

    So the constraint is carried as an OBSERVATION with ``established_from_source`` false,
    at the ``manual`` tier. ``extracted``/``recovered`` would both claim the program's own
    artifact or source said this, and neither did.
    """
    program = _let_me_buy()
    constraint = program.arg_constraints["store_name"]
    assert constraint.origin == "manual"
    assert constraint.established_from_source is False
    assert "gecko_coffee" in constraint.observed_rejected
    assert "geckocoffee" in constraint.observed_accepted
    lowered = constraint.note.lower()
    assert "observed" in lowered
    assert "not established" in lowered


def test_the_idl_states_no_constraint_on_store_name() -> None:
    """The source side of the same claim, checked against the artifact.

    Every instruction that takes ``store_name`` declares it as a bare ``string``: no
    length, no charset, no pattern. An Anchor IDL has no vocabulary for a value
    constraint at all, so the check behind 6009 lives in Rust the IDL does not carry —
    which is exactly why the constraint above may not be labelled source-derived.
    """
    idl = _idl()
    seen = 0
    for instruction in idl["instructions"]:
        for arg in instruction.get("args", ()):
            if arg["name"].lstrip("_") == "store_name":
                seen += 1
                assert arg["type"] == "string", (
                    f"{instruction['name']}.{arg['name']} is no longer a bare string — "
                    "if the artifact grew a constraint, the config may finally state one"
                )
                assert set(arg) == {"name", "type"}
    assert seen == len(idl["instructions"])  # all eight take it


def test_the_idl_gives_length_its_own_error_code() -> None:
    """A fact that IS established from the artifact, and the reason it is stated as a
    fact and not as the rule: 6007 StringTooLong and 6009 InvalidStoreName are distinct
    declared codes, so "too long" is a different answer than what ``gecko_coffee`` got.
    That narrows the space; it does not name the check."""
    codes = {error["name"]: error["code"] for error in _idl()["errors"]}
    assert codes["StringTooLong"] == 6007
    assert codes["InvalidStoreName"] == 6009


def test_the_constraint_never_claims_a_source_derived_tier() -> None:
    """The gate condition, mechanical: an observation may not wear a tier that implies
    the program said it. ``_arg_constraints_from_dict`` refuses the contradiction at load
    time; this pins the packaged bytes on the honest side of it."""
    program = _let_me_buy()
    for name, constraint in program.arg_constraints.items():
        if not constraint.established_from_source:
            assert constraint.origin == "manual", (
                f"{name} is observed-only and may not carry {constraint.origin!r}"
            )


def test_the_two_atas_are_a_pair_and_credit_different_owners() -> None:
    """The buyer pays from their own ATA; the store is credited at the authority's.
    Same mint, same token program, different owner — and therefore different addresses.
    A config that derived both from the same owner would build a purchase that pays the
    buyer back."""
    program = _let_me_buy()
    buyer = "8D8qFHBnvS6oMsJy7EmGTrpoZcGd3aCC3pnPLi93Ag2V"
    store = "GDwnP2fPfeaVLQNu1WMHZLwHnJBBrkxdvE7qzMPRhb3P"
    bindings = {"mint": USDC, "token_program": TOKEN_PROGRAM}
    sender = derive_pda(
        program.pdas["sender_token_account"], {**bindings, "signer": buyer}
    )
    recipient = derive_pda(
        program.pdas["recipient_token_account"], {**bindings, "authority": store}
    )
    assert sender.address != recipient.address


# -- D1: the origins lift, exactly as far as the evidence carries them ------------------
#
# On 2026-08-13 the program's on-chain IDL account
# (`2nvFnEJA1HKueLpPaje8vR4tsU8rgRktx2dZhyjAfQZG`, owner = the program) was fetched and
# its zlib payload found BYTE-IDENTICAL to the fixture below — so the fixture is no
# longer only a third-party mirror; it is chain-corroborated as the artifact's own word,
# and it declares a pda block for all three make_purchase PDAs.
#
# FOUNDER-RULED 2026-08-13 (superseding the one-day asymmetric state): the
# artifact-precedence rule. If the artifact declares the recipe, go with theirs
# (`extracted`); if the artifact is silent, go with ours (`recovered` — pumpfun's ATAs
# stay there); if both exist and disagree, neither wins (`flagged`, via the agreement
# test going red). All three make_purchase PDAs are IDL-declared, so all three carry
# `extracted`.
#
# The tier and the recipe are pinned TOGETHER: if either the packaged recipe or the
# fixture drifts, the agreement test goes red, and the tier falls with it.

_D1_INPUTS = {
    "store_name": "jonasbar",
    "signer": "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi",
    "authority": "8D8qFHBnvS6oMsJy7EmGTrpoZcGd3aCC3pnPLi93Ag2V",
    "token_program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}

#: The REAL mainnet accounts these inputs must derive — chain-grounded, not self-referential:
#: the receipts address is the live jonasbar store and the ATAs are the funded accounts
#: that fifteen published transactions actually moved money through.
_D1_MAINNET_ADDRESSES = {
    "receipts": "H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V",
    "sender_token_account": "5aZMVzBM39Cm2JRPmJ2DWFFUiVqzpGy6wxiavvPRu9hR",
    "recipient_token_account": "FaK5981JTnAbraeKQTjptKAHiF74Zy4upg2hoBdLnGyY",
}


def test_lifted_origins_agree_with_the_idl_and_the_chain() -> None:
    """Each `extracted` PDA derives the SAME address from the packaged recipe, from the
    IDL's own pda block, and matches the real mainnet account. Three-way, so the tier
    cannot survive a drift in either recipe."""
    import json as _json
    from pathlib import Path as _Path

    from gecko.pda import derive_pda
    from gecko.pda_extract import from_anchor_idl

    idl = _json.loads(
        (_Path(__file__).parent / "fixtures" / "let_me_buy_idl.json").read_text()
    )
    idl_nodes = from_anchor_idl(idl)
    program = _let_me_buy()
    # FOUNDER-RULED 2026-08-13: all three are IDL-declared, so all three carry the
    # artifact's tier. See test_each_ata_tier_follows_the_artifact_precedence_rule.
    expected_tiers = {
        "receipts": "extracted",
        "sender_token_account": "extracted",
        "recipient_token_account": "extracted",
    }
    for name, tier in program.pda_origins.items():
        assert tier == expected_tiers[name], (
            f"{name}: the D1 lift must not silently regress (or silently widen)"
        )
        packaged = derive_pda(program.pdas[name], _D1_INPUTS).address
        from_idl = derive_pda(idl_nodes[name], _D1_INPUTS).address
        assert packaged == from_idl, (
            f"{name}: the packaged recipe no longer derives what the program's own IDL "
            f"derives — the `extracted` tier may not outlive the agreement"
        )
        assert packaged == _D1_MAINNET_ADDRESSES[name], (
            f"{name}: the recipe no longer derives the live mainnet account"
        )


def test_every_lifted_origin_has_a_pda_block_in_the_idl() -> None:
    """`extracted` claims the artifact's own word — so the artifact must actually carry
    a pda block for that account. An origin without one may be at most `recovered`."""
    import json as _json
    from pathlib import Path as _Path

    idl = _json.loads(
        (_Path(__file__).parent / "fixtures" / "let_me_buy_idl.json").read_text()
    )
    idl_pda_accounts = {
        account["name"]
        for instruction in idl["instructions"]
        for account in instruction.get("accounts", ())
        if "pda" in account
    }
    for name, tier in _let_me_buy().pda_origins.items():
        if tier in ("extracted", "recovered"):
            assert name in idl_pda_accounts, (
                f"{name}: origin {tier!r} claims artifact/source derivation but the IDL "
                "declares no pda block for it"
            )


def test_the_notes_do_not_claim_an_authority_check_make_purchase_does_not_have() -> (
    None
):
    """A `manual`-tier note said the program validates the merchant. It does not.

    `require!(*authority.key == receipts.authority, InvalidAuthority)` guards add_product,
    update_details, update_telegram_channel, delete_product and delete_store — and is
    ABSENT from make_purchase, whose payee ATA is derived from a caller-supplied key.
    Mainnet `3YDcon1k…` is a make_purchase with source == destination and identical
    pre/post token balances that still returned success and emitted PurchaseMade.

    The claim mattered because it is the reason a reader might think the CHAIN protects
    the merchant, when in fact `check_plan_accounts` protects them and the chain does not.
    A wrong note at the manual tier is exactly what that tier exists to admit.
    """
    notes = _let_me_buy().notes

    assert "IT DOES NOT" in notes
    assert "ABSENT from make_purchase" in notes
    # And it must not read as a bare assertion — the source it was checked against is named.
    assert "Woody4618/bar" in notes


def test_the_notes_state_that_mark_as_delivered_checks_no_authority() -> None:
    """The hole an IDL cannot show, and the config did not mention at all.

    `authority` is declared writable+signer, so Anchor checks that somebody signed and
    never who, and mark_as_delivered carries no `require!` against `receipts.authority`.
    Measured on a fork: a keypair generated seconds earlier flipped another merchant's
    receipt for a 5,000-lamport fee. A reader of the IDL sees `signer: true` and infers a
    named signer — which is exactly the inference this note has to break.
    """
    notes = _let_me_buy().notes

    assert "NO AUTHORITY CHECK" in notes
    # the measurement, not a claim: what was signed, and what the chain then did
    assert "34,870 CU" in notes
    # and where it can be re-run, so the note is falsifiable rather than remembered
    assert "rehearse_delivery" in notes


def test_the_notes_state_the_20_receipt_cap_and_the_silent_eviction() -> None:
    """The single most load-bearing behavioural fact about this program, and the config
    did not mention it at all.

    At 20 receipts `make_purchase` does `receipts.remove(0)` — no error, no event, no log.
    jonasbar has 125 purchases and holds 20. It is also what makes compute BOUNDED rather
    than growing, which is the opposite of what the CU series suggests at first glance.
    """
    notes = _let_me_buy().notes

    assert "CAPPED AT 20" in notes
    assert "SILENTLY EVICTED" in notes
    # The consequence a fulfiller has to know, not just the mechanism.
    assert "mark_as_delivered" in notes and "SUCCEEDS AND DOES NOTHING" in notes
    # And that the compute story is bounded, so nobody re-derives the wrong conclusion.
    assert "BOUNDED, not growing" in notes

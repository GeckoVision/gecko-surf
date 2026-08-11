"""The packaged ``let_me_buy`` config: it loads by the normal path, it derives the
account the chain confirmed, and its ATA recipes do not claim the program said them.

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
   account is missing from the map, find_start.py:562-563), and neither associated-token
   account claims ``extracted``. An ATA address is SPL-standard-derived from the caller's
   own owner/mint, not something this program's artifact stated.

CI-time scope, stated plainly: the completeness assertion here checks the PACKAGED bytes.
It is not a runtime guarantee — the loader accepts a config with no ``pda_origins`` at
all, and the cap no-ops on the accounts it does not name.
"""

from __future__ import annotations

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
def test_neither_ata_claims_the_program_declared_it(
    account: str, owner_seed: str
) -> None:
    """G1: an associated-token address is SPL-standard-derived from
    (owner, token_program, mint) under ``ATokenGPv...`` — the caller's own inputs run
    through a rule that belongs to the ATA program, not a fact this program's artifact
    stated. ``extracted`` would claim the latter, so it is refused for both.

    The assertion runs on the EFFECTIVE origin, so deleting the entry from
    ``pda_origins`` does not quietly pass: absence resolves to ``extracted`` and this
    turns red.
    """
    program = _let_me_buy()
    node = program.pdas[account]
    assert node.program_id == "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
    assert [getattr(seed, "name", None) for seed in node.seeds] == [
        owner_seed,
        "token_program",
        "mint",
    ]
    assert _effective_origin(program, account) != "extracted", (
        f"{account} is derived by the SPL associated-token rule from caller-supplied "
        "inputs; `extracted` would claim the let_me_buy artifact declared this address"
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

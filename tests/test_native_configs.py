"""The native programs — surfaces that will never have an IDL to extract.

SPL Token, the Associated Token Account program, Token-2022 and Address Lookup Table
are native Rust, so nothing parses them and a catalog that only ingests IDLs reports
them as gaps. They are not gaps. They are surfaces that have to be carried as
REVIEWED data at the `manual` tier instead of extracted from an artifact — and the
tier is the honest part: it says a human wrote this down, so hold it to a human's
standard of evidence.

That standard, here and in `test_let_me_buy_config.py`, is the same one: a recipe is
believed because an address derived from it matches a real account on chain, not
because it was carefully typed. Correctness is EVIDENCE; the tier is PROVENANCE; the
two are never conflated.

The offline leg needs no network. The chain leg reads its identities from
~/.gecko/catalog-ci.json and skips without it — a committed test has no business
naming somebody's wallet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gecko.pda import PdaNode, VariablePdaSeedNode, derive_pda
from gecko.provenance import ProgramProvenanceTier
from gecko.store_accounts import derive_ata

CONFIG_DIR = Path(__file__).resolve().parents[1] / "gecko/providers/configs/native"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


def load(name: str) -> dict[str, Any]:
    return json.loads((CONFIG_DIR / f"{name}.json").read_text())


def seeds_of(config: dict[str, Any], pda: str) -> list[dict[str, Any]]:
    return config["program"]["pdas"][pda]["seeds"]


@pytest.mark.parametrize("name", ["associated_token", "address_lookup_table"])
def test_every_native_recipe_declares_itself_manual(name: str) -> None:
    """`manual` is the whole point: nothing here was extracted, so nothing may claim to be.

    A hand-authored recipe carried at `extracted` would be the single most damaging
    lie this repo could tell about itself — it invites a consumer to trust a human's
    typing at the level of a parsed artifact.
    """
    program = load(name)["program"]
    origins = program["pda_origins"]
    assert set(origins) == set(program["pdas"]), "every recipe needs a stated origin"
    for pda, tier in origins.items():
        assert tier == "manual", f"{pda} is hand-authored and must say so"
        # the tier must be one the single source of truth actually defines
        assert tier in ProgramProvenanceTier.__args__  # type: ignore[attr-defined]


def test_the_ata_recipe_is_the_one_the_program_actually_uses() -> None:
    """[owner, token_program, mint] — in that order, under the ATA program.

    The order is load-bearing and silently so: swap any pair and you still get a
    valid off-curve address, it just belongs to nobody. This asserts the recipe
    against `gecko.store_accounts.derive_ata`, which is a SEPARATE implementation —
    two implementations of one recipe agreeing is evidence; one agreeing with itself
    is not.
    """
    seeds = seeds_of(load("associated_token"), "associated_token_account")
    assert [s["name"] for s in seeds] == ["owner", "token_program", "mint"]
    assert [s["encoding"] for s in seeds] == ["pubkey", "pubkey", "pubkey"]

    owner = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"  # a public mainnet address
    node = PdaNode(
        name="associated_token_account",
        seeds=tuple(
            VariablePdaSeedNode(name, source="account", encoding="pubkey")
            for name in ("owner", "token_program", "mint")
        ),
        program_id=ATA_PROGRAM,
    )
    from_recipe = derive_pda(
        node, {"owner": owner, "token_program": TOKEN_PROGRAM, "mint": USDC}
    )
    assert from_recipe.address == derive_ata(owner, USDC, token_program=TOKEN_PROGRAM)


def test_a_swapped_seed_pair_still_derives_and_that_is_the_danger() -> None:
    """The failure this recipe exists to prevent, made visible.

    A reversed pair does not raise, does not look wrong, and produces a perfectly
    well-formed address. Nothing downstream can catch it — which is precisely why the
    order is pinned by a test rather than left to whoever writes the next caller.
    """
    owner = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    reversed_node = PdaNode(
        name="associated_token_account",
        seeds=tuple(
            VariablePdaSeedNode(name, source="account", encoding="pubkey")
            for name in ("mint", "token_program", "owner")
        ),
        program_id=ATA_PROGRAM,
    )
    wrong = derive_pda(
        reversed_node, {"owner": owner, "token_program": TOKEN_PROGRAM, "mint": USDC}
    )
    assert wrong.address != derive_ata(owner, USDC, token_program=TOKEN_PROGRAM)
    assert len(wrong.address) >= 32, "it is a real address — that is the whole problem"


def test_the_lookup_table_recipe_says_its_slot_is_an_le_u64() -> None:
    """A slot seed is 8 little-endian bytes, and the width has to be stated.

    This recipe is deliberately NOT pinned against a mainnet account: re-deriving an
    existing table needs the slot it was created at, which cannot be recovered from
    the table itself. The config says so in as many words. An unverifiable recipe
    that admits it is honest; one that stays quiet is the problem.
    """
    (authority, slot) = seeds_of(load("address_lookup_table"), "lookup_table")
    assert authority["encoding"] == "pubkey"
    assert slot["name"] == "recent_slot"
    assert slot["encoding"] == "le"
    assert slot["width"] == 8


def test_the_notes_do_not_promise_verification_that_did_not_happen() -> None:
    """The ATA recipe claims chain evidence; the ALT recipe must not."""
    alt = load("address_lookup_table")["program"]["notes"]
    assert "UNVERIFIED" in alt
    ata = load("associated_token")["program"]["notes"]
    assert "manual" in ata and "never `extracted`" in ata


def test_the_ata_recipe_matches_a_real_mainnet_account() -> None:
    """The evidence leg: derive, then confirm the ATA program owns the result.

    Skips without ~/.gecko/catalog-ci.json — identities stay on the machine that has
    them, never in a committed test.
    """
    config_path = Path.home() / ".gecko/catalog-ci.json"
    if not config_path.exists():
        pytest.skip("no ~/.gecko/catalog-ci.json — the chain leg is opt-in")
    config = json.loads(config_path.read_text())

    from gecko.rpc import default_rpc_call  # local: the offline legs need no transport

    address = derive_ata(config["authority"], USDC, token_program=TOKEN_PROGRAM)
    info = default_rpc_call(
        "https://api.mainnet-beta.solana.com",
        "getAccountInfo",
        [address, {"encoding": "base64"}],
    )
    value = ((info or {}).get("result") or {}).get("value")
    assert value is not None, f"{address} is not a live account — the recipe is wrong"
    assert value["owner"] == TOKEN_PROGRAM, "an ATA is owned by the token program"

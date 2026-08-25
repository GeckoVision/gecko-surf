"""The probe projector — does the graph write the case the scorecard runs?

The motivating defect is on record and is the reason this file exists: the
hand-written scorecard probed ``delete_product`` WITHOUT ``system_program`` and with
an arg called ``name`` where the instruction takes ``product_name``. Both facts were
stated correctly in the program graph the scorecard was scoring, and the run printed
a red row for the SURFACE. So the two assertions that matter here are the two the
human got wrong, plus the refusal that stops the next class of the same mistake:
a probe that INVENTS a value hands the chain its own invention to judge.

Everything is offline and free — a PDA is arithmetic, not a chain read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gecko.pda import PdaNode, ResolverPdaSeedNode, VariablePdaSeedNode
from gecko.program_graph import (
    AccountRef,
    InstructionGraph,
    ProgramGraph,
    SeedBinding,
    build_program_graph,
)
from gecko.project.errors import UnknownInstructionError
from gecko.project.probes import (
    MissingProbeBindingError,
    ProbeCase,
    UnderivableAccountError,
    UnprobableArgTypeError,
    probe_case,
    probe_cases,
)
from gecko.store_accounts import derive_ata, receipts_pda

FIXTURES = Path(__file__).parent / "fixtures"

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

#: Chain fact, pinned in tests/test_store_accounts.py: the receipts PDA the program
#: derives for the store "jonasbar", read off mainnet where the program owns it.
JONASBAR_RECEIPTS = "H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V"
STORE = "jonasbar"

#: The nine accounts make_purchase declares, in IDL order.
MAKE_PURCHASE_ACCOUNTS = [
    "receipts",
    "signer",
    "authority",
    "mint",
    "sender_token_account",
    "recipient_token_account",
    "token_program",
    "system_program",
    "associated_token_program",
]


def _synthetic_pubkey(byte: int) -> str:
    """A valid, obviously-not-anybody's pubkey. No wallet belongs in a test file."""
    from solders.pubkey import Pubkey

    return str(Pubkey.from_bytes(bytes([byte]) * 32))


BUYER = _synthetic_pubkey(7)
AUTHORITY = _synthetic_pubkey(9)


def let_me_buy_graph() -> ProgramGraph:
    idl = json.loads((FIXTURES / "let_me_buy_idl.json").read_text())
    return build_program_graph(idl=idl)


def bindings(**overrides: str) -> dict[str, str]:
    """Only what a caller has to CHOOSE. Note what is absent: no `system_program`,
    no `token_program`, no `associated_token_program`, no `receipts` — the IDL pins
    the first three and the graph derives the fourth."""
    base = {
        "signer": BUYER,
        "authority": AUTHORITY,
        "mint": USDC,
        "store_name": STORE,
        "_store_name": STORE,
        "product_name": "Water",
        "name": "Sparkling",
        "price": "120000",
        "table_number": "9",
        "details": "open until late",
        "telegram_channel_id": "@somewhere",
        "receipt_id": "106",
    }
    base.update(overrides)
    return base


def case_for(instruction: str, **overrides: str) -> ProbeCase:
    return probe_case(let_me_buy_graph(), instruction, bindings=bindings(**overrides))


def without(key: str) -> dict[str, str]:
    values = bindings()
    del values[key]
    return values


# ---------------------------------------------------------------------------
# the two facts the hand-written list got wrong
# ---------------------------------------------------------------------------


def test_make_purchase_carries_all_nine_accounts_in_idl_order() -> None:
    """The full account map, complete and ordered — a caller passing it positionally
    is right by construction, and an omitted slot cannot happen by typo."""
    case = case_for("make_purchase")
    assert list(case.accounts) == MAKE_PURCHASE_ACCOUNTS
    assert len(case.accounts) == 9
    assert all(case.accounts.values()), "every slot must carry an address"


def test_delete_product_carries_system_program_and_product_name() -> None:
    """THE regression. The hand-written case omitted `system_program` and sent `name`
    instead of `product_name`; the surface's two errors named both mistakes exactly,
    and the scorecard printed them as defects in the program."""
    case = case_for("delete_product")
    assert list(case.accounts) == ["receipts", "authority", "system_program"]
    assert case.accounts["system_program"] == SYSTEM_PROGRAM
    assert list(case.args) == ["store_name", "product_name"]
    assert case.args["product_name"] == "Water"
    # the wrong name specifically: `name` is add_product's arg, not this one's
    assert "name" not in case.args


def test_add_product_is_the_instruction_that_really_takes_name() -> None:
    """The mirror of the defect: `name` is right HERE, which is why the mix-up was
    plausible enough to ship."""
    case = case_for("add_product")
    assert list(case.args) == ["store_name", "name", "price"]
    assert case.args["name"] == "Sparkling"


# ---------------------------------------------------------------------------
# the refusal
# ---------------------------------------------------------------------------


def test_missing_account_binding_refuses_and_names_it() -> None:
    graph = let_me_buy_graph()
    with pytest.raises(MissingProbeBindingError) as raised:
        probe_cases(graph, bindings=without("authority"))
    assert "authority" in str(raised.value)


def test_missing_arg_binding_refuses_and_names_it() -> None:
    graph = let_me_buy_graph()
    with pytest.raises(MissingProbeBindingError) as raised:
        probe_cases(graph, bindings=without("telegram_channel_id"))
    assert "telegram_channel_id" in str(raised.value)


def test_an_empty_binding_is_missing_not_a_value() -> None:
    """A blank store name derives a real PDA. It would be the WRONG one, and it would
    build, simulate and revert against somebody else's account."""
    with pytest.raises(MissingProbeBindingError):
        probe_case(let_me_buy_graph(), "delete_store", bindings=bindings(store_name=""))


def test_no_binding_at_all_refuses_rather_than_producing_empty_cases() -> None:
    with pytest.raises(MissingProbeBindingError):
        probe_cases(let_me_buy_graph(), bindings={})


# ---------------------------------------------------------------------------
# the addresses — derived, and checked against an independent deriver
# ---------------------------------------------------------------------------


def test_receipts_matches_the_address_mainnet_confirmed() -> None:
    """The seed recipe walked by the probe projector must land on the account the
    chain already showed us for this store. `receipts_pda` shares no code path with
    `probe_cases`; two implementations agreeing is evidence, one agreeing with itself
    is not."""
    case = case_for("make_purchase")
    assert case.accounts["receipts"] == JONASBAR_RECEIPTS
    assert case.accounts["receipts"] == receipts_pda(STORE)


def test_both_token_accounts_are_the_right_owners_ata() -> None:
    case = case_for("make_purchase")
    assert case.accounts["sender_token_account"] == derive_ata(BUYER, USDC, token_program=TOKEN_PROGRAM)
    assert case.accounts["recipient_token_account"] == derive_ata(AUTHORITY, USDC, token_program=TOKEN_PROGRAM)
    assert (
        case.accounts["sender_token_account"]
        != case.accounts["recipient_token_account"]
    ), "buyer and merchant token accounts must not collapse to one address"


def test_pinned_program_addresses_come_from_the_idl_not_the_caller() -> None:
    """The IDL fixes them, so asking a caller is how a probe ends up parameterising
    the token program."""
    case = case_for("make_purchase")
    assert case.accounts["token_program"] == TOKEN_PROGRAM
    assert case.accounts["system_program"] == SYSTEM_PROGRAM
    assert case.accounts["associated_token_program"] == ATA_PROGRAM


def test_mark_as_delivered_resolves_the_underscored_arg_alias() -> None:
    """Its receipts seed says `store_name`; its args are `_store_name`/`receipt_id`.
    Matching seeds to args by name finds nothing for the seed that SELECTS THE STORE,
    which is the whole join this package exists to keep."""
    case = case_for("mark_as_delivered")
    assert list(case.args) == ["_store_name", "receipt_id"]
    assert case.accounts["receipts"] == JONASBAR_RECEIPTS


def test_a_different_store_derives_a_different_receipts_account() -> None:
    """The guard is only a guard if it moves when the property does — the H7Bj/HVkb
    pair is the `--store` half-select failure already on record."""
    other = probe_case(
        let_me_buy_graph(),
        "make_purchase",
        bindings=bindings(store_name="geckocoffee"),
    )
    assert other.accounts["receipts"] != JONASBAR_RECEIPTS
    assert other.accounts["receipts"] == receipts_pda("geckocoffee")


# ---------------------------------------------------------------------------
# args are typed by the graph, not by the author
# ---------------------------------------------------------------------------


def test_integer_args_are_converted_using_the_declared_idl_type() -> None:
    purchase = case_for("make_purchase")
    assert purchase.args["table_number"] == 9
    assert isinstance(purchase.args["table_number"], int)
    assert case_for("add_product").args["price"] == 120_000
    assert case_for("mark_as_delivered").args["receipt_id"] == 106


def test_a_value_that_does_not_fit_its_declared_width_refuses() -> None:
    """`table_number` is a u8. 300 fits in the JSON and not in the byte."""
    with pytest.raises(UnprobableArgTypeError) as raised:
        case_for("make_purchase", table_number="300")
    assert "u8" in str(raised.value)


def test_a_non_numeric_value_for_a_numeric_arg_refuses() -> None:
    with pytest.raises(UnprobableArgTypeError):
        case_for("add_product", price="a lot")


# ---------------------------------------------------------------------------
# the case set, and the fee payer
# ---------------------------------------------------------------------------


def test_every_instruction_of_the_graph_gets_a_case() -> None:
    """No silent shortening: a scorecard that drops a row reads as a program with
    fewer instructions than it has. Filtering is the CALLER's decision, stated."""
    graph = let_me_buy_graph()
    cases = probe_cases(graph, bindings=bindings())
    assert [c.instruction for c in cases] == [ix.name for ix in graph.instructions]
    assert len(cases) == 8


def test_the_fee_payer_is_the_instructions_own_signer() -> None:
    assert case_for("make_purchase").fee_payer == BUYER  # `signer`
    assert case_for("update_details").fee_payer == AUTHORITY
    assert case_for("delete_product").fee_payer == AUTHORITY


def test_intent_falls_back_to_a_label_and_can_be_overridden() -> None:
    """The one field the graph cannot supply — and the fallback is honest about being
    a label rather than a user's words."""
    cases = {
        c.instruction: c for c in probe_cases(let_me_buy_graph(), bindings=bindings())
    }
    assert cases["make_purchase"].intent == "make purchase"
    overridden = probe_cases(
        let_me_buy_graph(),
        bindings=bindings(),
        intents={"make_purchase": "buy a bottle of water at the bar"},
    )
    assert overridden[4].intent == "buy a bottle of water at the bar"
    assert overridden[4].instruction == "make_purchase"


def test_an_unknown_instruction_refuses_with_the_list_that_exists() -> None:
    with pytest.raises(UnknownInstructionError) as raised:
        probe_case(let_me_buy_graph(), "refund", bindings=bindings())
    assert "make_purchase" in str(raised.value)


def test_a_probe_case_cannot_be_edited_after_it_is_built() -> None:
    case = case_for("delete_product")
    with pytest.raises(TypeError):
        case.accounts["authority"] = BUYER  # type: ignore[index]


# ---------------------------------------------------------------------------
# an account the graph itself will not stand behind
# ---------------------------------------------------------------------------


def _unresolvable_graph() -> ProgramGraph:
    """A program whose one PDA seed is a runtime read — the honest-gap case."""
    node = PdaNode(
        name="vault",
        seeds=(
            ResolverPdaSeedNode(
                name="epoch", depends_on=("clock",), reason="read from another account"
            ),
            VariablePdaSeedNode(name="owner", source="account", encoding="pubkey"),
        ),
        program_id="BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya",
    )
    accounts = (
        AccountRef(
            name="vault",
            is_pda=True,
            writable=True,
            resolvable=False,
            derive_from=(
                SeedBinding("epoch", "", "unresolved", None),
                SeedBinding("owner", "pubkey", "account", "owner"),
            ),
        ),
        AccountRef(name="owner", is_pda=False, signer=True, writable=True),
    )
    return ProgramGraph(
        program_id="BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya",
        pdas={"vault": node},
        instructions=(
            InstructionGraph(
                name="claim",
                args=(),
                accounts=accounts,
                derivation_order=("vault",),
            ),
        ),
    )


def test_an_unresolvable_pda_refuses_instead_of_probing_a_plausible_address() -> None:
    with pytest.raises(UnderivableAccountError) as raised:
        probe_cases(_unresolvable_graph(), bindings={"owner": BUYER})
    assert "vault" in str(raised.value)

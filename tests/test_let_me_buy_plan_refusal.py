"""The same-token-account trap: a `make_purchase` that pays the buyer back.

A live probe POSTed a `make_purchase` whose `sender_token_account` and
`recipient_token_account` were the SAME address and the builder answered HTTP 200.
Nothing in the artifact refuses it: both accounts are associated-token addresses, both
are writable, both are structurally valid, and the program's own checks pass — the
transaction is well-formed and moves the buyer's tokens to the buyer. It is wrong in a
way only the JOIN can see: `sender_token_account = ATA(signer, ...)` and
`recipient_token_account = ATA(authority, ...)` are the same recipe over DIFFERENT
owners, so their collapsing to one address means an owner was bound twice.

So the refusal lives at plan time, and it is scoped as tightly as the fact is:

1. **Keyed on the resolved pair** ``(api_id, instruction)`` — never on an account NAME.
   A rule that fired on "two accounts whose names end in ``_token_account`` match" would
   refuse every legitimate self-transfer on every other surface. The scoping test below
   uses the SAME two account names on a different ``api_id`` and requires it to pass.
2. **Fail-closed on absence.** If the pair is registered and either side is missing from
   the resolved map, that is a refusal too (``account-unresolved``) — an unresolved
   account may never be the reason a distinctness check quietly did not run.
3. **Nothing global.** The registry is pinned with ``==`` below: one key, one rule.
"""

from __future__ import annotations

import pytest

from gecko.plan_refusals import (
    DISTINCT_ACCOUNT_RULES,
    PlanRefused,
    check_plan_accounts,
)
from gecko.provider_config import load_packaged_provider

#: The address the builder handed back for BOTH sides of the observed purchase. Real,
#: and on its own perfectly valid: it is the buyer's USDC ATA in the landed transactions
#: recorded in tests/fixtures/let_me_buy_mainnet_accounts.json.
BUYER_ATA = "AzNx1xhhXNAWueYhuusvzYessN23RNXk1xfmU7iJ5rjB"
#: The store's USDC ATA in those same landed transactions — the address the recipient
#: side is supposed to carry.
STORE_ATA = "FaK5981JTnAbraeKQTjptKAHiF74Zy4upg2hoBdLnGyY"

LET_ME_BUY = "let_me_buy"
MAKE_PURCHASE = "make_purchase"


def _purchase_accounts(sender: str, recipient: str) -> dict[str, str]:
    """The resolved subset a `make_purchase` plan carries for the two ATAs."""
    return {
        "receipts": "H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V",
        "sender_token_account": sender,
        "recipient_token_account": recipient,
    }


def test_the_landed_pair_is_not_refused() -> None:
    """The control: the two addresses twelve landed mainnet purchases actually used.
    If this ever refuses, the rule is refusing reality and the rule is wrong."""
    check_plan_accounts(
        LET_ME_BUY, MAKE_PURCHASE, _purchase_accounts(BUYER_ATA, STORE_ATA)
    )


def test_the_same_token_account_on_both_sides_is_refused() -> None:
    """The observed trap, plan time. HTTP 200 is not a verdict."""
    with pytest.raises(PlanRefused) as excinfo:
        check_plan_accounts(
            LET_ME_BUY, MAKE_PURCHASE, _purchase_accounts(BUYER_ATA, BUYER_ATA)
        )
    refusal = excinfo.value
    assert refusal.code == "same-token-account"
    assert refusal.api_id == LET_ME_BUY
    assert refusal.instruction == MAKE_PURCHASE
    assert refusal.accounts == ("sender_token_account", "recipient_token_account")
    # the refusal says WHAT would refute it, not just that it fired
    assert refusal.refuted_by


def test_an_unresolved_side_refuses_rather_than_skipping_the_check() -> None:
    """An account the plan could not resolve is the honest gap this codebase reports —
    never the silent reason a distinctness check did not run. Both directions."""
    for missing in ("sender_token_account", "recipient_token_account"):
        accounts = _purchase_accounts(BUYER_ATA, STORE_ATA)
        del accounts[missing]
        with pytest.raises(PlanRefused) as excinfo:
            check_plan_accounts(LET_ME_BUY, MAKE_PURCHASE, accounts)
        assert excinfo.value.code == "account-unresolved"
        assert missing in excinfo.value.accounts


def test_an_empty_address_is_unresolved_not_equal() -> None:
    """Two blanks are equal strings. They are not two accounts that collide — they are
    two accounts that were never resolved, and they must refuse under THAT code, or the
    report tells the caller to fix an owner binding that was never made."""
    with pytest.raises(PlanRefused) as excinfo:
        check_plan_accounts(LET_ME_BUY, MAKE_PURCHASE, _purchase_accounts("", ""))
    assert excinfo.value.code == "account-unresolved"


def test_the_registry_is_one_pair_and_nothing_global() -> None:
    """Pinned with ``==``: the day a second entry lands, this test says so."""
    assert set(DISTINCT_ACCOUNT_RULES) == {(LET_ME_BUY, MAKE_PURCHASE)}


def test_a_self_transfer_on_a_different_api_is_untouched() -> None:
    """The scoping proof, and it is deliberately the HARDEST version of it.

    Same two account NAMES, same colliding address, different ``api_id``. A self-transfer
    is legal on SPL Token — the same owner's account on both sides is a no-op transfer,
    not a mistake — and nothing here may refuse it. If this raises, the rule keyed on a
    name and has become a global rule wearing a scoped rule's costume.
    """
    check_plan_accounts(
        "spl_token",
        "transfer_checked",
        {
            "sender_token_account": BUYER_ATA,
            "recipient_token_account": BUYER_ATA,
        },
    )


def test_a_different_instruction_of_the_same_surface_is_untouched() -> None:
    """The key is the PAIR. `mark_as_delivered` has no token accounts and no rule; it is
    not reached by let_me_buy's entry, even though the api_id matches."""
    check_plan_accounts(
        LET_ME_BUY,
        "mark_as_delivered",
        {"receipts": BUYER_ATA, "authority": BUYER_ATA},
    )


def test_the_rule_names_accounts_the_packaged_config_actually_derives() -> None:
    """A typo in either account name would make the rule a permanent no-op that still
    reads as a control. The rule's accounts must exist in the config's own ``pdas``."""
    _provider, apis = load_packaged_provider("orquestra")
    program = apis[LET_ME_BUY].program
    assert program is not None
    rule = DISTINCT_ACCOUNT_RULES[(LET_ME_BUY, MAKE_PURCHASE)]
    for account in rule.accounts:
        assert account in program.pdas, (
            f"{account} is not a PDA this config derives — the rule would never fire"
        )

"""Plan-time refusals: the account-distinctness facts no artifact states.

An IDL says which accounts an instruction takes. It never says that two of them must be
DIFFERENT addresses. That fact lives in the join — ``sender_token_account`` and
``recipient_token_account`` are the same associated-token recipe over different owners —
and when a builder binds one owner twice, the result is a structurally valid transaction
that pays the buyer back. The observed case: a ``let_me_buy`` ``make_purchase`` built
with the same address on both sides, answered ``HTTP 200``.

Three rules this module keeps, and they are the reason it is small:

1. **Scope is the resolved pair** ``(api_id, instruction)``. Never an account name, never
   a substring, never a shape. Two accounts whose names both end in ``_token_account``
   matching is *normal* — a self-transfer is legal on SPL Token and on every surface that
   wraps it. A rule that fired on the name would refuse those, and a refusal that fires
   on legitimate traffic gets disabled, which is how the real one stops working too.
2. **Absence refuses.** If the pair is registered and either account is missing or empty
   in the resolved map, that is ``account-unresolved`` — a distinctness check that
   silently did not run is the honest-gap failure this codebase exists to prevent, and it
   would fail at signing time, far from here.
3. **The registry is closed and enumerable.** One entry today. Adding one is a reviewed
   decision (it asserts a fact about a surface), not a convenience edit, and each entry
   carries what would refute it.

Pure: no I/O, no chain read, no config read. A rule here is trusted CODE on purpose —
sourcing it from an ingested file would let an untrusted artifact delete its own guard.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "PlanRefusalCode",
    "PlanRefused",
    "DistinctAccountsRule",
    "DISTINCT_ACCOUNT_RULES",
    "check_plan_accounts",
]

#: Closed. A refusal must say WHICH check fired: "these two collapsed to one address" and
#: "one of them was never resolved" are different answers with different fixes, and they
#: may never render as the same string. Adding a member asserts a new class of plan-time
#: fact and goes through the same review the provenance ladders do.
PlanRefusalCode = Literal["same-token-account", "account-unresolved"]


class PlanRefused(Exception):
    """A plan was refused before anything was built, signed, or sent.

    Carries no transaction and no bytes on purpose: catching this cannot yield something
    callable, so the ``try/except`` a caller reaches for is not a bypass.
    """

    def __init__(
        self,
        reason: str,
        *,
        code: PlanRefusalCode,
        api_id: str,
        instruction: str,
        accounts: tuple[str, ...],
        refuted_by: str = "",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.api_id = api_id
        self.instruction = instruction
        self.accounts = accounts
        self.refuted_by = refuted_by


@dataclass(frozen=True)
class DistinctAccountsRule:
    """Two accounts of one instruction that may not resolve to the same address.

    Every field answers one of the four questions an edge must answer on its own:
    ``accounts`` — what it connects; ``basis`` — on what basis; ``origin`` — from which
    class of knowledge; ``refuted_by`` — what evidence would refute it.
    """

    accounts: tuple[str, str]
    code: PlanRefusalCode
    basis: str
    refuted_by: str


_MAKE_PURCHASE_PAYS_THE_BUYER_BACK = DistinctAccountsRule(
    accounts=("sender_token_account", "recipient_token_account"),
    code="same-token-account",
    basis=(
        "both are the SPL associated-token recipe over different owners — "
        "ATA(signer, token_program, mint) pays, ATA(authority, token_program, mint) is "
        "credited — so one address on both sides means one owner was bound twice and the "
        "purchase pays the buyer back. Observed: a build accepted exactly this and "
        "answered HTTP 200"
    ),
    refuted_by=(
        "a landed let_me_buy make_purchase whose sender and recipient token accounts are "
        "the same address, or an artifact change that stops deriving these two from "
        "different owners"
    ),
)

#: The whole registry. Enumerable, pinned by test, and deliberately not a pattern.
DISTINCT_ACCOUNT_RULES: dict[tuple[str, str], DistinctAccountsRule] = {
    ("let_me_buy", "make_purchase"): _MAKE_PURCHASE_PAYS_THE_BUYER_BACK,
}


def check_plan_accounts(
    api_id: str, instruction: str, accounts: Mapping[str, str]
) -> None:
    """Refuse a resolved account map that violates this instruction's distinctness rule.

    No-op for any ``(api_id, instruction)`` without an entry — there is no global rule
    here and there must not be one: a self-transfer is legitimate on most surfaces.

    :raises PlanRefused: ``same-token-account`` when the two accounts resolve to one
        address; ``account-unresolved`` when either is missing or empty (checked FIRST,
        so two blanks are reported as two unresolved accounts, never as a collision).
    """
    rule = DISTINCT_ACCOUNT_RULES.get((api_id, instruction))
    if rule is None:
        return
    left, right = rule.accounts
    missing = tuple(name for name in rule.accounts if not accounts.get(name))
    if missing:
        raise PlanRefused(
            f"{api_id}.{instruction}: {', '.join(missing)} did not resolve to an "
            "address, so the distinctness check between "
            f"{left} and {right} could not run — reported, never skipped",
            code="account-unresolved",
            api_id=api_id,
            instruction=instruction,
            accounts=missing,
            refuted_by=rule.refuted_by,
        )
    if accounts[left] == accounts[right]:
        raise PlanRefused(
            f"{api_id}.{instruction}: {left} and {right} resolved to the same address "
            f"({accounts[left]}) — {rule.basis}",
            code=rule.code,
            api_id=api_id,
            instruction=instruction,
            accounts=rule.accounts,
            refuted_by=rule.refuted_by,
        )

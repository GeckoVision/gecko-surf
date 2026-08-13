"""`prepare_purchase` must refuse a self-paying purchase BEFORE it reaches the builder.

The trap this pins is not hypothetical. On 2026-08-11 a `make_purchase` was built whose
`sender_token_account` and `recipient_token_account` were the same address, and the
builder answered **HTTP 200**. That transaction is structurally valid, signs, lands — and
pays the buyer back. Nothing errors.

`gecko/plan_refusals.py` has held the rule that would catch it since it was written. What
it did not have was a caller, which is the same as not having the rule: a guard nothing
invokes is a comment. These tests exist to make that failure mode impossible to
reintroduce, so they assert the two things that actually matter:

1. The refusal fires (`same-token-account`).
2. It fires **before the network**, proven by making any outbound request an error rather
   than by trusting call order. A check that ran after `/build` had already accepted the
   plan would still "pass" a naive assertion while the request had left the machine.

`scripts/` is not a package, so the module is loaded by path.
"""

from __future__ import annotations

import importlib.util
import urllib.request
from pathlib import Path
from types import ModuleType

import pytest

from gecko.plan_refusals import PlanRefused
from gecko.store_accounts import StoreAccounts
from gecko.store_directory import StoreProduct

_REPO = Path(__file__).resolve().parents[1]
BUYER = "DMjTEZJuV3mpfzBNeeuFy9m47A1bj5CXVhCNVo7BEPzy"
#: Kept for reference: the address the live probe saw on BOTH sides of the collision.
PROBE_COLLISION = "AzNx1xhhXNAWueYhuusvzYessN23RNXk1xfmU7iJ5rjB"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _store() -> StoreAccounts:
    """The published demo store, as the resolver would hand it over — no chain read.

    A light fake rather than a mock: this is the real dataclass, so its own
    receipts/token-account consistency check runs here too.
    """
    return StoreAccounts(
        store_name="jonasbar",
        product=StoreProduct(name="Water", price_raw=100_000, decimals=6, mint=USDC),
        receipts="H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V",
        authority="8D8qFHBnvS6oMsJy7EmGTrpoZcGd3aCC3pnPLi93Ag2V",
        token_account="FaK5981JTnAbraeKQTjptKAHiF74Zy4upg2hoBdLnGyY",
    )


def _prepare_purchase() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "prepare_purchase", _REPO / "scripts" / "prepare_purchase.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any outbound request is a test failure, not a mock to be satisfied."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "build_instruction reached the network — the plan-time refusal must fire "
            "BEFORE /build is asked to accept the collision"
        )

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def test_a_self_paying_purchase_is_refused_before_the_builder_is_asked(
    no_network: None,
) -> None:
    """The recorded trap: one owner bound twice, so the buyer pays themselves."""
    module = _prepare_purchase()
    # The recipient is the resolved store's token account, so the collision is reached
    # when the BUYER's token account resolves to that same address — i.e. the buyer is the
    # shop. That is the identical fault the live probe hit on a store whose merchant and
    # buyer were one wallet; only the route to it differs.
    store = _store()
    collides = store.token_account

    with pytest.raises(PlanRefused) as refusal:
        module.build_instruction(
            store=store,
            signer=BUYER,
            sender_token_account=collides,
            table=1,
        )

    assert refusal.value.code == "same-token-account"
    assert refusal.value.api_id == "let_me_buy"
    assert refusal.value.instruction == "make_purchase"
    # The address that collided must be named — a refusal you cannot act on is a shrug.
    assert collides in str(refusal.value)


def test_an_unresolved_token_account_is_refused_rather_than_skipped(
    no_network: None,
) -> None:
    """An empty address must not let the distinctness check quietly not run.

    Two blanks compare equal, so a check written the obvious way would report this as a
    collision. It is not one — it is a check that could not run, and it has its own code
    so the fix is obvious to whoever reads the refusal.
    """
    module = _prepare_purchase()

    with pytest.raises(PlanRefused) as refusal:
        module.build_instruction(
            store=_store(), signer=BUYER, sender_token_account="", table=1
        )

    assert refusal.value.code == "account-unresolved"


def test_a_distinct_purchase_is_not_refused_and_does_reach_the_builder() -> None:
    """The guard must not refuse the legitimate path — that is how a guard gets deleted.

    A rule that fires on honest traffic gets disabled, and then it is not there for the
    dishonest traffic either. So this asserts the good plan passes the check AND that the
    request is the one we meant to send.
    """
    module = _prepare_purchase()
    sent: dict[str, object] = {}

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"transaction": "ok"}'

    def capture(request: urllib.request.Request, **_kwargs: object) -> _Response:
        sent["url"] = request.full_url
        sent["body"] = request.data
        return _Response()

    original = urllib.request.urlopen
    urllib.request.urlopen = capture  # type: ignore[assignment]
    try:
        result = module.build_instruction(
            store=_store(),
            signer=BUYER,
            # A different address from the store's recipient — the real, honest case.
            sender_token_account="9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
            table=1,
        )
    finally:
        urllib.request.urlopen = original  # type: ignore[assignment]

    assert result == {"transaction": "ok"}
    assert sent["url"] == module.BUILD_URL
    assert b"make_purchase" in sent["url"].encode() or b"sender_token_account" in (
        sent["body"] or b""
    )

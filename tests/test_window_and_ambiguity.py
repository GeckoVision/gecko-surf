"""The ~40-second window, and the product name that needed a round trip.

Both findings come from the first real purchase through a hosted signer (PayBox, mainnet,
2026-08-15, signature `2KxAbvtke5s…`, 27,617 CU). The purchase landed on the second
attempt; the first died on `BlockhashNotFound` because the client spent the window loading
tool definitions between prepare and sign.

Three things were wrong, and only one of them was the client's:

1. **We were reading the blockhash at `finalized`**, which is ~35 blocks behind the tip, so
   a fifth of the 150-block budget was gone before the caller saw the bytes. Measured on
   mainnet at one instant: finalized left 114 blocks (~46s), confirmed left 149 (~60s).
2. **The deadline was prose.** "about 40 seconds" is not something an agent can subtract
   from; `blocks_remaining` is.
3. **The failure surfaced as a bare RPC error** after broadcast, naming no cause and
   offering no remedy. `verify_signed_transaction` sits immediately before broadcast and
   can now catch it there instead.

And separately: `"a coffee"` resolved to Espresso only because it was the only coffee on
the menu. The refusal for an unmatched product was coded `store-unknown`, which is a lie —
the store resolved fine — and carried the menu only as prose.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.landing import block_height, latest_blockhash
from gecko.prepare_purchase import _expiry
from gecko.verify_signed import verify_signed, verify_signed_result

RPC = "https://rpc.example.test"


class _FakeRpc:
    """Records the params it was called with. The commitment IS the thing under test."""

    def __init__(self, height: int = 1000, last_valid: int = 1150):
        self.calls: list[tuple[str, Any]] = []
        self._height, self._last_valid = height, last_valid

    def __call__(self, url: str, method: str, params: Any) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "getLatestBlockhash":
            return {
                "result": {
                    "value": {
                        "blockhash": "H" * 43,
                        "lastValidBlockHeight": self._last_valid,
                    }
                }
            }
        if method == "getBlockHeight":
            return {"result": self._height}
        raise AssertionError(f"unexpected method {method}")


# --- 1. the blockhash is read at `confirmed`, which is worth ~14 seconds ---------


def test_the_blockhash_is_read_at_confirmed_not_finalized() -> None:
    rpc = _FakeRpc()
    latest_blockhash(RPC, rpc)
    method, params = rpc.calls[0]
    assert method == "getLatestBlockhash"
    assert params == [{"commitment": "confirmed"}], (
        "finalized is ~35 blocks behind the tip and spends a fifth of the window before "
        "the caller ever sees the bytes"
    )


def test_the_height_is_read_at_the_same_commitment_as_the_blockhash() -> None:
    """Comparing a finalized height against a confirmed deadline would understate the
    budget by the very gap this change exists to reclaim."""
    rpc = _FakeRpc()
    block_height(RPC, rpc)
    assert rpc.calls[0] == ("getBlockHeight", [{"commitment": "confirmed"}])


def test_a_failed_height_read_costs_nothing() -> None:
    """The countdown is an ergonomic extra on a path whose real job already succeeded."""

    def broken(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ConnectionError("node unreachable")

    assert block_height(RPC, broken) is None


# --- 2. the deadline is a budget, not a sentence ---------------------------------


def test_expiry_reports_a_subtractable_budget() -> None:
    out = _expiry("H" * 43, last_valid_block_height=1150, current_block_height=1000)
    assert out["blocks_remaining"] == 150
    assert out["seconds_remaining_estimate"] == 60
    assert out["last_valid_block_height"] == 1150
    assert out["current_block_height"] == 1000


def test_expiry_never_reports_a_negative_budget() -> None:
    out = _expiry("H" * 43, last_valid_block_height=1000, current_block_height=1200)
    assert out["blocks_remaining"] == 0
    assert out["seconds_remaining_estimate"] == 0


def test_expiry_admits_when_it_could_not_read_the_height() -> None:
    """`None` means "I did not check" and must never render as a number an agent trusts."""
    out = _expiry("H" * 43, last_valid_block_height=1150, current_block_height=None)
    assert out["blocks_remaining"] is None
    assert out["seconds_remaining_estimate"] is None
    assert out["last_valid_block_height"] == 1150


def test_expiry_tells_the_agent_to_warm_its_signer_first() -> None:
    note = _expiry("H" * 43, 1150, 1000)["note"].lower()
    assert "before calling this" in note and "signer" in note


# --- 3. expired bytes refuse BEFORE broadcast ------------------------------------

VALID_BINDING = "a" * 64


def _signed_and_matching(monkeypatch: pytest.MonkeyPatch) -> str:
    """A transaction that binds and carries a signature, so the expiry branch is the ONLY
    thing left that can decide the outcome."""
    import gecko.verify_signed as module

    monkeypatch.setattr(module, "message_binding", lambda *a, **k: VALID_BINDING)
    monkeypatch.setattr(module, "_carries_a_signature", lambda _tx: True)
    return "dHggYnl0ZXM="


def test_expired_bytes_are_refused_with_a_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tx = _signed_and_matching(monkeypatch)
    out = verify_signed(tx, VALID_BINDING, still_landable=False)
    assert out.verified is False, "broadcasting expired bytes cannot succeed"
    # The two facts that ARE true stay true — this is not a claim the signer misbehaved.
    assert out.binding_matches is True and out.signed is True
    assert out.still_landable is False
    assert "prepare_purchase" in out.reason and "free" in out.reason


def test_live_bytes_still_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    tx = _signed_and_matching(monkeypatch)
    out = verify_signed(tx, VALID_BINDING, still_landable=True)
    assert out.verified is True and out.still_landable is True


def test_not_checking_is_not_the_same_as_passing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tx = _signed_and_matching(monkeypatch)
    out = verify_signed(tx, VALID_BINDING, still_landable=None)
    assert out.verified is True
    assert out.still_landable is None, (
        "'I did not check' must not render as 'it is fine'"
    )


def test_the_verifier_itself_never_touches_the_network() -> None:
    """`verify_signed` decides whether somebody broadcasts. It must not acquire a way to
    fail because a node was slow — the liveness read lives in the transport shim."""
    from gecko.verify_signed import verify_signed as fn

    # The names the compiled function actually REFERENCES — a substring scan of the source
    # matches the phrase "last_valid_block_height" inside the refusal prose and would fail
    # for the wrong reason.
    referenced = set(fn.__code__.co_names)
    for forbidden in ("block_height", "default_rpc_call", "urlopen", "requests"):
        assert forbidden not in referenced, forbidden


def test_the_liveness_read_is_skipped_without_both_arguments() -> None:
    for args in (
        {"last_valid_block_height": 100},  # no url
        {"rpc_url": "https://rpc.example.test"},  # no deadline
        {},
    ):
        out = verify_signed_result(
            {"transaction": "x", "binding": VALID_BINDING, **args}
        )
        assert out["still_landable"] is None


def test_the_liveness_read_refuses_an_unsafe_url() -> None:
    """This surface is unauthenticated; an unguarded URL would make it a proxy. Not
    knowing the height is fine — being a fetch gadget is not."""
    out = verify_signed_result(
        {
            "transaction": "x",
            "binding": VALID_BINDING,
            "last_valid_block_height": 100,
            "rpc_url": "http://169.254.169.254/latest/meta-data/",
        }
    )
    assert out["still_landable"] is None


def test_every_refusal_branch_reports_what_was_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A measured fact must not be dropped just because another check failed first —
    `still_landable` was silently `None` on the not-signed and not-matching paths."""
    import gecko.verify_signed as module

    monkeypatch.setattr(module, "message_binding", lambda *a, **k: VALID_BINDING)
    monkeypatch.setattr(module, "_carries_a_signature", lambda _tx: False)
    assert verify_signed("x", VALID_BINDING, still_landable=True).still_landable is True

    monkeypatch.setattr(module, "message_binding", lambda *a, **k: "b" * 64)
    assert verify_signed("x", VALID_BINDING, still_landable=True).still_landable is True


# --- 4. an unmatched product is not an unknown store ----------------------------


def test_an_unmatched_product_does_not_claim_the_store_is_unknown() -> None:
    """`store-unknown` made an agent abandon a storefront that exists over a product name
    it could simply fix. The store resolved; only the product did not match."""
    from tests.test_prepare_purchase_tool import BUYER, RPC_URL, FakeBuilder, FakeRpc
    from gecko.prepare_purchase import prepare_purchase_result

    out = prepare_purchase_result(
        {
            "store": "jonasbar",
            "product": "definitely-not-on-this-menu",
            "buyer": BUYER,
            "network": "mainnet",
            "rpc_url": RPC_URL,
        },
        build_call=FakeBuilder(),
        rpc_call=FakeRpc(),
    )
    assert out["refused"] is True
    assert out["code"] == "product-unknown"
    assert out["code"] != "store-unknown"


def test_the_menu_comes_back_as_data_not_only_prose() -> None:
    """ "a coffee" against a store with three coffees needs a list to choose from. It
    resolved here only because Espresso was the only coffee — a fact about this menu, not
    about the surface."""
    from tests.test_prepare_purchase_tool import BUYER, RPC_URL, FakeBuilder, FakeRpc
    from gecko.prepare_purchase import prepare_purchase_result

    out = prepare_purchase_result(
        {
            "store": "jonasbar",
            "product": "coffee",
            "buyer": BUYER,
            "network": "mainnet",
            "rpc_url": RPC_URL,
        },
        build_call=FakeBuilder(),
        rpc_call=FakeRpc(),
    )
    products = out.get("products")
    assert isinstance(products, list) and products, "the menu must be machine-readable"
    for entry in products:
        assert set(entry) == {"name", "price_ui", "mint"}
        assert entry["name"] and entry["mint"]
    # The prose still names them too — an agent that only reads `reason` is not stranded.
    assert products[0]["name"] in out["reason"]


# --- 5. the three independent round trips actually overlap ----------------------


def test_the_build_blockhash_and_height_run_concurrently() -> None:
    """Measured against mainnet, this path spent 99.6% of its wall clock in network I/O —
    4.0s, of which Python was 14.5ms. Rewriting the language would have bought back the
    14.5ms; overlapping the three independent round trips buys ~1.8s.

    Proven by TIME, not by reading the code: each fake sleeps, and a serial implementation
    cannot finish inside the sum.
    """
    import time

    from tests.test_prepare_purchase_tool import BUYER, RPC_URL, FakeBuilder, FakeRpc
    from gecko.prepare_purchase import prepare_purchase_result

    delay = 0.25

    class _SlowRpc(FakeRpc):
        def __call__(self, url: str, method: str, params: Any) -> Any:
            if method in ("getLatestBlockhash", "getBlockHeight"):
                time.sleep(delay)
            return super().__call__(url, method, params)

    class _SlowBuilder(FakeBuilder):
        def __call__(self, plan: Any) -> Any:
            time.sleep(delay)
            return super().__call__(plan)

    started = time.perf_counter()
    out = prepare_purchase_result(
        {
            "store": "jonasbar",
            "product": "Water",
            "buyer": BUYER,
            "network": "mainnet",
            "rpc_url": RPC_URL,
        },
        build_call=_SlowBuilder(),
        rpc_call=_SlowRpc(),
    )
    elapsed = time.perf_counter() - started

    assert out["refused"] is False, out.get("reason")
    # Serial would be >= 3 * delay. Concurrent is ~1 * delay plus the untimed store read
    # and simulate. The 2x bound is loose enough for a slow CI box and still impossible
    # to satisfy serially.
    assert elapsed < delay * 2, (
        f"took {elapsed:.2f}s for three {delay}s round trips — they are still serial"
    )


# --- 6. who resolves "a coffee" — and who must not ------------------------------


def _refusal_for(product: str) -> dict[str, Any]:
    from tests.test_prepare_purchase_tool import BUYER, RPC_URL, FakeBuilder, FakeRpc
    from gecko.prepare_purchase import prepare_purchase_result

    return prepare_purchase_result(
        {
            "store": "jonasbar",
            "product": product,
            "buyer": BUYER,
            "network": "mainnet",
            "rpc_url": RPC_URL,
        },
        build_call=FakeBuilder(),
        rpc_call=FakeRpc(),
    )


def test_a_category_word_gets_no_lexical_guess() -> None:
    """ "a coffee" against Espresso/Cappuccino/Mochaccino shares no token with any of them.
    Resolving it needs to know what coffee IS — world knowledge the calling agent has and
    a chain account does not. So we return the menu and no opinion."""
    out = _refusal_for("a black coffee")
    assert out["code"] == "product-unknown"
    assert out["close_matches"] == [], "a category word is not lexical evidence"
    assert out["products"], "the menu is still ground truth"


def test_a_typo_is_named_but_still_not_chosen() -> None:
    """A partial name IS evidence — and a tool that quietly substitutes the one thing it
    found is a tool that eventually buys the wrong thing confidently."""
    out = _refusal_for("Wate")
    assert [m["name"] for m in out["close_matches"]] == ["Water"]
    assert out["refused"] is True, (
        "one close match must STILL refuse, never auto-select"
    )
    assert "transaction" not in out


def test_the_refusal_delegates_the_choice_explicitly() -> None:
    reason = _refusal_for("a black coffee")["reason"]
    assert "EXACT" in reason, "the agent must re-call with the exact listed name"
    assert "ASK THE BUYER" in reason
    assert "do not pick for them" in reason


def test_an_exact_name_still_goes_straight_through() -> None:
    """The refusal path must not have made the normal case harder."""
    out = _refusal_for("Water")
    assert out["refused"] is False and out["status"] == "pass"

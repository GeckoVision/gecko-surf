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


def test_a_mistyped_name_is_named_but_a_category_is_not() -> None:
    """The line this draws is the whole argument for a deterministic layer here.

    A dropped letter is a TYPO and resembles its target character-for-character, so
    `difflib` names it. "a black coffee" resembles "Espresso" not at all — bridging that
    needs to know what black coffee IS, which is the calling agent's job. A model in this
    seat would blur the two and occasionally buy a Mochaccino for someone who said black.
    """
    from gecko.store_accounts import ResolvedStore
    from gecko.store_directory import StoreProduct

    mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    store = ResolvedStore(
        store_name="geckocoffee",
        receipts=__import__("gecko.store_accounts", fromlist=["x"]).receipts_pda(
            "geckocoffee"
        ),
        authority="DMjTEZJuV3mpfzBNeeuFy9m47A1bj5CXVhCNVo7BEPzy",
        products=tuple(
            StoreProduct(name=n, price_raw=p, decimals=6, mint=mint)
            for n, p in (
                ("Espresso", 100_000),
                ("Sparkling water", 50_000),
                ("Cappuccino", 150_000),
                ("Mochaccino", 200_000),
            )
        ),
    )

    def names(q: str) -> list[str]:
        return [e.name for e in store.close_matches(q)]

    # Typos and partials — resemblance is evidence.
    assert names("Cappucino") == ["Cappuccino"]
    assert names("Capuccino") == ["Cappuccino"]
    assert names("Mochacino") == ["Mochaccino"]
    assert names("Espress") == ["Espresso"]
    assert names("water") == ["Sparkling water"]

    # Categories and nonsense — resemblance is absent, and inventing it would be guessing.
    assert names("coffee") == []
    assert names("a black coffee") == []
    assert names("xyzzy") == []
    assert names("") == []

    # Menu order, never match order: a ranking here would read as a recommendation.
    assert names("ccino") == ["Cappuccino", "Mochaccino"]


# --- 7. a store branded one way and registered another --------------------------


class _CaseAwareRpc:
    """A node that has exactly one storefront, under the LOWERCASE name."""

    def __init__(self, registered: str = "jonasbar"):
        from gecko.store_accounts import receipts_pda

        self._live = receipts_pda(registered)
        self.reads: list[str] = []

    def __call__(self, url: str, method: str, params: Any) -> Any:
        from tests.test_prepare_purchase_tool import PROGRAM

        assert method == "getAccountInfo"
        address = str(params[0])
        self.reads.append(address)
        if address != self._live:
            return {"result": {"value": None}}
        import base64 as b64

        from tests.test_store_directory import encode_store

        blob = encode_store("jonasbar", products=[("Water", 100_000, 6)])
        return {
            "result": {
                "value": {
                    "owner": PROGRAM,
                    "data": [b64.b64encode(blob).decode(), "base64"],
                }
            }
        }


def test_a_branded_store_name_is_refused_with_the_registered_one() -> None:
    """Reported from a real session: the PDA seed is the EXACT string, so `JonasBar` and
    `jonasbar` are different accounts and the branded spelling refuses on a store that
    plainly exists. The refusal now names the registered spelling."""
    from gecko.store_accounts import StoreNotFound, resolve_store

    rpc = _CaseAwareRpc()
    try:
        resolve_store("JonasBar", rpc_url="https://93.184.216.34/rpc", rpc_call=rpc)
    except StoreNotFound as exc:
        assert "'jonasbar'" in str(exc), str(exc)
        assert "DOES exist" in str(exc)
        # ...and it is a HINT, not a substitution: the caller re-calls, we do not.
        assert "will not substitute it for you" in str(exc)
    else:
        raise AssertionError("a name that derives a dead PDA must refuse")


def test_the_case_hint_costs_no_program_scan() -> None:
    """`resolve_store` promises one targeted read so it works where `getProgramAccounts`
    is disabled. The hint must not quietly break that."""
    from gecko.store_accounts import StoreNotFound, resolve_store

    rpc = _CaseAwareRpc()
    with pytest.raises(StoreNotFound):
        resolve_store("JonasBar", rpc_url="https://93.184.216.34/rpc", rpc_call=rpc)
    assert len(rpc.reads) <= 4, f"{len(rpc.reads)} reads — this is turning into a scan"


def test_an_unknown_store_points_at_list_stores() -> None:
    from gecko.store_accounts import StoreNotFound, resolve_store

    with pytest.raises(StoreNotFound) as excinfo:
        resolve_store(
            "NoSuchStoreAnywhere",
            rpc_url="https://93.184.216.34/rpc",
            rpc_call=_CaseAwareRpc(),
        )
    assert "list_stores" in str(excinfo.value)
    assert "DOES exist" not in str(excinfo.value), "do not invent a suggestion"


def test_a_hint_lookup_that_fails_does_not_break_the_refusal() -> None:
    """The hint runs on a path that is ALREADY refusing. A node that errors mid-hint must
    still produce the clear refusal, never an exception."""
    from gecko.store_accounts import StoreNotFound, resolve_store

    calls = {"n": 0}

    def flaky(url: str, method: str, params: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"result": {"value": None}}
        raise ConnectionError("node went away")

    with pytest.raises(StoreNotFound) as excinfo:
        resolve_store("JonasBar", rpc_url="https://93.184.216.34/rpc", rpc_call=flaky)
    assert "no store named 'JonasBar'" in str(excinfo.value)


# --- 8. the preflight must never be stricter than the blockhash -----------------


#: `processed` sees the most, `finalized` the least. A transaction can only be validated by
#: something at least as LOOSE as whatever issued its blockhash.
_LOOSENESS = {"processed": 2, "confirmed": 1, "finalized": 0}


def _commitments_used() -> dict[str, str]:
    """Every commitment this path actually sends, read from the calls themselves."""
    import gecko.rpc as rpcmod
    from gecko.landing import block_height, latest_blockhash

    seen: dict[str, str] = {}

    def spy(url: str, method: str, params: Any) -> Any:
        if params and isinstance(params[-1], dict) and "commitment" in params[-1]:
            seen[method] = params[-1]["commitment"]
        if method == "getLatestBlockhash":
            return {
                "result": {"value": {"blockhash": "H" * 43, "lastValidBlockHeight": 9}}
            }
        return {"result": 1}

    latest_blockhash("https://rpc.example.test", spy)
    block_height("https://rpc.example.test", spy)
    del rpcmod  # only imported to make the dependency explicit
    return seen


def test_the_send_preflight_is_not_stricter_than_the_blockhash() -> None:
    """The regression, and the reason it cost a live purchase.

    `latest_blockhash` moved to `confirmed` to reclaim ~14s of window. `sendTransaction`
    was sending no `preflightCommitment`, so its preflight ran at the RPC default —
    `finalized` — which is STRICTER, does not know a confirmed-only blockhash, and rejects
    a perfectly valid transaction with `BlockhashNotFound`. 100% failure, while the
    simulation passed because it runs at `processed`.

    Asserting the ORDERING rather than the literal values is the point: an equality test
    on "confirmed" would have passed before the fix too, because the broken side sent
    nothing at all.
    """
    import inspect

    from gecko.autonomous_purchase import _send
    from gecko.landing import RPC_COMMITMENT

    used = _commitments_used()
    assert used["getLatestBlockhash"] == RPC_COMMITMENT
    assert used["getBlockHeight"] == RPC_COMMITMENT

    source = inspect.getsource(_send)
    assert "preflightCommitment" in source, (
        "no preflightCommitment means the RPC default, `finalized`, which is stricter "
        "than the blockhash commitment and rejects every send"
    )
    assert "RPC_COMMITMENT" in source, "it must be THE constant, not a second copy"


def test_simulation_stays_at_least_as_loose_as_the_blockhash() -> None:
    """Simulation may be looser (it is: `processed`), never stricter."""
    import inspect

    from gecko import simulate as simmod
    from gecko.landing import RPC_COMMITMENT

    source = inspect.getsource(simmod)
    used = [c for c in _LOOSENESS if f'"commitment": "{c}"' in source]
    assert used, "simulate names no commitment — it would inherit an RPC default"
    for commitment in used:
        assert _LOOSENESS[commitment] >= _LOOSENESS[RPC_COMMITMENT], (
            f"simulate uses {commitment!r}, stricter than the blockhash's "
            f"{RPC_COMMITMENT!r} — it would reject bytes that are valid"
        )


def test_the_result_tells_the_agent_which_commitment_to_send_with() -> None:
    """An agent submits these bytes itself. With default options it gets BlockhashNotFound
    and an error naming neither cause nor remedy, so the answer ships in the result."""
    from tests.test_prepare_purchase_tool import BUYER, RPC_URL, FakeBuilder, FakeRpc
    from gecko.landing import RPC_COMMITMENT
    from gecko.prepare_purchase import prepare_purchase_result

    out = prepare_purchase_result(
        {
            "store": "jonasbar",
            "product": "Water",
            "buyer": BUYER,
            "network": "mainnet",
            "rpc_url": RPC_URL,
        },
        build_call=FakeBuilder(),
        rpc_call=FakeRpc(),
    )
    assert out["submit"]["preflight_commitment"] == RPC_COMMITMENT
    submit_step = next(s for s in out["next_step"]["do"] if s["step"] == "submit")
    assert submit_step["options"]["preflightCommitment"] == RPC_COMMITMENT

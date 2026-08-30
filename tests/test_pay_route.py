"""Can this wallet buy this product, and if not what is the shortest CHECKED route.

Every test drives injected seams — no network. The decision ORDER is what most of these
pin, because each refusal is cheaper and more certain than the one after it, and a check
that runs too late is a check that already spent something.
"""

from gecko import pay_route
from gecko.peg_guard import PegReading
from gecko.pegana import recorded_peg_reader
from gecko.store_accounts import TOKEN_PROGRAM_ID, derive_ata
from gecko.store_directory import StoreProduct

TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
BUYER = "5cjBs5VE8WVVctG2EoUkYiRkW92sXkoT4YsNxszWC9CE"
AUTHORITY = "3i92aBEYCPTVYT8bMYcLdEjcJRP1UBmvPHnUdRDvMrs1"

PEGGED = PegReading(
    tracked=True, symbol="X", state_body={"state": "PEGGED", "stale": False}
)
DEPEGGED = PegReading(
    tracked=True, symbol="X", state_body={"state": "DEPEG", "stale": False}
)


class _Store:
    """A stand-in for ResolvedStore.accounts_for(product) — the two facts we consume."""

    def __init__(self, mint: str, price_raw: int = 100_000, authority: str = AUTHORITY):
        self.store_name = "geckocoffee"
        self.authority = authority
        self.product = StoreProduct(
            name="Espresso", price_raw=price_raw, decimals=6, mint=mint
        )
        self.receipts = "receipts111"
        self.token_account = derive_ata(authority, mint, token_program=TOKEN_PROGRAM_ID)

    @property
    def mint(self) -> str:
        return self.product.mint


class _Counter:
    def __init__(self, fn):
        self.n = 0
        self._fn = fn

    def __call__(self, *a, **k):
        self.n += 1
        return self._fn(*a, **k)


def _assess(
    *, store, holdings, peg=None, mint_owner=None, idl=None, venues=None, buyer=BUYER
):
    peg_reader = _Counter(peg or recorded_peg_reader({}))
    idl_fetch = _Counter(idl or (lambda program: {}))
    find_venues = _Counter(venues or (lambda **k: []))
    report = pay_route.assess_payment(
        store=store,
        buyer=buyer,
        holdings=holdings,
        mint_owner=mint_owner or (lambda m: TOKEN_PROGRAM_ID),
        peg_reader=peg_reader,
        idl_fetch=idl_fetch,
        find_venues=find_venues,
    )
    return report, peg_reader, idl_fetch, find_venues


# --- the order of refusals ------------------------------------------------------------


def test_a_token_2022_priced_product_is_refused_before_anything_else() -> None:
    """let_me_buy PINS classic SPL in its IDL, so a Token-2022 priced mint has NO path
    through make_purchase — no balance and no swap makes it payable. Refusing this late,
    or not at all, is the incident that funded a wallet three times on mainnet to buy
    from a store that structurally could not be paid."""
    store = _Store(mint=USDG)
    report, peg, idl, venues = _assess(
        store=store,
        holdings={USDG: (10**9, TOKEN_2022)},  # MORE than the price
        mint_owner=lambda m: TOKEN_2022,
    )
    assert report.outcome == "pinned_program_mismatch"
    assert report.blocks is True
    assert report.route is None
    # and it cost nothing to find out
    assert (peg.n, idl.n, venues.n) == (0, 0, 0)


def test_self_purchase_is_refused_before_any_venue_or_oracle_is_touched() -> None:
    store = _Store(mint=USDC, authority=BUYER)  # buyer IS the store
    report, peg, idl, venues = _assess(
        store=store, holdings={USDC: (10**9, TOKEN_PROGRAM_ID)}
    )
    assert report.outcome == "self_purchase"
    assert report.blocks is True
    assert (peg.n, idl.n, venues.n) == (0, 0, 0)


def test_the_self_purchase_comparison_uses_one_program_basis() -> None:
    """Both sides must derive with the pinned classic program. Deriving the buyer's ATA
    with the priced mint's own owner puts the two addresses on different bases, and the
    guard silently never fires."""
    store = _Store(mint=USDC, authority=BUYER)
    report, *_ = _assess(
        store=store,
        holdings={USDC: (10**9, TOKEN_PROGRAM_ID)},
        mint_owner=lambda m: TOKEN_2022,  # a lying owner must not move the comparison
    )
    assert report.outcome in {"pinned_program_mismatch", "self_purchase"}


# --- the peg gate, on BOTH sides of the conversion ------------------------------------


def test_a_depegged_priced_mint_is_never_a_route_destination() -> None:
    """The mint being converted INTO is checked too. Quoting a route into a broken peg
    while reporting blocked:false is the failure this whole module exists to prevent."""
    store = _Store(mint=USDC)
    report, *_ = _assess(
        store=store,
        holdings={USDG: (10**9, TOKEN_PROGRAM_ID)},
        peg=recorded_peg_reader({USDC: DEPEGGED, USDG: PEGGED}),
    )
    assert report.outcome == "peg_blocked"
    assert report.blocks is True
    assert report.route is None
    assert USDC in report.reason


def test_payable_now_reports_the_priced_mint_verdict_without_blocking() -> None:
    """Holding enough means no conversion happens, so a depeg is information rather than
    a refusal — we are not asking anyone to acquire the asset."""
    store = _Store(mint=USDC)
    report, *_ = _assess(
        store=store,
        holdings={USDC: (10**9, TOKEN_PROGRAM_ID)},
        peg=recorded_peg_reader({USDC: DEPEGGED}),
    )
    assert report.outcome == "payable_now"
    assert report.blocks is False
    assert any(c.mint == USDC for c in report.peg_checks)


def test_an_unreachable_oracle_blocks_the_route() -> None:
    store = _Store(mint=USDC)
    report, *_ = _assess(store=store, holdings={USDG: (10**9, TOKEN_PROGRAM_ID)})
    assert report.blocks is True
    assert report.outcome == "peg_blocked"


def test_a_peg_refusal_on_one_mint_does_not_abandon_the_wallet() -> None:
    """A blocked candidate is skipped, not fatal — the wallet may hold another mint that
    is fine, and refusing the whole request would be a blanket denial."""
    store = _Store(mint=USDC)
    venue = pay_route.Quote(
        pool="pool111",
        amount_in=200_000,
        direction="a_to_b",
        liquidity=10**9,
        tick_spacing=64,
        fee_rate=300,
    )
    report, *_ = _assess(
        store=store,
        holdings={BONK: (10**9, TOKEN_PROGRAM_ID), USDG: (10**9, TOKEN_PROGRAM_ID)},
        peg=recorded_peg_reader({USDC: PEGGED, BONK: DEPEGGED, USDG: PEGGED}),
        venues=lambda **k: [venue] if k.get("held_mint") == USDG else [],
    )
    assert report.outcome == "route_found"
    assert report.route is not None
    assert report.route.held_mint == USDG
    # every evaluated mint is reported, including the one that blocked
    assert {c.mint for c in report.peg_checks} == {USDC, BONK, USDG}
    assert any(c.blocks for c in report.peg_checks)


def test_peg_checks_covers_every_evaluated_mint_including_the_destination() -> None:
    store = _Store(mint=USDC)
    report, *_ = _assess(
        store=store,
        holdings={USDG: (10**9, TOKEN_PROGRAM_ID)},
        peg=recorded_peg_reader({USDC: PEGGED, USDG: PEGGED}),
    )
    assert USDC in {c.mint for c in report.peg_checks}


# --- the ordinary answers -------------------------------------------------------------


def test_holding_enough_is_payable_now() -> None:
    store = _Store(mint=USDC, price_raw=100_000)
    report, _, idl, venues = _assess(
        store=store,
        holdings={USDC: (100_000, TOKEN_PROGRAM_ID)},
        peg=recorded_peg_reader({USDC: PEGGED}),
    )
    assert report.outcome == "payable_now"
    assert report.blocks is False
    assert (idl.n, venues.n) == (0, 0)  # no venue work when none is needed


def test_an_empty_wallet_is_no_candidates_not_no_route() -> None:
    store = _Store(mint=USDC)
    report, *_ = _assess(
        store=store, holdings={}, peg=recorded_peg_reader({USDC: PEGGED})
    )
    assert report.outcome == "no_candidates"
    assert report.blocks is True


def test_a_pair_with_no_pool_is_no_route() -> None:
    store = _Store(mint=USDC)
    report, *_ = _assess(
        store=store,
        holdings={USDG: (10**9, TOKEN_PROGRAM_ID)},
        peg=recorded_peg_reader({USDC: PEGGED, USDG: PEGGED}),
        venues=lambda **k: [],
    )
    assert report.outcome == "no_route"
    assert report.blocks is True
    assert USDG in report.no_pool_for


def test_a_route_that_costs_more_than_is_held_is_rejected_and_recorded() -> None:
    store = _Store(mint=USDC)
    too_big = pay_route.Quote(
        pool="pool111",
        amount_in=10**12,
        direction="a_to_b",
        liquidity=10**9,
        tick_spacing=64,
        fee_rate=300,
    )
    report, *_ = _assess(
        store=store,
        holdings={USDG: (1000, TOKEN_PROGRAM_ID)},
        peg=recorded_peg_reader({USDC: PEGGED, USDG: PEGGED}),
        venues=lambda **k: [too_big],
    )
    assert report.outcome == "no_route"
    assert report.rejected_legs
    assert report.rejected_legs[0].held_mint == USDG


def test_a_report_crosses_the_boundary_as_data() -> None:
    store = _Store(mint=USDC)
    report, *_ = _assess(
        store=store,
        holdings={USDC: (10**9, TOKEN_PROGRAM_ID)},
        peg=recorded_peg_reader({USDC: PEGGED}),
    )
    d = report.to_dict()
    assert d["outcome"] == "payable_now"
    assert d["blocked"] is False
    assert "peg_evidence_as_of" in d
    assert isinstance(d["peg_checks"], list)


def test_blocks_is_true_for_every_outcome_that_is_not_an_answer() -> None:
    assert pay_route.BLOCKING == frozenset(
        {
            "pinned_program_mismatch",
            "self_purchase",
            "no_candidates",
            "peg_blocked",
            "no_route",
        }
    )
    assert "payable_now" not in pay_route.BLOCKING
    assert "route_found" not in pay_route.BLOCKING


def test_nothing_in_the_package_raises_system_exit() -> None:
    """The script this replaces called SystemExit for an incomplete IDL. A library that
    exits the process cannot be used by an MCP surface."""
    import inspect

    assert "SystemExit" not in inspect.getsource(pay_route)

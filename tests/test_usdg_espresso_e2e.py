"""The gap, closed and proven offline: buy an espresso holding only USDG.

This is the whole reason `plan_payment` exists, driven end to end with every seam
recorded — no network, $0, deterministic. Per Pattern B the free simulation is the FIRST
deliverable for a wire integration and the live run is the last check, never the debugger.

The shape of the problem, and why three surfaces have to agree:

  * the STOREFRONT prices an espresso in USDC, under classic SPL Token
  * the WALLET holds USDG, which is Token-2022 — invisible to a classic-SPL balance query
  * the AMM has a pool that converts one into the other, if it can prove it is that pool
  * the PEG ORACLE has an opinion about both mints, and no opinion is not permission

They join on ONE value: the mint. That is the cross-API join — a peg oracle, an AMM and a
storefront program, addressed by the same key, with no symbol translation anywhere.
"""

import pytest

from gecko.pay_route import Quote, assess_payment
from gecko.peg_guard import PegReading
from gecko.pegana import recorded_peg_reader
from gecko.store_accounts import TOKEN_PROGRAM_ID, derive_ata
from gecko.store_directory import StoreProduct

TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
BUYER = "5cjBs5VE8WVVctG2EoUkYiRkW92sXkoT4YsNxszWC9CE"
AUTHORITY = "3i92aBEYCPTVYT8bMYcLdEjcJRP1UBmvPHnUdRDvMrs1"

#: 0.1 USDC — the real price of the espresso at geckocoffee.
PRICE_RAW = 100_000

PEGGED = PegReading(
    tracked=True, symbol="X", state_body={"state": "PEGGED", "stale": False}
)
DEPEGGED = PegReading(
    tracked=True, symbol="USDG", state_body={"state": "DEPEG", "stale": False}
)
STALE = PegReading(
    tracked=True,
    symbol="USDG",
    state_body={"state": "PEGGED", "stale": True, "updated_at": "2026-08-01T00:00:00Z"},
)


class _Espresso:
    """geckocoffee's espresso, priced in USDC under the pinned classic program."""

    def __init__(self, mint: str = USDC, authority: str = AUTHORITY):
        self.store_name = "geckocoffee"
        self.authority = authority
        self.product = StoreProduct(
            name="Espresso", price_raw=PRICE_RAW, decimals=6, mint=mint
        )
        self.receipts = "receipts111"
        self.token_account = derive_ata(authority, mint, token_program=TOKEN_PROGRAM_ID)

    @property
    def mint(self) -> str:
        return self.product.mint


def _pool(amount_in: int) -> Quote:
    return Quote(
        pool="7qbRF6YsyGuLUVs6Y1q64bdVrfe4ZcUUz1JRdoVNUJnm",
        amount_in=amount_in,
        direction="a_to_b",
        liquidity=4_200_000_000,
        tick_spacing=1,
        fee_rate=100,
    )


def _run(*, holdings, peg, venues):
    return assess_payment(
        store=_Espresso(),
        buyer=BUYER,
        holdings=holdings,
        mint_owner=lambda m: TOKEN_2022 if m == USDG else TOKEN_PROGRAM_ID,
        peg_reader=peg,
        idl_fetch=lambda _n: {},
        find_venues=venues,
    )


def test_the_whole_point_a_usdg_wallet_gets_a_checked_route_to_an_espresso() -> None:
    """The headline. One call in, a two-step route out, and every leg checked."""
    report = _run(
        holdings={USDG: (5_000_000, TOKEN_2022)},
        peg=recorded_peg_reader({USDC: PEGGED, USDG: PEGGED}),
        venues=lambda **k: [_pool(101_500)],
    )

    assert report.outcome == "route_found"
    assert report.blocks is False
    assert report.route is not None
    assert report.route.held_mint == USDG
    assert report.route.quote is not None
    # it sizes the SHORTFALL, which is the whole price here — the wallet holds no USDC
    assert report.route.quote.amount_in == 101_500
    assert report.route.quote.amount_in <= 5_000_000  # affordable

    # BOTH sides of the conversion were checked, not just the one being sold
    assert {c.mint for c in report.peg_checks} == {USDC, USDG}
    assert {c.side for c in report.peg_checks} == {"destination", "candidate"}
    assert not any(c.blocks for c in report.peg_checks)

    # and it crosses a boundary as data an agent can act on
    out = report.to_dict()
    assert out["blocked"] is False
    assert (
        out["route"]["quote"]["pool"] == "7qbRF6YsyGuLUVs6Y1q64bdVrfe4ZcUUz1JRdoVNUJnm"
    )
    assert out["peg_evidence_as_of"]


@pytest.mark.parametrize(
    "reading,why",
    [
        (DEPEGGED, "a depegged source"),
        (STALE, "a stale reading — a statement about the past"),
        (PegReading(tracked=None, error="URLError"), "an unreachable oracle"),
        (PegReading(tracked=None, status=429), "a rate-limited oracle"),
        (PegReading(tracked=None, status=200), "a degraded 200 that is not a card"),
    ],
)
def test_the_same_wallet_is_refused_when_the_peg_cannot_vouch(reading, why) -> None:
    """The identical request, refused for five different reasons — and the last three are
    the oracle SAYING NOTHING. Silence is not consent: this is the defect that shipped in
    the script, where an unreachable Pegana read as permission to convert."""
    report = _run(
        holdings={USDG: (5_000_000, TOKEN_2022)},
        peg=recorded_peg_reader({USDC: PEGGED, USDG: reading}),
        venues=lambda **k: [_pool(101_500)],
    )
    assert report.outcome == "peg_blocked", why
    assert report.blocks is True
    assert report.route is None


def test_a_depegged_destination_refuses_even_with_a_healthy_wallet() -> None:
    """The symmetric half. Checking only what you SELL quotes a route INTO a broken peg
    and reports blocked:false beside it."""
    report = _run(
        holdings={USDG: (5_000_000, TOKEN_2022)},
        peg=recorded_peg_reader({USDC: DEPEGGED, USDG: PEGGED}),
        venues=lambda **k: [_pool(101_500)],
    )
    assert report.outcome == "peg_blocked"
    assert USDC in report.reason


def test_a_pool_that_cannot_prove_itself_leaves_no_route() -> None:
    """`find_venues` DROPS a pool that does not re-derive its own address, so an empty
    list here is what a wrong-but-funded pool looks like from this side: no route, never
    a plausible one."""
    report = _run(
        holdings={USDG: (5_000_000, TOKEN_2022)},
        peg=recorded_peg_reader({USDC: PEGGED, USDG: PEGGED}),
        venues=lambda **k: [],
    )
    assert report.outcome == "no_route"
    assert report.blocks is True
    assert USDG in report.no_pool_for


def test_a_wallet_that_cannot_afford_the_swap_says_so_with_the_numbers() -> None:
    report = _run(
        holdings={USDG: (50_000, TOKEN_2022)},
        peg=recorded_peg_reader({USDC: PEGGED, USDG: PEGGED}),
        venues=lambda **k: [_pool(101_500)],
    )
    assert report.outcome == "no_route"
    assert report.rejected_legs
    leg = report.rejected_legs[0]
    assert leg.held_mint == USDG
    assert "101500" in leg.reason and "50000" in leg.reason


def test_holding_the_usdc_already_skips_the_conversion_entirely() -> None:
    report = _run(
        holdings={USDC: (PRICE_RAW, TOKEN_PROGRAM_ID)},
        peg=recorded_peg_reader({USDC: PEGGED}),
        venues=lambda **k: pytest.fail(
            "no venue should be sought when no swap is needed"
        ),
    )
    assert report.outcome == "payable_now"
    assert report.blocks is False


def test_an_espresso_priced_in_token_2022_is_refused_before_the_oracle_is_asked() -> (
    None
):
    """The trap that cost three mainnet swaps: make_purchase PINS classic SPL, so a
    Token-2022 PRICE has no path through the program and no swap rescues it."""
    store = _Espresso(mint=USDG)  # priced in the Token-2022 mint
    report = assess_payment(
        store=store,
        buyer=BUYER,
        holdings={USDG: (5_000_000, TOKEN_2022)},
        mint_owner=lambda m: TOKEN_2022,
        peg_reader=lambda m: pytest.fail("the oracle must not be asked"),
        idl_fetch=lambda _n: pytest.fail("the IDL must not be fetched"),
        find_venues=lambda **k: pytest.fail("no venue must be sought"),
    )
    assert report.outcome == "pinned_program_mismatch"
    assert report.blocks is True

"""Venue derivation: the memcmp proposes, the seed recipe disposes.

A pool that cannot reproduce its own address from its own configuration is DROPPED, not
ranked lower — because the second-best answer here is a real, funded, working pool that
would accept the money and report success.
"""

import base64

import pytest

from gecko import whirlpool_venue as wv

CONFIG = "2LecshUwdy9xi7meFgHtFJQNSKk4KdTrcpvaB56dP2NQ"
MINT_A = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC
MINT_B = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"  # USDG
DISC = bytes([63, 149, 209, 12, 225, 128, 99, 9])


def _idl(extra_field: bool = False, dynamic: bool = False) -> dict:
    fields = [{"name": "whirlpools_config", "type": "pubkey"}]
    if extra_field:
        fields.append({"name": "padding_inserted", "type": "u64"})
    if dynamic:
        fields.append({"name": "label", "type": "string"})
    fields += [
        {"name": "tick_spacing", "type": "u16"},
        {"name": "fee_rate", "type": "u16"},
        {"name": "liquidity", "type": "u128"},
        {"name": "sqrt_price", "type": "u128"},
        {"name": "token_mint_a", "type": "pubkey"},
        {"name": "token_mint_b", "type": "pubkey"},
    ]
    # Anchor shape: `accounts` names the type + discriminator, `types` carries the struct.
    return {
        "accounts": [{"name": "Whirlpool", "discriminator": list(DISC)}],
        "types": [{"name": "Whirlpool", "type": {"kind": "struct", "fields": fields}}],
    }


_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58_decode(value: str) -> bytes:
    """Test-local: the package only ever needs to ENCODE, so this stays out of gecko/."""
    n = 0
    for ch in value:
        n = n * 58 + _ALPHABET.index(ch)
    raw = n.to_bytes(32, "big") if n else b"\x00" * 32
    pad = len(value) - len(value.lstrip("1"))
    return (b"\x00" * pad + raw)[-32:]


def _blob(layout, *, config=CONFIG, a=MINT_A, b=MINT_B, tick=64, liq=10**9) -> bytes:
    size = max(o + w for o, w in layout.fields.values())
    buf = bytearray(size)
    buf[0:8] = DISC

    def put(name, value, *, pub=False):
        off, width = layout.fields[name]
        buf[off : off + width] = (
            _b58_decode(value) if pub else int(value).to_bytes(width, "little")
        )

    put("whirlpools_config", config, pub=True)
    put("token_mint_a", a, pub=True)
    put("token_mint_b", b, pub=True)
    put("tick_spacing", tick)
    put("fee_rate", 300)
    put("liquidity", liq)
    put("sqrt_price", 2**64)
    return bytes(buf)


def _recipe():
    from gecko.provider_config import load_packaged_provider

    _, apis = load_packaged_provider("orquestra")
    return dict(apis["whirlpool"].program.pdas)["whirlpool"]


def _rpc_serving(rows, layout):
    """A fake that HONOURS the memcmp filters.

    Without this it returns every row for both orderings, so a single pool comes back
    twice and the test passes or fails for a reason that has nothing to do with the code.
    """

    def call(url, method, params):
        assert method == "getProgramAccounts"
        wanted = [f["memcmp"] for f in params[1]["filters"]][1:]
        out = []
        for row in rows:
            blob = base64.b64decode(row["account"]["data"][0])
            if all(
                blob[m["offset"] : m["offset"] + 32] == _b58_decode(m["bytes"])
                for m in wanted
            ):
                out.append(row)
        return {"result": out}

    return call


def test_offsets_come_from_the_idl_not_a_table() -> None:
    """Insert a u64 before the mints and every downstream offset must move by 8. A
    hardcoded size table cannot do this, which is why the script's `_SIZES` is deleted."""
    base = wv.whirlpool_layout(_idl())
    shifted = wv.whirlpool_layout(_idl(extra_field=True))
    assert shifted.fields["token_mint_a"][0] == base.fields["token_mint_a"][0] + 8
    assert shifted.discriminator == base.discriminator == DISC


def test_a_dynamic_width_idl_raises_a_venue_error_not_a_layout_error() -> None:
    from gecko.idl_layout import LayoutError

    with pytest.raises(wv.WhirlpoolIdlIncomplete):
        wv.whirlpool_layout(_idl(dynamic=True))
    try:
        wv.whirlpool_layout(_idl(dynamic=True))
    except LayoutError:  # pragma: no cover
        pytest.fail(
            "LayoutError escaped as itself; callers cannot catch what they expect"
        )
    except wv.WhirlpoolIdlIncomplete:
        pass


def test_an_idl_with_no_discriminator_is_refused() -> None:
    idl = _idl()
    idl["accounts"][0].pop("discriminator")
    with pytest.raises(wv.WhirlpoolIdlIncomplete):
        wv.whirlpool_layout(idl)


def test_decoding_returns_a_typed_record_not_a_bare_dict() -> None:
    layout = wv.whirlpool_layout(_idl())
    acct = wv.decode_whirlpool(_blob(layout), layout)
    assert isinstance(acct, wv.WhirlpoolAccount)
    assert acct.whirlpools_config == CONFIG
    assert acct.token_mint_a == MINT_A
    assert acct.tick_spacing == 64
    assert acct.liquidity == 10**9


def test_a_pool_that_does_not_rederive_its_own_address_is_dropped() -> None:
    """Two blobs identical but for the config. Only the self-consistent one survives."""
    from gecko.pda import derive_pda

    layout = wv.whirlpool_layout(_idl())
    good_addr = derive_pda(
        _recipe(),
        {
            "whirlpools_config": CONFIG,
            "token_mint_a": MINT_A,
            "token_mint_b": MINT_B,
            "tick_spacing": 64,
        },
    ).address
    rows = [
        {
            "pubkey": good_addr,
            "account": {"data": [base64.b64encode(_blob(layout)).decode(), "base64"]},
        },
        # same address claimed, but its config says a different pool -> cannot re-derive
        {
            "pubkey": good_addr,
            "account": {
                "data": [
                    base64.b64encode(_blob(layout, config=MINT_B)).decode(),
                    "base64",
                ]
            },
        },
    ]
    venues = wv.find_venues(
        "http://rpc.test",
        MINT_A,
        MINT_B,
        layout=layout,
        recipe=_recipe(),
        rpc_call=_rpc_serving(rows, layout),
    )
    assert len(venues) == 1
    assert venues[0].pool == good_addr
    assert venues[0].verify == "rederived"


def test_a_zero_liquidity_pool_is_excluded_even_when_it_rederives() -> None:
    from gecko.pda import derive_pda

    layout = wv.whirlpool_layout(_idl())
    addr = derive_pda(
        _recipe(),
        {
            "whirlpools_config": CONFIG,
            "token_mint_a": MINT_A,
            "token_mint_b": MINT_B,
            "tick_spacing": 64,
        },
    ).address
    rows = [
        {
            "pubkey": addr,
            "account": {
                "data": [base64.b64encode(_blob(layout, liq=0)).decode(), "base64"]
            },
        }
    ]
    assert (
        wv.find_venues(
            "http://rpc.test",
            MINT_A,
            MINT_B,
            layout=layout,
            recipe=_recipe(),
            rpc_call=_rpc_serving(rows, layout),
        )
        == []
    )


def test_both_orderings_are_queried_and_carry_their_direction() -> None:
    layout = wv.whirlpool_layout(_idl())
    seen = []

    def call(url, method, params):
        filters = params[1]["filters"]
        seen.append((filters[1]["memcmp"]["bytes"], filters[2]["memcmp"]["bytes"]))
        return {"result": []}

    wv.find_venues(
        "http://rpc.test",
        MINT_A,
        MINT_B,
        layout=layout,
        recipe=_recipe(),
        rpc_call=call,
    )
    assert seen == [(MINT_A, MINT_B), (MINT_B, MINT_A)]


def test_the_reversed_ordering_reports_b_to_a() -> None:
    """The pool stores (MINT_A, MINT_B). A caller HOLDING MINT_B is selling the b side,
    so the ordering that matched is the one `swap_v2` has to be told about."""
    from gecko.pda import derive_pda

    layout = wv.whirlpool_layout(_idl())
    addr = derive_pda(
        _recipe(),
        {
            "whirlpools_config": CONFIG,
            "token_mint_a": MINT_A,
            "token_mint_b": MINT_B,
            "tick_spacing": 64,
        },
    ).address
    rows = [
        {
            "pubkey": addr,
            "account": {"data": [base64.b64encode(_blob(layout)).decode(), "base64"]},
        }
    ]
    venues = wv.find_venues(
        "http://rpc.test",
        MINT_B,  # held
        MINT_A,  # needed
        layout=layout,
        recipe=_recipe(),
        rpc_call=_rpc_serving(rows, layout),
    )
    assert len(venues) == 1
    assert venues[0].direction == "b_to_a"


def test_venues_come_back_richest_first() -> None:
    from gecko.pda import derive_pda

    layout = wv.whirlpool_layout(_idl())
    rows = []
    for tick, liq in ((64, 5), (128, 500)):
        addr = derive_pda(
            _recipe(),
            {
                "whirlpools_config": CONFIG,
                "token_mint_a": MINT_A,
                "token_mint_b": MINT_B,
                "tick_spacing": tick,
            },
        ).address
        rows.append(
            {
                "pubkey": addr,
                "account": {
                    "data": [
                        base64.b64encode(_blob(layout, tick=tick, liq=liq)).decode(),
                        "base64",
                    ]
                },
            }
        )
    venues = wv.find_venues(
        "http://rpc.test",
        MINT_A,
        MINT_B,
        layout=layout,
        recipe=_recipe(),
        rpc_call=_rpc_serving(rows, layout),
    )
    assert [v.liquidity for v in venues] == [500, 5]


# --- tick arrays: WHICH three, and in which direction ---------------------------------
#
# tests/test_whirlpool_config.py already pins that the tick_array PDA RECIPE is right —
# it derives the three accounts Jupiter's router passes for a real pool. What it does not
# cover is the function that decides WHICH three: the arithmetic on tick_current and
# tick_spacing, and the direction of travel. That lived in a 260-line `main()` inside
# scripts/prepare_whirlpool_swap.py, where nothing could call it, so a wrong direction or
# a wrong span would produce three real, derivable, well-formed accounts for the wrong
# region of the curve — the swap fails at the program, not at the derivation.

POOL = "9RqDTfwCx2SgxsvKpspQHc38HUo3B6hRd3oR9JR966Ps"  # the pool test_whirlpool_config pins


def test_the_three_arrays_match_the_ones_a_real_swap_passed() -> None:
    """Anchored to the same mainnet accounts test_whirlpool_config pins: Jupiter's router
    at tick_current=2, tick_spacing=1, travelling DOWN."""
    got = wv.tick_arrays(POOL, tick_current=2, tick_spacing=1, upward=False)
    assert got == [
        "2QRj3Ug2RZ9ffSCP3pp7U6ex45adrnMW7u5HAihfH2mE",  # start 0
        "6o9yaeyc8rHKKbdRxN8M3F9Qii5zpBC33gH2L1GUNBPj",  # start -88
        "94cnSkfZfnpkS8yBs1XuqugRUJWruTkMhMPPPfiBdapm",  # start -176
    ]


def test_direction_of_travel_changes_which_arrays_are_selected() -> None:
    up = wv.tick_arrays(POOL, tick_current=2, tick_spacing=1, upward=True)
    down = wv.tick_arrays(POOL, tick_current=2, tick_spacing=1, upward=False)
    assert up[0] == down[0], "both directions start from the array containing the tick"
    assert up[1:] != down[1:], "but they must then travel opposite ways"


def test_the_start_index_floors_toward_negative_infinity() -> None:
    """The trap. A tick BELOW zero must land in the array that CONTAINS it, and Python's
    `//` floors toward -inf, which is the behaviour this needs. Truncating toward zero (C
    semantics, or int(a/b)) puts a tick of -1 in array 0 instead of array -88, deriving a
    real account for the wrong region."""
    assert wv.tick_array_start(-1, tick_spacing=1) == -88
    assert wv.tick_array_start(0, tick_spacing=1) == 0
    assert wv.tick_array_start(-88, tick_spacing=1) == -88
    assert wv.tick_array_start(-89, tick_spacing=1) == -176
    assert wv.tick_array_start(87, tick_spacing=1) == 0
    assert wv.tick_array_start(88, tick_spacing=1) == 88


def test_the_span_scales_with_tick_spacing() -> None:
    """88 ticks per array is a count of TICKS, not of price steps — the byte span depends
    on the pool's spacing, so a fixed 88 would be wrong for every pool but spacing=1."""
    assert wv.tick_array_start(0, tick_spacing=64) == 0
    assert wv.tick_array_start(5631, tick_spacing=64) == 0  # 88*64 = 5632
    assert wv.tick_array_start(5632, tick_spacing=64) == 5632
    assert wv.tick_array_start(-1, tick_spacing=64) == -5632


def test_it_always_returns_exactly_three_distinct_arrays() -> None:
    for spacing in (1, 8, 64, 128):
        for tick in (-100_000, -1, 0, 1, 100_000):
            got = wv.tick_arrays(
                POOL, tick_current=tick, tick_spacing=spacing, upward=True
            )
            assert len(got) == 3
            assert len(set(got)) == 3, (
                f"duplicate arrays at spacing={spacing} tick={tick}"
            )

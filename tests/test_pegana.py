"""The fetch half of the peg gate: what came back from the wire, before any judgement.

Every test here drives an injected getter, so the whole module is falsifiable offline at
$0. The one thing being pinned throughout: `tracked` is established from the HTTP STATUS
and never from the shape of a 200 body.
"""

import json
import urllib.error

import pytest

from gecko import pegana
from gecko.peg_guard import verdict_from_reading

MINT = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"  # USDG
CARD = json.dumps({"symbol": "USDG", "asset": "USDG"})
STATE = json.dumps({"state": "PEGGED", "stale": False})


def _reader(script):
    """`script` maps a URL substring -> (status, body) or an exception to raise."""
    seen = []

    def get(url: str) -> tuple[int, str]:
        seen.append(url)
        for key, item in script.items():
            if key in url:
                if isinstance(item, BaseException):
                    raise item
                return item
        raise AssertionError(f"unscripted url: {url}")

    return pegana.pegana_reader(get=get), seen


def test_an_unreachable_card_fetch_reads_as_could_not_ask() -> None:
    read, _ = _reader({"by-mint": urllib.error.URLError("dns")})
    r = read(MINT)
    assert r.tracked is None
    assert r.error == "URLError"
    # the error carries a CLASS name only — never a url, a body, or a token
    assert "http" not in (r.error or "").lower()
    assert MINT not in (r.error or "")


def test_a_404_is_the_only_thing_that_means_not_tracked() -> None:
    read, _ = _reader({"by-mint": (404, "")})
    r = read(MINT)
    assert r.tracked is False
    assert r.status == 404
    assert verdict_from_reading(MINT, r).blocks is False


@pytest.mark.parametrize(
    "body",
    [
        "",  # empty
        "{}",  # a 200 with nothing in it
        '{"data":{"symbol":"USDG"}}',  # a schema change that wraps the card
        '{"error":"rate limited"}',  # a rate-limit served as 200
        "<html>maintenance</html>",  # a WAF/gateway envelope
        '{"symbol":""}',  # present but empty
        '{"symbol":123}',  # present but not a string
        "[]",  # valid json, wrong type
    ],
)
def test_a_degraded_200_is_could_not_ask_not_untracked(body: str) -> None:
    """The whole point. Reading `tracked=False` off a body shape lets any degraded 200
    forge "Pegana has no opinion", which is a fail-open wearing a fix's clothes."""
    read, _ = _reader({"by-mint": (200, body)})
    r = read(MINT)
    assert r.tracked is None, f"a degraded 200 forged 'untracked' for: {body!r}"
    assert verdict_from_reading(MINT, r).blocks is True


@pytest.mark.parametrize("code", [429, 500, 502, 503, 403, 401])
def test_a_non_404_error_status_is_could_not_ask(code: int) -> None:
    read, _ = _reader({"by-mint": (code, "")})
    r = read(MINT)
    assert r.tracked is None
    assert r.status == code
    assert verdict_from_reading(MINT, r).blocks is True


def test_a_resolved_card_with_a_failing_state_read_stays_tracked() -> None:
    read, _ = _reader(
        {"by-mint": (200, CARD), "/state": urllib.error.HTTPError(
            "u", 500, "boom", None, None
        )}
    )
    r = read(MINT)
    assert r.tracked is True
    assert r.symbol == "USDG"
    assert r.state_body is None
    assert r.error == "HTTPError"
    v = verdict_from_reading(MINT, r)
    assert v.blocks is True
    assert "USDG" in v.reason
    assert "does not track" not in v.reason


def test_a_healthy_pair_of_reads_is_a_normal_verdict() -> None:
    read, seen = _reader({"by-mint": (200, CARD), "/state": (200, STATE)})
    r = read(MINT)
    assert (r.tracked, r.symbol, r.state_body) == (True, "USDG", {"state": "PEGGED", "stale": False})
    assert verdict_from_reading(MINT, r).outcome == "ok"
    assert len(seen) == 2


def test_a_state_read_that_is_not_a_dict_does_not_become_a_peg_opinion() -> None:
    read, _ = _reader({"by-mint": (200, CARD), "/state": (200, "[]")})
    r = read(MINT)
    assert r.tracked is True
    assert r.state_body is None
    assert verdict_from_reading(MINT, r).blocks is True


def test_a_programming_error_is_not_swallowed() -> None:
    """Only transport classes are caught. A TypeError here is our bug, and swallowing it
    would report a broken reader as a peg opinion."""
    read, _ = _reader({"by-mint": TypeError("bug")})
    with pytest.raises(TypeError):
        read(MINT)


def test_the_mint_is_shape_validated_before_it_reaches_the_getter() -> None:
    reached = []

    def get(url: str) -> tuple[int, str]:
        reached.append(url)
        return (200, CARD)

    read = pegana.pegana_reader(get=get)
    for bad in ("../../etc/passwd", "a/b", "mint?x=1", "", "O0Il" * 12, "x" * 200):
        with pytest.raises(pegana.PeganaError):
            read(bad)
    assert reached == [], "a malformed mint reached the network"


def test_a_mint_absent_from_the_recorded_reader_blocks() -> None:
    """The $0 lane must not be more permissive than the live one — it is the lane every
    other test in this change runs in."""
    read = pegana.recorded_peg_reader({})
    r = read(MINT)
    assert r.tracked is None
    assert verdict_from_reading(MINT, r).blocks is True


def test_the_recorded_reader_has_no_way_to_weaken_the_default() -> None:
    import inspect

    params = inspect.signature(pegana.recorded_peg_reader).parameters
    assert "default_tracked" not in params
    assert list(params) == ["readings"]


def test_the_recorded_reader_serves_what_it_was_given() -> None:
    read = pegana.recorded_peg_reader(
        {MINT: pegana.PegReading(tracked=True, symbol="USDG", state_body={"state": "DEPEG"})}
    )
    assert verdict_from_reading(MINT, read(MINT)).outcome == "refuse"


def test_the_default_getter_is_the_ssrf_safe_one() -> None:
    """The script this replaces called urllib.request.urlopen directly — no scheme check,
    no IP check. The default must be netguard's."""
    import gecko.netguard as netguard

    assert pegana._default_getter().func is netguard.safe_get_status

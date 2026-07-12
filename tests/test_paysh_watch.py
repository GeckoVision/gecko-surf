"""pay.sh self-refresh drift-watch — Tier-1 sha-diff + Tier-2 challenge-only re-probe.

Light fakes only: an injected catalog fetcher (a JSON string), an injected drift probe (a
status-code function), and an injected clock. No test touches the network.

Asserts the loop:
  * re-comprehends ONLY changed shas (Tier-1),
  * logs a verified→broken transition on a non-402, recovers on 402 (Tier-2),
  * threads the injected clock into the transition line (never a wall-clock call),
  * survives a fetch exception (the loop keeps ticking).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from urllib.parse import urlsplit

from gecko.paysh_catalog import CatalogRegistry, ProbeFn, fetch_catalog
from gecko.paysh_watch import refresh_seconds, run_tick, watch_loop

# --- Fake catalog: the 2 verified providers (real fqns) + 2 pending. ------------------

_COINGECKO_URL = "https://pro-api.coingecko.com/api/v3/x402/onchain"
_PERPLEXITY_URL = "https://pplx.x402.paysponge.com"


def _provider(
    fqn: str, service_url: str, sha: str, **over: object
) -> dict[str, object]:
    base: dict[str, object] = {
        "fqn": fqn,
        "title": fqn.split("/")[-1].title(),
        "service_url": service_url,
        "description": "desc",
        "use_case": "use it",
        "category": "finance",
        "endpoint_count": 3,
        "has_metering": True,
        "has_free_tier": False,
        "min_price_usd": 0.01,
        "max_price_usd": 0.01,
        "sha": sha,
    }
    base.update(over)
    return base


_PROVIDERS = [
    _provider("paysponge/coingecko", _COINGECKO_URL, "sha-cg-1"),
    _provider("paysponge/perplexity", _PERPLEXITY_URL, "sha-px-1"),
    _provider("birdeye/data", "https://public-api.birdeye.so", "sha-be-1"),
    _provider("clustly/tipping", "https://tip.clustly.ai", "sha-cl-1"),
]


def _fetcher(providers: list[dict[str, object]]):
    payload = json.dumps({"version": 2, "providers": providers})
    return lambda _url: payload


def _fetch(providers: list[dict[str, object]]):
    return lambda: fetch_catalog(fetcher=_fetcher(providers))


def _registry(providers: list[dict[str, object]] | None = None) -> CatalogRegistry:
    return CatalogRegistry.build(_fetch(providers or _PROVIDERS)())


def _probe_returning(mapping: dict[str, int | None]) -> ProbeFn:
    def probe(req: object) -> int | None:
        host = (urlsplit(req.url).hostname or "").lower()  # type: ignore[attr-defined]
        return mapping.get(host)

    return probe


_ALL_402 = {"pro-api.coingecko.com": 402, "pplx.x402.paysponge.com": 402}
_CG_DOWN = {"pro-api.coingecko.com": 404, "pplx.x402.paysponge.com": 402}


# --- Tier 1: a tick re-comprehends ONLY the changed sha -------------------------------


def test_tick_refreshes_only_changed_shas() -> None:
    reg = _registry()
    before = {ps.entry.fqn: ps.client for ps in reg.providers()}

    bumped = [p.copy() for p in _PROVIDERS]
    bumped[2]["sha"] = "sha-be-2"  # only birdeye's content changed

    result = run_tick(
        reg, fetch=_fetch(bumped), probe=_probe_returning(_ALL_402), now=lambda: 1000.0
    )
    assert result.refresh is not None
    assert result.refresh.changed == ["birdeye/data"]
    assert result.refresh.added == [] and result.refresh.removed == []

    after = {ps.entry.fqn: ps.client for ps in reg.providers()}
    # unchanged providers keep their EXACT comprehended client; only birdeye is rebuilt.
    assert after["paysponge/coingecko"] is before["paysponge/coingecko"]
    assert after["birdeye/data"] is not before["birdeye/data"]


# --- Tier 2: a tick logs verified→broken on a non-402, recovers on 402 ----------------


def test_tick_logs_verified_to_broken_transition() -> None:
    reg = _registry()
    lines: list[str] = []
    result = run_tick(
        reg,
        fetch=_fetch(_PROVIDERS),
        probe=_probe_returning(_CG_DOWN),
        now=lambda: 1000.0,  # -> 1970-01-01T00:16:40+00:00
        sink=lines.append,
    )
    # exactly one transition: coingecko flipped verified -> broken (probe 404).
    assert [t.fqn for t in result.transitions] == ["paysponge/coingecko"]
    assert lines == [
        "paysh drift: paysponge/coingecko verified→broken (probe=404) "
        "at 1970-01-01T00:16:40+00:00"
    ]
    # persisted in the live surface: broken is no longer first-call-correct.
    assert reg.get("paysponge/coingecko").status == "broken"
    assert reg.counts()["verified"] == 1


def test_tick_recovers_broken_to_verified() -> None:
    reg = _registry()
    run_tick(reg, fetch=_fetch(_PROVIDERS), probe=_probe_returning(_CG_DOWN))
    assert reg.get("paysponge/coingecko").status == "broken"

    lines: list[str] = []
    run_tick(
        reg,
        fetch=_fetch(_PROVIDERS),
        probe=_probe_returning(_ALL_402),
        now=lambda: 2000.0,
        sink=lines.append,
    )
    assert reg.get("paysponge/coingecko").status == "verified"
    assert lines == [
        "paysh drift: paysponge/coingecko broken→verified (probe=402) "
        "at 1970-01-01T00:33:20+00:00"
    ]


def test_tick_no_transition_when_status_holds() -> None:
    reg = _registry()
    lines: list[str] = []
    result = run_tick(
        reg,
        fetch=_fetch(_PROVIDERS),
        probe=_probe_returning(_ALL_402),
        sink=lines.append,
    )
    assert result.transitions == []
    assert lines == []  # steady state is silent — only transitions are logged


# --- Resilience: a fetch exception is caught; the tick + loop keep going --------------


def test_tick_survives_a_fetch_exception() -> None:
    reg = _registry()

    def boom() -> list:
        raise ConnectionError("catalog unreachable")

    before = {ps.entry.fqn for ps in reg.providers()}
    result = run_tick(reg, fetch=boom, probe=_probe_returning(_CG_DOWN))
    assert result.fetch_failed is True
    assert result.transitions == []
    # the surface is untouched — a failed fetch never mutates or breaks providers.
    assert {ps.entry.fqn for ps in reg.providers()} == before
    assert reg.counts()["broken"] == 0


def test_watch_loop_survives_fetch_error_and_cancels_clean() -> None:
    """The loop keeps ticking after a fetch raises, catches the drift on a later tick,
    and cancels cleanly on shutdown."""

    async def _run() -> tuple[int, list[str]]:
        reg = _registry()
        lines: list[str] = []
        calls = {"n": 0}

        def flaky() -> list:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("first tick down")
            return fetch_catalog(fetcher=_fetcher(_PROVIDERS))

        task = asyncio.create_task(
            watch_loop(
                reg,
                interval=0.001,
                fetch=flaky,
                probe=_probe_returning(_CG_DOWN),
                sink=lines.append,
            )
        )
        # Wait for the broken transition (proves the loop survived the first exception).
        for _ in range(400):
            if any("verified→broken" in ln for ln in lines):
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return calls["n"], lines

    n_calls, lines = asyncio.run(_run())
    assert n_calls >= 2  # survived the first-tick fetch exception
    assert any(
        "paysh drift: paysponge/coingecko verified→broken (probe=404)" in ln
        for ln in lines
    )


# --- Config knob ----------------------------------------------------------------------


def test_refresh_seconds_env_and_default() -> None:
    assert refresh_seconds({}) == 3600
    assert refresh_seconds({"PAYSH_REFRESH_SECONDS": "60"}) == 60
    assert (
        refresh_seconds({"PAYSH_REFRESH_SECONDS": "0"}) == 3600
    )  # non-positive ignored
    assert refresh_seconds({"PAYSH_REFRESH_SECONDS": "xyz"}) == 3600

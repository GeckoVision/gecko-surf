"""Tier 1 probe and the per-operation payTo pin, offline.

The host is a fake ``HeaderFetch`` answering with a v2 ``payment-required`` header shaped
like the ones measured on Orthogonal's x402 host on 9 Oct, where scrapecreators and fiber
name DIFFERENT Solana payTos. Nothing here touches a network or a real store.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from gecko.servicemap.ids import op_id, sighting_id, url_template
from gecko.servicemap.pins import (
    SOLANA_USDC,
    PayToPinError,
    confirm_pay_to,
    op_for_call,
    pinned_pay_to,
    pinned_pay_tos,
)
from gecko.servicemap.observe import record_probe, sweep
from gecko.servicemap.probe import (
    ProbeTarget,
    Throttle,
    probe_all,
    probe_targets,
)
from gecko.servicemap.registry import MemoryRegistry
from gecko.servicemap.types import Claim, Operation, Service, Sighting
from gecko.x402_svm import SOLANA_MAINNET

HOST = "https://x402.example"
SCRAPE = "4Bf6zpkrv4gtirAzM3kzqXrRRLXNmbKrpHLebTpAk3Wg"
FIBER = "DHZJ3PpyVtaomjEiHVwzL982prM2LeENNuHiFMxJy4NC"
OTHER = "FqQtW9mNLSbicvbhgdZHj7vpdxniJKLXsgmvDm3F46U4"
T0 = "2026-10-09T00:00:00+00:00"
T1 = "2026-10-09T01:00:00+00:00"
T2 = "2026-10-10T02:00:00+00:00"  # past a 24h hold from T1


def _listing(api: str, method: str, path: str) -> tuple[Service, Operation, Sighting]:
    op = op_id(method, url_template(api, path))
    claim = Claim("t", "d", "u", 2, None, False, (), None)
    return (
        Service(api, api, None, T0, T0),
        Operation(op, api, method, url_template(api, path), T0, T0),
        Sighting(
            sighting_id("orthogonal", api, method, path),
            op,
            api,
            "orthogonal",
            api,
            method,
            path,
            "details.json",
            None,
            claim,
            T0,
            T0,
        ),
    )


LISTINGS = [
    ("scrapecreators", "GET", "/v2/instagram/post/comments"),
    ("fiber", "POST", "/v1/people-search"),
    ("scrapecreators", "GET", "/v1/tiktok/profile/{handle}"),
]


def _registry() -> MemoryRegistry:
    registry = MemoryRegistry()
    for api, method, path in LISTINGS:
        service, op, sighting = _listing(api, method, path)
        registry.put_service(service)
        registry.put_operation(op)
        registry.put_sighting(sighting)
    return registry


def _offer(pay_to: str, amount: int = 20_000, network: str = SOLANA_MAINNET) -> dict:
    return {
        "scheme": "exact",
        "network": network,
        "asset": SOLANA_USDC if network == SOLANA_MAINNET else "0xusdc",
        "amount": str(amount),
        "payTo": pay_to,
        "maxTimeoutSeconds": 60,
        "extra": {"feePayer": OTHER},
    }


def _challenge(*offers: dict) -> str:
    body = {"x402Version": 2, "accepts": list(offers), "resource": None}
    return base64.b64encode(json.dumps(body).encode()).decode()


@dataclass
class FakeHost:
    """Answers 402 with a per-API payTo; records (url, method), never sends a body."""

    pay_to: dict[str, str] = field(
        default_factory=lambda: {"scrapecreators": SCRAPE, "fiber": FIBER}
    )
    amount: int = 20_000
    status: int = 402
    seen: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, url: str, method: str) -> tuple[int, Mapping[str, str]]:
        self.seen.append((url, method))
        api = url.removeprefix(HOST + "/").split("/", 1)[0]
        if self.status != 402:
            return self.status, {}
        offers = [
            _offer("0xbase", network="eip155:8453"),
            _offer(self.pay_to[api], self.amount),
        ]
        return 402, {"payment-required": _challenge(*offers)}


@dataclass
class FakeClock:
    now: float = 100.0
    slept: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _sweep(registry: MemoryRegistry, host: FakeHost, at: str, **kw: Any) -> list[Any]:
    targets, _ = probe_targets(registry, {"orthogonal": HOST}, **kw)
    clock = FakeClock()
    outcomes = probe_all(
        targets,
        fetch=host,
        throttle=Throttle(clock=clock.clock, sleep=clock.sleep),
    )
    return [record_probe(o, read=registry, write=registry, at=at) for o in outcomes]


def _op(api: str, method: str, path: str) -> str:
    return op_for_call(method, api, path)


SCRAPE_OP = _op("scrapecreators", "GET", "/v2/instagram/post/comments")
FIBER_OP = _op("fiber", "POST", "/v1/people-search")


# --- targets and politeness -----------------------------------------------------------


def test_targets_are_the_x402_route_of_each_listed_operation() -> None:
    targets, skipped = probe_targets(_registry(), {"orthogonal": HOST})
    assert sorted((t.url, t.method) for t in targets) == [
        (f"{HOST}/fiber/v1/people-search", "POST"),
        (f"{HOST}/scrapecreators/v2/instagram/post/comments", "GET"),
    ]
    assert len(skipped) == 1 and "placeholder" in skipped[0]
    only, _ = probe_targets(_registry(), {"orthogonal": HOST}, only="fiber")
    assert [t.service_id for t in only] == ["fiber"]
    assert probe_targets(_registry(), {})[0] == []


def test_one_request_per_second_per_host_and_at_most_limit() -> None:
    clock = FakeClock()
    host = FakeHost()
    same_host = [
        ProbeTarget(f"op{i}", "scrapecreators", "GET", f"{HOST}/scrapecreators/x{i}")
        for i in range(4)
    ]
    outcomes = probe_all(
        same_host,
        fetch=host,
        throttle=Throttle(clock=clock.clock, sleep=clock.sleep),
        limit=3,
    )
    assert len(outcomes) == 3 and len(host.seen) == 3
    assert clock.slept == [1.0, 1.0]
    assert probe_all(same_host, fetch=host, limit=0) == []


# --- what a probe writes --------------------------------------------------------------


def test_a_first_probe_records_every_offer_and_health_without_events() -> None:
    registry = _registry()
    recorded = _sweep(registry, FakeHost(), T0)
    assert all(r.health.status == "up" and r.health.tier == 1 for r in recorded)
    assert all(r.events == () for r in recorded)
    assert {o.pay_to for o in registry.offers_for(FIBER_OP)} == {"0xbase", FIBER}
    assert registry.events() == []


def test_the_payer_pin_is_per_operation() -> None:
    """The live refusal of 9 Oct: one host, two providers, two payTos."""
    registry = _registry()
    _sweep(registry, FakeHost(), T0)
    assert pinned_pay_to(registry, SCRAPE_OP) == SCRAPE
    assert pinned_pay_to(registry, FIBER_OP) == FIBER
    assert pinned_pay_tos(registry) == {
        SCRAPE: ("scrapecreators",),
        FIBER: ("fiber",),
    }
    assert pinned_pay_tos(registry, only=["fiber"]) == {FIBER: ("fiber",)}


def test_no_probed_offer_is_no_pin() -> None:
    with pytest.raises(PayToPinError, match="no probed offer"):
        pinned_pay_to(_registry(), SCRAPE_OP)


def test_a_price_change_is_an_event_and_not_a_quarantine() -> None:
    registry = _registry()
    _sweep(registry, FakeHost(), T0)
    _sweep(registry, FakeHost(amount=30_000), T1, only="scrapecreators")
    [event] = registry.events()
    assert (event.kind, event.before, event.after) == ("price", "20000", "30000")
    assert pinned_pay_to(registry, SCRAPE_OP) == SCRAPE


def test_a_changed_pay_to_is_quarantined_until_a_probe_past_the_hold() -> None:
    registry = _registry()
    _sweep(registry, FakeHost(), T0)
    moved = FakeHost(pay_to={"scrapecreators": OTHER, "fiber": FIBER})
    _sweep(registry, moved, T1, only="scrapecreators")
    kinds = [(e.kind, e.before, e.after) for e in registry.events()]
    assert ("pay_to", SCRAPE, OTHER) in kinds
    assert ("status", "up", "quarantined") in kinds
    with pytest.raises(PayToPinError, match="quarantined"):
        pinned_pay_to(registry, SCRAPE_OP)
    assert SCRAPE not in pinned_pay_tos(registry) and OTHER not in pinned_pay_tos(
        registry
    )
    # Inside the hold, a matching probe does not lift it.
    _sweep(registry, moved, T1, only="scrapecreators")
    with pytest.raises(PayToPinError):
        pinned_pay_to(registry, SCRAPE_OP)
    # Past the hold, the same address again lifts it.
    _sweep(registry, moved, T2, only="scrapecreators")
    assert pinned_pay_to(registry, SCRAPE_OP) == OTHER
    # The untouched provider was never quarantined.
    assert pinned_pay_to(registry, FIBER_OP) == FIBER


def test_the_founder_confirms_only_the_latest_address() -> None:
    registry = _registry()
    _sweep(registry, FakeHost(), T0)
    _sweep(registry, FakeHost(pay_to={"scrapecreators": OTHER, "fiber": FIBER}), T1)
    assert confirm_pay_to(registry, "scrapecreators", SCRAPE, at=T1) == []
    assert confirm_pay_to(registry, "scrapecreators", OTHER, at=T1) == [SCRAPE_OP]
    assert pinned_pay_to(registry, SCRAPE_OP) == OTHER
    assert registry.events()[-1].note == "payTo confirmed by the founder"


def test_no_challenge_is_health_not_an_offer() -> None:
    registry = _registry()
    _sweep(registry, FakeHost(status=404), T0, only="fiber")
    health = registry.health_for(FIBER_OP)
    assert health is not None and health.status == "unknown"
    assert health.last_http_status == 404 and health.consecutive_failures == 1
    _sweep(registry, FakeHost(status=503), T1, only="fiber")
    health = registry.health_for(FIBER_OP)
    assert health is not None and health.status == "down"
    assert health.consecutive_failures == 2
    assert registry.offers_for(FIBER_OP) == []


def test_a_dry_run_reads_one_registry_and_writes_another() -> None:
    held = _registry()
    scratch = MemoryRegistry()
    targets, _ = probe_targets(held, {"orthogonal": HOST})
    for outcome in probe_all(targets, fetch=FakeHost(), throttle=_no_wait()):
        record_probe(outcome, read=held, write=scratch, at=T0)
    assert held.offers() == [] and held.health() == []
    assert len(scratch.health()) == 2


def _no_wait() -> Throttle:
    clock = FakeClock()
    return Throttle(clock=clock.clock, sleep=clock.sleep)


def test_the_local_usdc_mint_is_the_purchase_paths() -> None:
    from gecko.autonomous_purchase import USDC_MINT

    assert SOLANA_USDC == USDC_MINT


def test_a_sweep_records_every_target_and_reports_in_our_words() -> None:

    registry = _registry()
    report = sweep(
        read=registry,
        write=registry,
        hosts={"orthogonal": HOST},
        at=T0,
        limit=1,
        fetch=FakeHost(),
        throttle=_no_wait(),
    )
    assert len(report.probed) == 1 and report.deferred == 1
    lines = report.lines()
    assert lines[0].startswith(f"POST {HOST}/fiber/v1/people-search -> 402 up")
    assert FIBER in lines[0]
    assert any(line.startswith("skipped scrapecreators") for line in lines)


def test_a_killed_registry_is_refused_before_any_request() -> None:
    from gecko.servicemap.registry import RegistryKilledError

    host = FakeHost()
    with pytest.raises(RegistryKilledError):
        sweep(
            read=_registry(),
            write=MemoryRegistry(kill_switch=True),
            hosts={"orthogonal": HOST},
            at=T0,
            fetch=host,
        )
    assert host.seen == []


# --- the founder script ---------------------------------------------------------------


@pytest.fixture
def script_rig(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, MemoryRegistry, FakeHost]:
    import gecko.servicemap.probe as probe_module
    from scripts import servicemap_probe as script

    held = _registry()
    host = FakeHost()

    def fetch(url: str, method: str) -> tuple[int, Mapping[str, str]]:
        return host(url.replace("https://x402.orthogonal.com", HOST), method)

    monkeypatch.setattr(script, "_mongo_registry", lambda: held)
    monkeypatch.setattr(probe_module, "_live_fetch", fetch)
    monkeypatch.setattr(probe_module.time, "sleep", lambda s: None)
    return script, held, host


def test_the_probe_script_is_dry_by_default(script_rig: Any, capsys: Any) -> None:
    script, held, host = script_rig
    assert script.main(["--only", "fiber"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("DRY RUN") and FIBER in out
    assert held.offers() == [] and held.health() == []
    assert [m for _, m in host.seen] == ["POST"]


def test_the_probe_script_writes_with_write_and_lifts_by_confirm(
    script_rig: Any, capsys: Any
) -> None:
    script, held, host = script_rig
    assert script.main(["--write"]) == 0
    assert pinned_pay_to(held, SCRAPE_OP) == SCRAPE
    host.pay_to["scrapecreators"] = OTHER
    assert script.main(["--write", "--only", "scrapecreators"]) == 0
    with pytest.raises(PayToPinError):
        pinned_pay_to(held, SCRAPE_OP)
    assert script.main(["--confirm-pay-to", f"scrapecreators={OTHER}"]) == 2
    sent = len(host.seen)
    assert script.main(["--confirm-pay-to", f"scrapecreators={OTHER}", "--write"]) == 0
    assert len(host.seen) == sent, "a confirmation sends no probe"
    assert pinned_pay_to(held, SCRAPE_OP) == OTHER

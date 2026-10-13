"""Readers, the importer and the snapshot adapter, on the committed any-query fixtures.

The parity tests are the point of day 1: a registry imported from the files must give
the planner the SAME pool, sha and drop counts that ``load_catalog`` gives from those
files today. Nothing here touches a network or a real store.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from gecko.anyquery.candidates import fold_path
from gecko.anyquery.catalog_load import load_catalog, read_jsonl
from gecko.servicemap.importer import import_files
from gecko.servicemap.readers import (
    observed_fields,
    orthogonal_sightings,
    paysh_entries_from_text,
    paysh_sightings,
    ReadReport,
)
from gecko.servicemap.registry import MemoryRegistry, RegistryKilledError
from gecko.servicemap.snapshot import (
    CatalogSourceError,
    catalog_source,
    load_from_registry,
)
from gecko.servicemap.types import Health

FIXTURES = Path(__file__).parent / "fixtures" / "anyquery"
LEDGER = FIXTURES / "observed_fields.jsonl"
NOW = "2026-10-10T00:00:00+00:00"

PAYSH_TEXT = json.dumps(
    {
        "providers": [
            {
                "fqn": "paysponge/coingecko",
                "service_url": "https://pro-api.coingecko.example",
                "title": "CoinGecko",
                "description": "Token prices and pools.",
                "use_case": "price lookups",
                "min_price_usd": 0.01,
                "max_price_usd": 0.05,
            },
            {
                "fqn": "acme/weather",
                "service_url": "https://weather.example",
                "title": "Weather",
                "description": "Forecasts.",
                "max_price_usd": 0.003,
            },
            {"fqn": "broken", "service_url": "file:///etc/passwd"},
        ]
    }
)


def _records() -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / "orthogonal_details_subset.json").read_text("utf-8"))
    return list(data["records"])


def _imported(**kw: Any) -> MemoryRegistry:
    registry = MemoryRegistry()
    import_files(registry, details=_records(), ledgers=[LEDGER], now=NOW, **kw)
    return registry


# --- parity with load_catalog ---------------------------------------------------------


def test_the_registry_gives_the_planner_the_same_pool_as_the_files() -> None:
    files = load_catalog(_records(), ledgers=[LEDGER])
    registry = load_from_registry(_imported(), now=NOW)

    assert registry.candidates == files.candidates
    assert registry.candidates.snapshot_sha == files.candidates.snapshot_sha
    assert registry.report == files.report


def test_confirmed_differs_only_by_envelope_pairs_no_plan_can_use() -> None:
    files = load_catalog(_records(), ledgers=[LEDGER])
    registry = load_from_registry(_imported(), now=NOW)

    assert registry.confirmed == {p for p in files.confirmed if fold_path(p[1])}
    usable = {
        (c.candidate_id, path)
        for c in files.candidates.candidates
        for path in c.response_paths
    }
    assert files.confirmed & usable == registry.confirmed & usable


def test_parity_holds_with_the_paysh_catalog_too(tmp_path: Path) -> None:
    saved = tmp_path / "paysh.json"
    saved.write_text(PAYSH_TEXT, encoding="utf-8")
    entries = paysh_entries_from_text(PAYSH_TEXT)
    files = load_catalog(_records(), ledgers=[LEDGER], paysh=entries)

    registry = load_from_registry(_imported(paysh_catalog=saved), now=NOW)

    assert registry.candidates == files.candidates
    paysh = [c for c in registry.candidates.candidates if c.catalog == "paysh"]
    assert {c.api for c in paysh} == {"paysponge/coingecko", "acme/weather"}


def test_importing_twice_changes_nothing_the_planner_sees() -> None:
    registry = _imported()
    before = load_from_registry(registry, now=NOW)
    rows = (len(registry.sightings()), len(registry.fields()))

    import_files(registry, details=_records(), ledgers=[LEDGER], now=NOW)

    assert load_from_registry(registry, now=NOW) == before
    assert (len(registry.sightings()), len(registry.fields())) == rows


# --- readers --------------------------------------------------------------------------


def test_orthogonal_reader_keeps_routes_the_planner_refuses() -> None:
    records = [
        {
            "api": "svc",
            "details": {
                "endpoint": {
                    "method": "DELETE",
                    "path": "/things/{id}",
                    "price": 0.01,
                    "responseParams": [
                        {"name": "data.0.name"},
                        {"name": "credits_consumed"},
                        {"name": "bad path with spaces"},
                    ],
                }
            },
        },
        {"api": "svc", "details": {}},
    ]
    report = ReadReport()

    pairs = orthogonal_sightings(records, now=NOW, found_url="file:x", report=report)

    assert report.read == 1 and report.skipped == {"malformed": 1}
    sighting, draft = pairs[0]
    assert sighting.method == "DELETE" and sighting.claimed.price_cents == 1
    # Lists folded to [], Orthogonal's envelope and non-grammar names dropped.
    assert draft.claimed_paths == ("data[].name",)


def test_paysh_reader_names_verified_routes_and_roots_the_rest() -> None:
    pairs = paysh_sightings(paysh_entries_from_text(PAYSH_TEXT), now=NOW)
    paths = {s.api: (s.method, s.path) for s, _d in pairs}

    assert paths["paysponge/coingecko"] == ("GET", "/api/v3/x402/onchain/search/pools")
    assert paths["acme/weather"] == ("GET", "/")
    assert "broken" not in paths, "a non-http service_url is dropped by the parser"


def test_the_registry_holds_no_subject_or_value_from_a_ledger() -> None:
    rows = read_jsonl(LEDGER)
    subjects = {r["subject"] for r in rows if isinstance(r.get("subject"), str)}
    rows.append(
        {
            "catalog": "orthogonal",
            "api": "akta-pro",
            "path": "/v1/company/enrichment/company_hierarchy/{company}",
            "subject": "Secret Customer Co",
            "customer": "acct-123",
            "output_fields": ["data.uuid"],
            "ts": NOW,
        }
    )
    subjects |= {"Secret Customer Co", "acct-123"}
    registry = _imported()
    from gecko.servicemap.importer import import_observed

    import_observed(registry, observed_fields(rows), now=NOW)

    dumped = json.dumps(
        [asdict(r) for r in (*registry.fields(), *registry.sightings())]
    )
    assert subjects and not any(s in dumped for s in subjects)


def test_an_observed_listing_no_catalog_names_is_counted_not_invented() -> None:
    registry = MemoryRegistry()
    counts = import_files(
        registry,
        details=[],
        ledgers=[LEDGER],
        now=NOW,
    )
    assert counts.observed_unmatched > 0
    assert registry.fields() == [] and registry.operations() == []


def test_observed_fields_take_their_times_from_the_ledger() -> None:
    rows = [
        {"catalog": "orthogonal", "api": "a", "path": "/p", "output_fields": ["x"],
         "ts": "2026-10-01T00:00:00+00:00"},
        {"catalog": "orthogonal", "api": "a", "path": "/p", "output_fields": ["y"],
         "ts": "2026-10-03T00:00:00+00:00"},
    ]  # fmt: skip
    (obs,) = observed_fields(rows)
    assert obs.paths == {"x", "y"}
    assert (obs.first_seen, obs.last_seen) == (
        "2026-10-01T00:00:00+00:00",
        "2026-10-03T00:00:00+00:00",
    )


# --- snapshot: health, the flag, the kill switch ---------------------------------------


def test_a_quarantined_operation_leaves_the_pool_until_it_expires() -> None:
    registry = _imported()
    full = load_from_registry(registry, now=NOW)
    victim = full.candidates.candidates[0]
    (sighting,) = [
        s
        for s in registry.sightings()
        if (s.api, s.method, s.path) == (victim.api, victim.method, victim.path)
    ]
    registry.put_health(
        Health(sighting.op_id, "quarantined", 1, NOW, None, 402, 0,
               quarantined_until="2026-10-11T00:00:00+00:00")
    )  # fmt: skip

    during = load_from_registry(registry, now=NOW)
    after = load_from_registry(registry, now="2026-10-12T00:00:00+00:00")

    assert victim.candidate_id not in {
        c.candidate_id for c in during.candidates.candidates
    }
    assert after.candidates == full.candidates


@pytest.mark.parametrize(
    ("value", "source"),
    [
        (None, "files"),
        ("files", "files"),
        ("registry", "registry"),
        (" Registry ", "registry"),
    ],
)
def test_the_flag_picks_the_loader(value: str | None, source: str) -> None:
    env = {} if value is None else {"GECKO_ANYQUERY_CATALOG": value}
    assert catalog_source(env) == source


def test_an_unknown_flag_value_is_refused_not_read_as_files() -> None:
    with pytest.raises(CatalogSourceError):
        catalog_source({"GECKO_ANYQUERY_CATALOG": "mongo"})


def test_the_kill_switch_stops_an_import_before_any_write() -> None:
    registry = MemoryRegistry(kill_switch=True)
    with pytest.raises(RegistryKilledError):
        import_files(registry, details=_records(), ledgers=[LEDGER], now=NOW)
    registry.kill_switch = False
    assert registry.sightings() == [] and registry.services() == []


def test_a_claimed_field_from_another_catalog_does_not_become_an_orthogonal_path() -> (
    None
):
    registry = _imported()
    before = load_from_registry(registry, now=NOW)
    some = next(f for f in registry.fields() if f.evidence == "CLAIMED")
    registry.put_field(replace(some, path="from.paysh.only", claimed_by=("paysh",)))

    assert load_from_registry(registry, now=NOW).candidates == before.candidates

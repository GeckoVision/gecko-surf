"""Source map, drift, repair, changes and the read tools — offline. No wire, no key, no
money, no DNS.

Every test runs the real job against injected fakes: the made-up Orthogonal
(``RecordedSupplierOrthogonal``, with ``changed_shape`` to mutate a reply), the made-up
GLEIF register, an in-memory field ledger shared across runs, and a temp lists folder.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from gecko.caller import PreparedRequest
from gecko.netguard import UnsafeUrlError
from gecko.orthogonal.job import deps_for
from gecko.orthogonal.spec import DETAILS_OP, RUN_OP, SUPPLIER_TARGETS, check_allowed
from gecko.orthogonal.supplier import plan_watch, read_runs, run_watch
from gecko.orthogonal.supplier.deps import supplier_deps_for
from gecko.orthogonal.supplier.demo import run_self_repair_demo
from gecko.orthogonal.supplier.drift import baseline_from, check
from gecko.orthogonal.supplier.fixture import (
    SAMPLE_SUPPLIERS_CSV,
    RecordedSupplierOrthogonal,
)
from gecko.orthogonal.supplier.gleif import (
    DEFAULT_GLEIF_BASE_URL,
    GleifConfigError,
    check_base_url,
    gleif_live_fetch,
    lookup_parent,
    made_up_gleif_fetch,
)
from gecko.orthogonal.supplier.history import (
    RunNotFoundError,
    changes_since,
    priors,
)
from gecko.orthogonal.supplier.readers import data_of, field_names
from gecko.orthogonal.supplier.repair import same_value
from gecko.orthogonal.supplier.sources import (
    EQUIVALENCES,
    SOURCE_MAP,
    UnknownFieldError,
)
from gecko.orthogonal.supplier.store import MemoryFieldLedger
from gecko.orthogonal.surface import CommenterListSurface

T0 = dt.datetime(2026, 10, 8, 12, tzinfo=dt.timezone.utc).timestamp()
TODAY = dt.date(2026, 10, 8)
SHEET = "suppliers.csv"
CIRCUITS, INVENTED, ROGUE = (
    "Made Up Circuits Inc.",
    "Invented Assembly Ltd",
    "Made Up Rogue Trading LLC",
)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    lists = tmp_path / "gecko-lists"
    lists.mkdir()
    (lists / SHEET).write_text(SAMPLE_SUPPLIERS_CSV, encoding="utf-8")
    return lists


def _deps(
    folder: Path,
    ledger: MemoryFieldLedger,
    *,
    hours: int = 0,
    transport: Any = None,
    **kw: Any,
) -> Any:
    at = T0 + 3600 * hours
    return supplier_deps_for(
        "recorded",
        folder=folder,
        field_ledger=ledger,
        transport=transport or RecordedSupplierOrthogonal(today=TODAY),
        clock=lambda: at,
        **kw,
    )


def _run(folder: Path, ledger: MemoryFieldLedger, hours: int = 0, **kw: Any) -> Any:
    return run_watch(_deps(folder, ledger, hours=hours, **kw), SHEET, confirm=True)


def _changed(*apis: str) -> RecordedSupplierOrthogonal:
    return RecordedSupplierOrthogonal(today=TODAY, changed_shape=frozenset(apis))


def _row(result: Any, supplier: str, field: str) -> dict[str, Any]:
    return next(
        r for r in result.rows if r["supplier"] == supplier and r["field"] == field
    )


def _stored(folder: Path, run_id: str, supplier: str, field: str) -> dict[str, Any]:
    return next(
        r
        for r in read_runs(folder)
        if (r["run_id"], r["supplier"], r["field"]) == (run_id, supplier, field)
    )


# --- the source map is data ---------------------------------------------------------


def test_source_map_ranks_and_prices_each_field() -> None:
    entity = SOURCE_MAP["entity"].sources
    assert [s.provider for s in entity] == ["crustdata", "predictleads", "coresignal"]
    assert [s.price_cents for s in entity] == [0, 4, 5]
    parent = SOURCE_MAP["parent_company"].sources
    assert [(s.provider, s.kind, s.price_cents) for s in parent] == [
        ("akta-pro", "orthogonal", 3),
        ("gleif", "public", 0),
    ]
    for fs in SOURCE_MAP.values():
        for source in fs.sources:
            assert source.reader is not None or source.kind == "public"
            if source.target is not None:
                assert source.target in SUPPLIER_TARGETS
                check_allowed(
                    DETAILS_OP, {"api": source.target.api, "path": source.target.path}
                )


def test_equivalences_say_how_they_are_known() -> None:
    confirmed = [e for e in EQUIVALENCES if e.level == "confirmed"]
    assert {e.meaning for e in confirmed} == {"company name", "company website domain"}
    domain = next(e for e in confirmed if e.meaning == "company website domain")
    assert ("predictleads company name", "data[].attributes.domain") in domain.paths
    # Never called live: coresignal and GLEIF stay "declared" until a run sees them.
    for equivalence in EQUIVALENCES:
        named = {source for source, _path in equivalence.paths}
        if named & {"coresignal company_clean/enrich", "GLEIF direct parent (public)"}:
            assert equivalence.level == "declared"


def test_each_paid_source_path_is_one_the_ledger_records() -> None:
    """A drift check can only find a path the field ledger writes down."""
    fake = RecordedSupplierOrthogonal(today=TODAY)
    bodies = {
        "crustdata": {
            "domains": ["made-up-circuits.example"],
            "fields": ["basic_info"],
        },
    }
    for fs in SOURCE_MAP.values():
        for source in fs.sources:
            if source.target is None:
                continue
            path = source.target.path.replace(
                "{id_or_domain}", "made-up-circuits.example"
            )
            path = path.replace("{company}", "invented-assembly.example")
            body: dict[str, Any] = {"api": source.target.api, "path": path}
            if source.target.api in bodies:
                body["body"] = bodies[source.target.api]
            if source.target.api == "coresignal":
                body["query"] = {"website": "made-up-circuits.example"}
            status, reply = fake(
                PreparedRequest("POST", "https://api.orthogonal.com/v1/run", {}, body)
            )
            assert status == 200, source.name
            assert source.field_path in field_names(data_of(reply)), source.name
    found = lookup_parent(
        "Invented Assembly", base_url=DEFAULT_GLEIF_BASE_URL, fetch=made_up_gleif_fetch
    )
    assert (
        SOURCE_MAP["parent_company"].sources[1].field_path
        in found.steps[0].output_fields
    )


# --- drift ---------------------------------------------------------------------------

_IDENTIFY_FIELDS = [  # as the live field ledger recorded them on 2026-10-08
    "[].matched_on",
    "[].matches",
    "[].matches[].company_data",
    "[].matches[].company_data.basic_info",
]
_PATH = "[].matches[].company_data.basic_info"


def _ledger_row(run_id: str, **kw: Any) -> dict[str, Any]:
    row = {
        "run_id": run_id,
        "mode": "live",
        "kind": "call",
        "api": "crustdata",
        "path": "/company/identify",
        "page": 1,
        "subject": "s1",
        "status": "ok",
        "http_status": 200,
        "output_fields": _IDENTIFY_FIELDS,
    }
    return {**row, **kw}


def test_missing_field_path_is_drift_against_the_last_run() -> None:
    later = "20261009-090000"
    base = baseline_from(
        [_ledger_row("20261008-082501")], mode="live", before_run=later
    )
    now = _ledger_row(later, output_fields=["results", "results[].company"])
    drift = check(now, _PATH, base)
    assert drift is not None and drift.short == "field path missing"
    assert "present in run 20261008-082501" in drift.reason
    assert check(_ledger_row(later), _PATH, base) is None


def test_a_first_run_can_fail_but_cannot_drift() -> None:
    empty = baseline_from([], mode="live", before_run="3")
    assert check(_ledger_row("3", output_fields=[]), _PATH, empty) is None
    assert (
        check(_ledger_row("3", status="upstream_error", http_status=404), _PATH, empty)
        is None
    )


def test_4xx_or_empty_where_it_used_to_answer_is_drift() -> None:
    base = baseline_from([_ledger_row("1")], mode="live", before_run="2")
    gone = check(
        _ledger_row("2", status="upstream_error", http_status=404), _PATH, base
    )
    assert (
        gone is not None
        and gone.reason == "answered HTTP 404 where it answered in run 1"
    )
    empty = check(_ledger_row("2", output_fields=[]), _PATH, base)
    assert empty is not None and empty.kind == "empty"
    # Key, credit and rate problems are not a changed contract.
    for status in (401, 402, 403, 429):
        row = _ledger_row("2", status="key_rejected", http_status=status)
        assert check(row, _PATH, base) is None
    # Another supplier's history says nothing about this one.
    other = _ledger_row("2", subject="s2", status="upstream_error", http_status=404)
    assert check(other, _PATH, base) is None


def test_baseline_ignores_other_modes_and_later_runs() -> None:
    rows = [
        _ledger_row("1", mode="recorded", output_fields=["x"]),
        _ledger_row("5", output_fields=["y"]),
    ]
    assert baseline_from(rows, mode="live", before_run="3").prints == {}


# --- repair, end to end --------------------------------------------------------------


def test_changed_shape_fails_over_and_publishes_when_it_agrees(folder: Path) -> None:
    ledger = MemoryFieldLedger()
    first = _run(folder, ledger)
    second = _run(folder, ledger, hours=1, transport=_changed("crustdata"))
    row = _row(second, CIRCUITS, "entity")
    assert (row["status"], row["failover"]) == ("repaired", True)
    assert row["filled_by"] == "predictleads company name"
    assert row["second_source"] == f"last snapshot {first.run_id}"
    assert (
        f"{CIRCUITS}: entity: crustdata drifted (field path missing) → filled from "
        "predictleads, agrees with last snapshot"
    ) in second.repairs
    field_rows = [
        r
        for r in ledger.rows()
        if r["kind"] == "field"
        and r["run_id"] == second.run_id
        and r["field"] == "entity"
    ]
    assert {r["outcome"] for r in field_rows} == {"repaired", "stale"}
    assert all(r["failover"] and "field path missing" in r["drift"] for r in field_rows)


def test_a_drifted_reply_is_never_read(folder: Path) -> None:
    """The reshaped reply still has a ``name`` deeper down — the wrong one."""
    ledger = MemoryFieldLedger()
    _run(folder, ledger)
    second = _run(folder, ledger, hours=1, transport=_changed("crustdata"))
    table = Path(second.table_path).read_text(encoding="utf-8")
    assert "Unrelated Holdings" not in table
    assert "Unrelated Holdings" not in json.dumps(second.to_dict())


def test_disagreeing_failover_keeps_the_last_good_value_marked_stale(
    folder: Path,
) -> None:
    ledger = MemoryFieldLedger()
    first = _run(folder, ledger)
    second = _run(folder, ledger, hours=1, transport=_changed("crustdata"))
    third = _run(folder, ledger, hours=2, transport=_changed("crustdata"))
    stored = _stored(folder, second.run_id, INVENTED, "entity")
    assert stored["status"] == "stale" and stored["stale_since"] == second.run_id
    assert stored["value"] == "Invented Assembly"  # run 1's value, kept
    old = _stored(folder, first.run_id, INVENTED, "entity")["value_summary"]
    assert stored["value_summary"] == f"{old} (stale since {second.run_id})"
    assert any("predictleads differs from last snapshot" in r for r in second.repairs)
    # Still stale a run later: "since" is the run it went stale in, not this one.
    later = _stored(folder, third.run_id, INVENTED, "entity")
    assert later["stale_since"] == second.run_id
    # Run 2's drifted shape did not become the new normal: run 3 still sees drift.
    assert any(CIRCUITS in r and "field path missing" in r for r in third.repairs)
    assert "Unrelated Holdings" not in Path(third.table_path).read_text(
        encoding="utf-8"
    )
    assert _stored(folder, first.run_id, INVENTED, "entity")["status"] == "fresh"


def test_third_source_is_one_paid_call_inside_the_allowlist(folder: Path) -> None:
    ledger = MemoryFieldLedger()
    _run(folder, ledger)
    fake = _changed("crustdata")
    second = _run(folder, ledger, hours=1, transport=fake)
    row = _row(second, ROGUE, "entity")
    assert (row["status"], row["filled_by"]) == (
        "repaired",
        "coresignal company_clean/enrich",
    )
    core = [
        c for c in second.receipt["calls"] if c["endpoint"].startswith("coresignal")
    ]
    assert [c["charged_cents"] for c in core] == [5]
    for req in fake.requests:
        if req.url.endswith("/v1/run"):
            check_allowed(RUN_OP, req.json_body)
    assert second.receipt["failovers"] == 4  # 3 entities + the rogue's parent


def test_a_failover_that_would_cross_the_cap_is_skipped_not_fatal(folder: Path) -> None:
    ledger = MemoryFieldLedger()
    _run(folder, ledger, per_run_cap=53)  # 52 counted
    second = _run(
        folder, ledger, hours=1, transport=_changed("crustdata"), per_run_cap=53
    )
    row = _row(second, ROGUE, "entity")
    assert row["status"] == "stale"
    assert second.receipt["stopped"] is None and second.receipt["charged_cents"] <= 53
    assert not any(
        c["endpoint"].startswith("coresignal") for c in second.receipt["calls"]
    )
    assert any("coresignal not called" in line for line in second.repairs)


def test_forced_failover_does_not_call_the_first_source(folder: Path) -> None:
    ledger = MemoryFieldLedger()
    fake = RecordedSupplierOrthogonal(today=TODAY)
    result = _run(folder, ledger, transport=fake, force_failover=["entity"])
    apis = [r.json_body["api"] for r in fake.requests if r.url.endswith("/v1/run")]
    assert "crustdata" not in apis
    row = _row(result, CIRCUITS, "entity")
    assert (row["status"], row["filled_by"]) == (
        "repaired",
        "predictleads company name",
    )
    assert any(
        "crustdata forced (forced off for this run)" in r for r in result.repairs
    )


def test_unknown_forced_field_is_refused_before_anything_runs(folder: Path) -> None:
    with pytest.raises(UnknownFieldError):
        _deps(folder, MemoryFieldLedger(), force_failover=["news"])


def _akta_404_for_invented(fake: RecordedSupplierOrthogonal) -> Any:
    def transport(req: PreparedRequest) -> tuple[int, Any]:
        body = req.json_body if isinstance(req.json_body, dict) else {}
        if "company_hierarchy/invented-assembly.example" in str(body.get("path")):
            fake.requests.append(req)
            return 404, {"error": "made-up"}
        return fake(req)

    return transport


def test_parent_fails_over_to_the_public_register(folder: Path) -> None:
    ledger = MemoryFieldLedger()
    first = _run(folder, ledger)  # akta names fictional-holdings.example
    fake = RecordedSupplierOrthogonal(today=TODAY)
    second = _run(folder, ledger, hours=1, transport=_akta_404_for_invented(fake))
    row = _row(second, INVENTED, "parent_company")
    assert (row["status"], row["filled_by"]) == (
        "repaired",
        "GLEIF direct parent (public)",
    )
    assert row["second_source"] == f"last snapshot {first.run_id}"
    assert (
        f"{INVENTED}: parent_company: akta-pro drifted (HTTP 404 where it answered) "
        "→ filled from gleif, agrees with last snapshot"
    ) in second.repairs
    public = [r for r in ledger.rows() if r.get("catalog") == "public"]
    assert {r["path"] for r in public} >= {
        "/lei-records",
        "/lei-records/{lei}/direct-parent",
    }
    assert all(r["charged_cents"] == 0 for r in public)


def test_same_value_rules() -> None:
    assert same_value("entity", "Made Up Circuits Inc.", "MADE UP CIRCUITS")
    assert not same_value("entity", "Invented Assembly", "Imaginary Assembly Group")
    assert same_value(
        "parent_company", "fictional-holdings.example", "Fictional Holdings Ltd"
    )
    assert same_value("parent_company", "(no parent named)", "(no parent named)")
    assert not same_value("parent_company", "(no parent named)", "Fictional Holdings")


# --- GLEIF: config, same-entity check, SSRF guard ------------------------------------


def test_gleif_refuses_a_record_for_a_different_entity() -> None:
    def near_miss(url: str) -> tuple[int, str]:
        record = {
            "id": "MADEUP00000000000003",
            "attributes": {
                "entity": {"legalName": {"name": "Invented Assembly Group"}}
            },
        }
        return 200, json.dumps({"data": [record]})

    found = lookup_parent(
        "Invented Assembly", base_url=DEFAULT_GLEIF_BASE_URL, fetch=near_miss
    )
    assert found.reading is None and "different entity" in found.reason
    assert len(found.steps) == 1  # the parent was never asked for


def test_gleif_base_url_is_config_and_https_only() -> None:
    assert (
        check_base_url("https://gleif.example/api/v1/")
        == "https://gleif.example/api/v1"
    )
    for bad in (
        "http://api.gleif.org/api/v1",
        "https://api.gleif.org/x?y=1",
        "file:///x",
    ):
        with pytest.raises(GleifConfigError):
            check_base_url(bad)


def test_gleif_live_fetch_goes_through_the_ssrf_guard() -> None:
    """Never fetched from here: a private resolver answer stops it before any socket."""
    with pytest.raises(UnsafeUrlError):
        gleif_live_fetch(
            f"{DEFAULT_GLEIF_BASE_URL}/lei-records", resolver=lambda host: ["10.0.0.7"]
        )
    with pytest.raises(UnsafeUrlError):
        gleif_live_fetch(
            "http://api.gleif.org/api/v1", resolver=lambda host: ["1.1.1.1"]
        )


def test_live_deps_wire_gleif_live_and_recorded_stays_made_up(
    folder: Path, tmp_path: Path
) -> None:
    live = supplier_deps_for("live", folder=folder, state_dir=tmp_path / "state")
    assert live.gleif_fetch is not made_up_gleif_fetch
    assert live.gleif_base_url == DEFAULT_GLEIF_BASE_URL
    assert (
        supplier_deps_for("recorded", folder=folder).gleif_fetch is made_up_gleif_fetch
    )
    with pytest.raises(GleifConfigError):
        supplier_deps_for("recorded", folder=folder, gleif_base_url="http://x.example")


# --- the plan shows the worst case ----------------------------------------------------


def test_plan_prices_the_failover_as_worst_case(folder: Path) -> None:
    plan = plan_watch(_deps(folder, MemoryFieldLedger()), SHEET)
    assert plan.total_cents == 48 and plan.worst_case_cents == 48 + 3 * (4 + 5)
    entity = next(f for f in plan.failover_sources if f["field"] == "entity")
    assert [s["cents"] for s in entity["sources"]] == [0, 4, 5]
    assert "worst case $0.75" in plan.message and "GLEIF $0.00" in plan.message


# --- changes and history ---------------------------------------------------------------


def _stored_row(run_id: str, supplier: str, field: str, **kw: Any) -> dict[str, Any]:
    row = {
        "run_id": run_id,
        "mode": "recorded",
        "supplier": supplier,
        "field": field,
        "value": "A",
        "value_summary": "A",
        "source": "s1",
        "filled_by": "s1",
        "status": "fresh",
        "agreement": "only one",
        "model_summary": "one source named it",
    }
    return {**row, **kw}


def test_changes_since_reports_added_changed_removed_and_source() -> None:
    rows = [
        _stored_row("1", "Co", "entity"),
        _stored_row("1", "Co", "parent_company"),
        _stored_row("1", "Gone", "entity"),
        _stored_row("2", "Co", "entity", filled_by="s2", status="repaired"),
        _stored_row("2", "Co", "parent_company"),
        _stored_row("2", "New", "entity"),
        _stored_row("3", "Co", "entity", mode="live"),  # other mode: ignored
    ]
    diff = changes_since(rows, "1")
    assert diff.against_run == "2"
    got = {(c.supplier, c.field): c for c in diff.changes}
    assert set(got) == {("Co", "entity"), ("Gone", "entity"), ("New", "entity")}
    assert got[("Co", "entity")].what == ("source", "status")
    assert got[("Co", "entity")].source_changed
    assert got[("Gone", "entity")].change == "removed"
    assert got[("New", "entity")].change == "added"
    assert "value_summary" not in json.dumps(diff.to_dict())
    with pytest.raises(RunNotFoundError):
        changes_since(rows, "nope")
    assert changes_since(rows, "2").against_run is None


def test_changes_since_over_two_stored_parquet_runs(folder: Path) -> None:
    ledger = MemoryFieldLedger()
    first = _run(folder, ledger)
    second = _run(folder, ledger, hours=1, transport=_changed("crustdata"))
    diff = changes_since(read_runs(folder), first.run_id)
    assert diff.against_run == second.run_id
    rogue = next(c for c in diff.changes if (c.supplier, c.field) == (ROGUE, "entity"))
    assert rogue.source_before == "crustdata /company/identify"
    assert rogue.source_after == "coresignal company_clean/enrich"


def test_runs_before_the_value_column_still_give_an_entity_snapshot() -> None:
    legacy = [  # the shape of the two live runs of 2026-10-08
        {"run_id": "1", "mode": "live", "supplier": "Jabil", "field": "entity",
         "value_summary": "Jabil Inc.", "agreement": "agree"},
        {"run_id": "1", "mode": "live", "supplier": "Jabil", "field": "parent_company",
         "value_summary": "x; 3 subsidiaries", "agreement": "only one"},
    ]  # fmt: skip
    got = priors(legacy, mode="live")
    snap = got[("Jabil", "entity")].last_good
    assert snap is not None and snap.value == "Jabil Inc."
    assert ("Jabil", "parent_company") not in got


# --- the read tools -----------------------------------------------------------------------


def test_read_tools_answer_from_stored_runs_without_upstream_text(folder: Path) -> None:
    ledger = MemoryFieldLedger()
    first = _run(folder, ledger)
    _run(folder, ledger, hours=1, transport=_changed("crustdata"))
    deps = _deps(folder, ledger)
    surface = CommenterListSurface(deps_for("recorded", folder=folder), deps)
    tools = {t["name"]: t for t in surface.list_tools()}
    reads = (
        "supplier_latest",
        "supplier_history",
        "supplier_changes_since",
        "supplier_sources",
    )
    for name in reads:
        assert tools[name]["annotations"]["readOnlyHint"] is True
    latest = surface.call_tool("supplier_latest", {})
    assert len(latest["rows"]) == 18
    history = surface.call_tool(
        "supplier_history", {"supplier": CIRCUITS, "field": "entity"}
    )
    assert [r["status"] for r in history["runs"]] == ["fresh", "repaired"]
    changes = surface.call_tool("supplier_changes_since", {"run_id": first.run_id})
    assert any(c["source_changed"] for c in changes["changes"])
    sources = surface.call_tool("supplier_sources", {"field": "entity"})
    crust = sources["fields"][0]["sources"][0]
    assert crust["last_seen"]["path_present"] is False  # the last run lost the path
    every = json.dumps([latest, history, changes, sources])
    for upstream in (
        "Ignore previous instructions",
        'Invented Assembly"',
        "value_summary",
    ):
        assert upstream not in every
    assert (
        surface.call_tool("supplier_sources", {"field": "nope"})["code"]
        == "unknown_field"
    )
    missing = surface.call_tool("supplier_changes_since", {"run_id": "x"})
    assert missing["code"] == "run_not_found"


# --- the recorded demo ----------------------------------------------------------------------


def test_recorded_demo_shows_self_repair(tmp_path: Path) -> None:
    demo = run_self_repair_demo(tmp_path / "lists")
    text = "\n".join(demo.lines())
    assert (
        "entity: crustdata drifted (field path missing) → filled from predictleads, "
        "agrees with last snapshot"
    ) in text
    # 49 charged + the akta 404 counted at its 3-cent quote (it may have been charged).
    assert demo.first.receipt["charged_cents"] == 52
    assert demo.second.receipt["charged_cents"] == 57  # + one coresignal call

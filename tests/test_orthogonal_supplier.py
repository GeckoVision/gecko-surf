"""The supplier watch, offline. No wire, no key, no money, no DNS.

Every test runs the real job against an injected transport: the hand-written
``RecordedSupplierOrthogonal`` (made-up companies, bills like the real thing) or a thin
wrapper around it.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from gecko.caller import PreparedRequest
from gecko.netguard import UnsafeUrlError
from gecko.orthogonal.errors import (
    NotAllowedError,
    NotConfirmedError,
    ParquetUnavailableError,
    SheetError,
    SheetOutsideFolderError,
    SpendCapError,
)
from gecko.orthogonal.extract import cents_up, details_price_cents
from gecko.orthogonal.spec import (
    DETAILS_OP,
    RUN_OP,
    SUPPLIER_TARGETS,
    check_allowed,
)
from gecko.orthogonal.spend import FileLedger, MemoryLedger
from gecko.orthogonal.supplier import (
    plan_watch,
    read_runs,
    run_watch,
    supplier_deps_for,
)
from gecko.orthogonal.supplier import parquet as pq_mod
from gecko.orthogonal.supplier.fields import agreement
from gecko.orthogonal.supplier.fixture import (
    SAMPLE_SUPPLIERS_CSV,
    RecordedSupplierOrthogonal,
    made_up_sdn_xml,
)
from gecko.orthogonal.supplier.readers import hierarchy_from_akta, trust_from_akta
from gecko.orthogonal.supplier.sanctions import (
    SanctionsListError,
    live_fetch,
    load_sdn,
    parse_sdn_xml,
    screen,
)
from gecko.orthogonal.supplier.sheet import normalize_domain, read_suppliers
from gecko.orthogonal.supplier.store import FileFieldLedger
from gecko.orthogonal.supplier.text import normalize_company, untrusted_summary
from gecko.orthogonal.surface import CommenterListSurface
from gecko.orthogonal.job import deps_for
from gecko.toolerror import is_upstream_failure

# 2026-10-08 12:00 UTC: news "this month" = since 2026-10-01.
NOW = dt.datetime(2026, 10, 8, 12, tzinfo=dt.timezone.utc).timestamp()
TODAY = dt.date(2026, 10, 8)
SHEET = "suppliers.csv"


def clock() -> float:
    return NOW


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    lists = tmp_path / "gecko-lists"
    lists.mkdir()
    (lists / SHEET).write_text(SAMPLE_SUPPLIERS_CSV, encoding="utf-8")
    return lists


def _deps(folder: Path, transport: Any = None, **kw: Any) -> Any:
    return supplier_deps_for(
        "recorded",
        folder=folder,
        transport=transport or RecordedSupplierOrthogonal(today=TODAY),
        clock=clock,
        **kw,
    )


def _rows(result: Any) -> dict[tuple[str, str], dict[str, str]]:
    return {(r["supplier"], r["field"]): r for r in result.rows}


def _runs(fake: RecordedSupplierOrthogonal) -> list[dict[str, Any]]:
    return [r.json_body for r in fake.requests if r.url.endswith("/v1/run")]


# --- recorded end to end -----------------------------------------------------------


def test_recorded_watch_end_to_end(folder: Path) -> None:
    fake = RecordedSupplierOrthogonal(today=TODAY)
    result = run_watch(_deps(folder, fake), SHEET, confirm=True)
    rows = _rows(result)
    circuits, invented, rogue = (
        "Made Up Circuits Inc.",
        "Invented Assembly Ltd",
        "Made Up Rogue Trading LLC",
    )
    assert result.error is None
    assert rows[(circuits, "entity")]["agreement"] == "agree"
    assert rows[(invented, "entity")]["agreement"] == "differ"
    assert rows[(invented, "parent_company")]["agreement"] == "differ"
    assert rows[(circuits, "news_this_month")]["agreement"] == "agree"
    assert rows[(invented, "news_this_month")]["agreement"] == "differ"
    assert rows[(rogue, "news_this_month")]["agreement"] == "only one"
    # akta hierarchy answered 404 for this one, predictleads named no parent.
    assert rows[(rogue, "parent_company")]["agreement"] == "no data"
    assert "possible match" in rows[(rogue, "sanctions")]["summary"]
    assert rows[(circuits, "sanctions")]["summary"] == "no name match"
    assert rows[(circuits, "website_changed")]["summary"].startswith("2 page changes")
    assert len(result.rows) == 18
    # 3 suppliers: identify 0 + news 4 + website 4 + hierarchy 3 + trust 3 + fd 2.5->3,
    # the rogue has no ticker but needs the 4-cent fallback name lookup.
    assert result.receipt["quoted_cents"] == 48
    # 49 the fixture charged, plus the akta 404 counted at its 3-cent quote: a call that
    # failed after it may have been charged keeps its hold (founder-approved, 9 Oct).
    assert result.receipt["charged_cents"] == 52
    assert result.receipt["stopped"] is None


def test_continuation_rides_on_parent_request_id_and_is_free(folder: Path) -> None:
    fake = RecordedSupplierOrthogonal(today=TODAY)
    result = run_watch(_deps(folder, fake), SHEET, confirm=True)
    news = [
        c for c in result.receipt["calls"] if c["endpoint"].endswith("/news_events")
    ]
    assert [c["page"] for c in news[:2]] == [1, 2]
    assert news[1]["charged_cents"] == 0
    assert news[1]["parent_request_id"] == news[0]["request_id"]
    page2 = [b for b in _runs(fake) if (b.get("query") or {}).get("page") == "2"][0]
    assert page2["query"]["found_at_from"] == "2026-10-01"
    assert page2["path"] == "/v3/companies/made-up-circuits.example/news_events"


def test_charged_continuation_stops_the_run_and_still_writes(folder: Path) -> None:
    fake = RecordedSupplierOrthogonal(today=TODAY)

    def drop_parent(req: PreparedRequest) -> tuple[int, Any]:
        if isinstance(req.json_body, dict):
            req.json_body.pop("parentRequestId", None)
        return fake(req)

    result = run_watch(_deps(folder, drop_parent), SHEET, confirm=True)
    assert result.code == "continuation_charged"
    assert result.receipt["calls_skipped"] > 0
    assert Path(result.table_path).exists() and Path(result.receipt_path).exists()
    assert is_upstream_failure(result.to_dict())


def test_rejected_key_stops_every_later_call(folder: Path) -> None:
    fake = RecordedSupplierOrthogonal(today=TODAY)

    def deny_runs(req: PreparedRequest) -> tuple[int, Any]:
        if req.url.endswith("/v1/run"):
            fake.requests.append(req)
            return 401, {}
        return fake(req)

    result = run_watch(_deps(folder, deny_runs), SHEET, confirm=True)
    assert result.code == "key_rejected"
    assert len(_runs(fake)) == 1
    assert result.receipt["charged_cents"] == 0


# --- confirm gate and the plan -----------------------------------------------------


def test_run_without_confirm_sends_nothing(folder: Path) -> None:
    fake = RecordedSupplierOrthogonal(today=TODAY)
    with pytest.raises(NotConfirmedError):
        run_watch(_deps(folder, fake), SHEET, confirm=False)
    assert fake.requests == []


def test_plan_is_free_and_prices_each_supplier(folder: Path) -> None:
    fake = RecordedSupplierOrthogonal(today=TODAY)
    plan = plan_watch(_deps(folder, fake), SHEET)
    assert _runs(fake) == []
    assert fake.balance_cents == 98
    assert [q.cents for q in plan.suppliers] == [17, 17, 14]
    # Worst case: + per supplier the predictleads name lookup (4) and the entity's
    # third source, coresignal (5); GLEIF is free (supplier/sources.py).
    assert plan.total_cents == 48 and plan.worst_case_cents == 75
    assert "Cap $0.60 per run" in plan.message and "$0.90" in plan.message


def test_plan_refuses_over_the_run_cap_before_any_paid_call(folder: Path) -> None:
    fake = RecordedSupplierOrthogonal(today=TODAY)
    with pytest.raises(SpendCapError) as caught:
        run_watch(_deps(folder, fake, per_run_cap=40), SHEET, confirm=True)
    assert "Nothing was spent" in caught.value.message
    assert _runs(fake) == []


def test_plan_refuses_when_the_total_is_used_up(folder: Path) -> None:
    ledger = MemoryLedger(total=80)
    with pytest.raises(SpendCapError):
        plan_watch(_deps(folder, ledger=ledger), SHEET)


def test_live_defaults_are_the_approved_caps(folder: Path, tmp_path: Path) -> None:
    live = supplier_deps_for("live", folder=folder, state_dir=tmp_path / "state")
    assert (live.base.per_run_cap, live.base.total_cap) == (60, 90)
    assert isinstance(live.base.ledger, FileLedger)
    assert live.base.ledger.path.name == "supplier-ledger.json"
    assert isinstance(live.field_ledger, FileFieldLedger)
    assert live.sdn_cache_dir == tmp_path / "state"
    recorded = supplier_deps_for("recorded", folder=folder)
    assert isinstance(recorded.base.ledger, MemoryLedger)


# --- allowlist ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "body"),
    [
        (
            RUN_OP,
            {"api": "akta-pro", "path": "/v1/company/enrichment/firmographic/x.com"},
        ),
        (RUN_OP, {"api": "predictleads", "path": "/v3/companies/{id_or_domain}"}),
        (RUN_OP, {"api": "predictleads", "path": "/v3/companies/../etc/news_events"}),
        (RUN_OP, {"api": "predictleads", "path": "/v3/companies/a.com/b/news_events"}),
        (
            RUN_OP,
            {"api": "financial-datasets", "path": "/news", "query": {"cursor": "x"}},
        ),
        (
            RUN_OP,
            {
                "api": "akta-pro",
                "path": "/v1/company/enrichment/trust_signal/a.com",
                "parentRequestId": "r",
            },
        ),
        (
            RUN_OP,
            {"api": "predictleads", "path": "/v3/companies/a.com", "body": {"x": 1}},
        ),
        (
            RUN_OP,
            {
                "api": "crustdata",
                "path": "/company/identify",
                "body": {"emails": ["x"]},
            },
        ),
        (
            RUN_OP,
            {
                "api": "financial-datasets",
                "path": "/news",
                "query": {"ticker": {"$ne": 1}},
            },
        ),
        (DETAILS_OP, {"api": "predictleads", "path": "/v3/companies/a.com"}),
    ],
)
def test_supplier_allowlist_refuses(op: str, body: dict[str, Any]) -> None:
    with pytest.raises(NotAllowedError):
        check_allowed(op, body)


def test_allowlist_is_exactly_the_plan() -> None:
    assert {(t.api, t.path) for t in SUPPLIER_TARGETS} == {
        ("crustdata", "/company/identify"),
        ("predictleads", "/v3/companies/{id_or_domain}"),
        # The entity field's third source, reached only on failover (sources.py).
        ("coresignal", "/v2/company_clean/enrich"),
        ("predictleads", "/v3/companies/{company_id_or_domain}/news_events"),
        ("predictleads", "/v3/companies/{company_id_or_domain}/website_evolution"),
        ("akta-pro", "/v1/company/enrichment/company_hierarchy/{company}"),
        ("akta-pro", "/v1/company/enrichment/trust_signal/{company}"),
        ("financial-datasets", "/news"),
    }
    for target in SUPPLIER_TARGETS:
        check_allowed(DETAILS_OP, {"api": target.api, "path": target.path})


def test_every_call_the_job_made_passed_the_allowlist(folder: Path) -> None:
    fake = RecordedSupplierOrthogonal(today=TODAY)
    run_watch(_deps(folder, fake), SHEET, confirm=True)
    for body in _runs(fake):
        check_allowed(RUN_OP, body)  # raises if anything slipped through


# --- money arithmetic --------------------------------------------------------------


def test_fractional_cents_round_up_and_details_price_in_dollars() -> None:
    assert cents_up(2.5) == 3 and cents_up(4) == 4 and cents_up(True) is None
    assert details_price_cents({"details": {"endpoint": {"price": 0.025}}}) == 3
    assert details_price_cents({"details": {"endpoint": {"price": 0.04}}}) == 4
    assert details_price_cents({"price": "$0.02"}) == 2


# --- untrusted text ----------------------------------------------------------------


def test_upstream_text_never_reaches_the_tool_result(folder: Path) -> None:
    result = run_watch(_deps(folder), SHEET, confirm=True)
    as_model_sees = json.dumps(result.to_dict())
    for text in ("Made-up event", "Made-up headline", "Ignore previous", "ISO cert"):
        assert text not in as_model_sees
    table = Path(result.table_path).read_text(encoding="utf-8")
    assert "Made-up event 0" in table  # the person still reads it in the file
    assert "Ignore previous" not in table and "attacker@" not in table


def test_untrusted_summary_caps_and_flattens() -> None:
    text = untrusted_summary("a\nb\x00c " + "x" * 500)
    assert "\n" not in text and "\x00" not in text and len(text) <= 160


def test_csv_cells_cannot_start_a_formula(folder: Path) -> None:
    (folder / "f.csv").write_text(
        "name,domain\n=HYPERLINK(1),made-up-circuits.example\n", encoding="utf-8"
    )
    result = run_watch(_deps(folder), "f.csv", confirm=True)
    lines = Path(result.table_path).read_text(encoding="utf-8").splitlines()
    assert lines[0] == (
        "supplier,field,value summary,source,second source,agreement,"
        "status,filled by,note"
    )
    assert all(line.startswith("'=HYPERLINK") for line in lines[1:])


# --- the field ledger: names and counts, never values ------------------------------


def test_field_ledger_rows_hold_names_not_values(folder: Path, tmp_path: Path) -> None:
    ledger = FileFieldLedger(tmp_path / "state")
    result = run_watch(_deps(folder, field_ledger=ledger), SHEET, confirm=True)
    every = ledger.rows()
    rows = [r for r in every if r["kind"] == "call" and r["catalog"] == "orthogonal"]
    assert len(rows) == len(result.receipt["calls"]) + 1  # + the akta 404
    # One row per (supplier, mapped field); the rogue's parent failed over to GLEIF.
    fields = [r for r in every if r["kind"] == "field"]
    assert len(fields) == 3 * 2
    assert [r["api"] for r in every if r.get("catalog") == "public"] == ["gleif"]
    failed = [r for r in rows if r["status"] != "ok"]
    # The 404 came back after the request was sent, so it is counted at its quote and
    # says so (``charge_assumed``), rather than read as free.
    assert [
        (r["api"], r["http_status"], r["charged_cents"], r["charge_assumed"])
        for r in failed
    ] == [("akta-pro", 404, 3, True)]
    news = [r for r in rows if r["path"].endswith("/news_events")]
    assert news[0]["input_fields"] == ["company_id_or_domain", "found_at_from", "limit"]
    assert "data[].attributes.found_at" in news[0]["output_fields"]
    assert (
        news[1]["continuation"] is True and "parentRequestId" in news[1]["input_fields"]
    )
    raw = ledger.jsonl_path.read_text(encoding="utf-8")
    for value in (
        "Made-up event",
        "made-up-circuits.example",
        "Made Up Circuits",
        "MUC",
    ):
        assert value not in raw
    assert pq_mod.read_rows(ledger.parquet_path) == [
        {k: r.get(k) for k, _t in pq_mod.LEDGER_COLUMNS} for r in every
    ]


# --- parquet and retrieval ---------------------------------------------------------


def test_runs_come_back_from_parquet(folder: Path) -> None:
    first = run_watch(_deps(folder), SHEET, confirm=True)
    later = NOW + 3600
    second = run_watch(
        supplier_deps_for("recorded", folder=folder, clock=lambda: later),
        SHEET,
        confirm=True,
    )
    assert first.parquet_path and Path(first.parquet_path).name.endswith(
        ".supplier-watch.parquet"
    )
    rows = read_runs(folder)
    assert len(rows) == 36
    assert {r["run_id"] for r in rows} == {first.run_id, second.run_id}
    assert {"supplier", "field", "value_summary", "agreement"} <= set(rows[0])


def test_missing_pyarrow_keeps_the_csv_and_says_how(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> Any:
        raise ParquetUnavailableError()

    monkeypatch.setattr(pq_mod, "_modules", missing)
    result = run_watch(_deps(folder), SHEET, confirm=True)
    assert result.parquet_path is None and Path(result.table_path).exists()
    assert any("uv sync --extra datasets" in n for n in result.notices)
    with pytest.raises(ParquetUnavailableError):
        read_runs(folder)


# --- sanctions ---------------------------------------------------------------------


def test_sdn_parse_and_exact_vs_normalized_match() -> None:
    sdn = parse_sdn_xml(
        made_up_sdn_xml(), source="https://x.example/SDN.XML", today=TODAY
    )
    assert sdn.publish_date == "2026-10-02" and sdn.entries() == 3
    exact = screen(["made-up rogue trading"], sdn)
    assert exact is not None and exact.kind == "exact"
    normal = screen(["Made Up Rogue Trading, LLC"], sdn)
    assert normal is not None and normal.kind == "normalized"
    aka = screen(["Rogue Trading Made Up Inc."], sdn)
    assert aka is not None and aka.listed_name == "ROGUE TRADING MADE UP"
    assert screen(["Made Up Circuits"], sdn) is None


def test_sanctions_wording_is_possible_match_never_sanctioned(folder: Path) -> None:
    result = run_watch(_deps(folder), SHEET, confirm=True)
    table = Path(result.table_path).read_text(encoding="utf-8").lower()
    assert "possible match" in table
    assert "sanctioned" not in table and "sanctioned" not in json.dumps(
        result.to_dict()
    )


def test_sdn_cache_is_dated_and_reused(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return made_up_sdn_xml()

    url = "https://lists.example/SDN.XML"
    for _ in range(2):
        load_sdn(url, today=TODAY, fetch=fetch, cache_dir=tmp_path, min_entries=1)
    assert calls == [url]
    assert (tmp_path / "ofac-sdn-2026-10-08.json").exists()
    load_sdn(
        url,
        today=TODAY + dt.timedelta(days=1),
        fetch=fetch,
        cache_dir=tmp_path,
        min_entries=1,
    )
    assert len(calls) == 2


def test_sdn_refuses_http_and_a_short_list(tmp_path: Path) -> None:
    with pytest.raises(SanctionsListError):
        load_sdn(
            "http://lists.example/SDN.XML",
            today=TODAY,
            fetch=lambda u: "",
            cache_dir=None,
        )
    with pytest.raises(SanctionsListError) as caught:
        load_sdn(
            "https://lists.example/SDN.XML",
            today=TODAY,
            fetch=lambda u: made_up_sdn_xml(),
            cache_dir=None,
        )
    assert "expected at least 1000" in caught.value.message


def test_live_fetch_goes_through_the_ssrf_guard() -> None:
    with pytest.raises(UnsafeUrlError):
        live_fetch("https://lists.example/SDN.XML", resolver=lambda host: ["10.0.0.7"])
    with pytest.raises(UnsafeUrlError):
        live_fetch("file:///etc/passwd", resolver=lambda host: ["1.1.1.1"])


def test_sdn_unavailable_is_no_data_not_clear(folder: Path) -> None:
    def broken(url: str) -> str:
        raise OSError("down")

    result = run_watch(_deps(folder, sdn_fetch=broken), SHEET, confirm=True)
    rows = _rows(result)
    sanction = rows[("Made Up Circuits Inc.", "sanctions")]
    assert sanction["agreement"] == "no data" and sanction["summary"] == "not checked"
    assert any(n.startswith("Sanctions not checked") for n in result.notices)


# --- readers on unknown shapes -----------------------------------------------------


def test_unknown_shapes_read_as_no_data() -> None:
    odd = {"data": {"something": "else"}}
    assert hierarchy_from_akta(odd).parent.found is False
    assert trust_from_akta({"data": {}}) is None


def test_agreement_rules() -> None:
    same = lambda a, b: a == b  # noqa: E731
    assert agreement("a", "a", same) == "agree"
    assert agreement("a", "b", same) == "differ"
    assert agreement("a", None, same) == "only one"
    assert agreement(None, None, same) == "no data"
    assert agreement(0, 0, lambda a, b: (a > 0) == (b > 0)) == "agree"
    assert normalize_company("The Jabil Inc.") == normalize_company("JABIL") == "jabil"
    assert normalize_company("Celestica, Ltd") == "celestica"


# --- the sheet ---------------------------------------------------------------------


def test_sheet_normalizes_domains_and_skips_bad_rows(folder: Path) -> None:
    (folder / "s2.csv").write_text(
        "Name;Website;Ticker\nA Co;https://www.A-Co.example/x;aco\n"
        "B Co;not a domain;\nC Co;a-co.example;\n;d.example;\n",
        encoding="utf-8",
    )
    sheet = read_suppliers("s2.csv", folder)
    assert [(s.name, s.domain, s.ticker) for s in sheet.suppliers] == [
        ("A Co", "a-co.example", "ACO")
    ]
    assert len(sheet.notices) == 3
    assert normalize_domain("user@evil.example") is None


def test_sheet_needs_name_and_domain_and_stays_in_folder(folder: Path) -> None:
    (folder / "bad.csv").write_text("company,ticker\nX,XX\n", encoding="utf-8")
    with pytest.raises(SheetError):
        read_suppliers("bad.csv", folder)
    with pytest.raises(SheetOutsideFolderError):
        read_suppliers("../suppliers.csv", folder)


# --- the MCP surface ---------------------------------------------------------------


def test_surface_supplier_tools(folder: Path) -> None:
    surface = CommenterListSurface(deps_for("recorded", folder=folder), _deps(folder))
    names = [t["name"] for t in surface.list_tools()]
    assert names[2:4] == ["plan_supplier_watch", "run_supplier_watch"]
    plan = surface.call_tool("plan_supplier_watch", {"sheet": SHEET})
    assert plan["total_cents"] == 48 and not is_upstream_failure(plan)
    refused = surface.call_tool("run_supplier_watch", {"sheet": SHEET})
    assert refused["code"] == "not_confirmed"
    outside = surface.call_tool("plan_supplier_watch", {"sheet": "../x.csv"})
    assert outside["code"] == "sheet_outside_folder"
    done = surface.call_tool("run_supplier_watch", {"sheet": SHEET, "confirm": True})
    assert done["agreement_counts"]["differ"] == 3
    assert all("value_summary" not in row for row in done["rows"])


def test_surface_without_supplier_deps_hides_the_watch(folder: Path) -> None:
    surface = CommenterListSurface(deps_for("recorded", folder=folder))
    assert [t["name"] for t in surface.list_tools()] == [
        "plan_commenter_list",
        "build_commenter_list",
    ]


def test_lists_mount_serves_both_jobs() -> None:
    from gecko.orthogonal.surface import lists_surface

    names = [t["name"] for t in lists_surface("recorded").list_tools()]
    assert names == [
        "plan_commenter_list",
        "build_commenter_list",
        "plan_supplier_watch",
        "run_supplier_watch",
        "supplier_latest",
        "supplier_history",
        "supplier_changes_since",
        "supplier_sources",
        "xstock_file",
        "xstock_changes_since",
    ]


def test_query_values_are_sent_as_strings() -> None:
    """Live 2026-10-08: Orthogonal's /v1/run rejected `limit: 1` with HTTP 400 "Expected string"."""
    import datetime as dt

    from gecko.orthogonal.supplier.calls import planned_calls
    from gecko.orthogonal.supplier.sheet import Supplier

    supplier = Supplier(name="Made Up Co", domain="madeup.example", ticker="MUC")
    for call in planned_calls(
        supplier, month_start=dt.date(2026, 10, 1), website_since=dt.date(2026, 9, 1)
    ):
        body = call.body_for()(None, None)
        assert all(isinstance(v, str) for v in (body.get("query") or {}).values()), (
            call.key
        )
        paged = call.body_for()("2", "req_parent")
        assert all(isinstance(v, str) for v in (paged.get("query") or {}).values()), (
            call.key
        )

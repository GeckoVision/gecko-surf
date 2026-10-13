"""The living file per tokenized stock, offline. No wire, no key, no money, no DNS.

Every test runs the real job against injected fakes: a made-up chain (``MadeUpChain``),
a made-up Jupiter and legal pages (``MadeUpWeb``) and a made-up Orthogonal
(``RecordedXstockOrthogonal``). All values are invented.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gecko.caller import PreparedRequest
from gecko.livingfile import dataset
from gecko.livingfile.chain import NO_AUTHORITY, read_mint
from gecko.livingfile.checks import ReservesRead
from gecko.livingfile.deps import XstockDeps, xstock_deps_for
from gecko.livingfile.errors import LivingFileConfigError, UnknownMintError
from gecko.livingfile.fixture import (
    MADE_UP_MINT_AUTHORITY,
    MadeUpChain,
    MadeUpWeb,
    RecordedXstockOrthogonal,
    made_up_page,
)
from gecko.livingfile.job import plan_file, run_file
from gecko.livingfile.legal import key_sentences, page_text, read_legal, text_sha256
from gecko.livingfile.market import read_market, read_share_price
from gecko.livingfile.store import stored_rows
from gecko.livingfile.wire import live_public_fetch, live_rpc_call
from gecko.livingfile.xstocks import (
    LEGAL_PAGES,
    NVDAX,
    TSLAX,
    jupiter_url,
    xstock_for,
)
from gecko.netguard import UnsafeUrlError
from gecko.orthogonal.errors import (
    NotAllowedError,
    NotConfirmedError,
    SpendCapError,
)
from gecko.orthogonal.job import deps_for
from gecko.orthogonal.spec import RUN_OP, check_allowed
from gecko.orthogonal.spend import MemoryLedger
from gecko.orthogonal.supplier.store import FileFieldLedger, MemoryFieldLedger
from gecko.orthogonal.surface import CommenterListSurface
from gecko.token_program import TOKEN_2022_PROGRAM

T0 = 1791460000.0  # 2026-10-08, made up
MINT = NVDAX.mint


def _deps(
    folder: Path,
    *,
    at: float = T0,
    chain: MadeUpChain | None = None,
    web: MadeUpWeb | None = None,
    transport: Any = None,
    **kw: Any,
) -> XstockDeps:
    return xstock_deps_for(
        "recorded",
        folder=folder,
        clock=lambda: at,
        rpc_call=chain or MadeUpChain(),
        public_fetch=web or MadeUpWeb(),
        transport=transport or RecordedXstockOrthogonal(),
        **kw,
    )


def _by_field(result: Any) -> dict[str, dict[str, Any]]:
    return {r["field"]: r for r in result.rows}


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    return tmp_path / "gecko-lists"


# --- the plan: free -----------------------------------------------------------------


def test_plan_is_free_and_prices_the_one_paid_call(folder: Path) -> None:
    chain, web, orth = MadeUpChain(), MadeUpWeb(), RecordedXstockOrthogonal()
    plan = plan_file(_deps(folder, chain=chain, web=web, transport=orth), MINT)
    assert plan.total_cents == 3  # $0.025 listed, counted up
    assert [c[2] for c in plan.calls] == [0, 0, 3, 0, 0]
    assert "$0.03" in plan.message and "Continue?" in plan.message
    assert chain.calls == [] and web.fetched == []
    assert {
        r.url.rsplit("/", 2)[-2] + "/" + r.url.rsplit("/", 1)[-1] for r in orth.requests
    } == {
        "credits/balance",
        "v1/details",
    }


def test_run_refuses_without_confirm_and_reads_nothing(folder: Path) -> None:
    chain = MadeUpChain()
    with pytest.raises(NotConfirmedError):
        run_file(_deps(folder, chain=chain), MINT, confirm=False)
    assert chain.calls == [] and not folder.exists()


@pytest.mark.parametrize(
    ("per_run", "spent"),
    [(2, 0), (5, 18)],  # one call over the per-run cap; the total cap almost used
)
def test_caps_refuse_before_any_call(folder: Path, per_run: int, spent: int) -> None:
    orth = RecordedXstockOrthogonal()
    deps = _deps(
        folder, transport=orth, per_run_cap=per_run, ledger=MemoryLedger(spent)
    )
    with pytest.raises(SpendCapError):
        run_file(deps, MINT, confirm=True)
    assert not any(r.url.endswith("/v1/run") for r in orth.requests)


def test_unknown_mint_is_refused_not_guessed(folder: Path) -> None:
    with pytest.raises(UnknownMintError):
        plan_file(_deps(folder), "So11111111111111111111111111111111111111112")
    with pytest.raises(UnknownMintError):
        xstock_for("not a mint")


# --- one run, end to end --------------------------------------------------------------


def test_recorded_run_writes_every_fact_with_source_and_time(folder: Path) -> None:
    orth = RecordedXstockOrthogonal()
    result = run_file(_deps(folder, transport=orth), MINT, confirm=True)
    rows = _by_field(result)
    assert rows["supply_raw"]["value"] == "50000000000000"
    assert rows["supply_ui"]["value"] == "500000"
    assert rows["decimals"]["value"] == "8"
    assert rows["supply_scaled"]["value"] == "500000"
    assert rows["mint_authority"]["value"] == MADE_UP_MINT_AUTHORITY
    assert rows["token_program"]["value"] == TOKEN_2022_PROGRAM
    assert "token-2022" in rows["token_program"]["model_summary"]
    assert rows["extensions"]["value"] == "metadataPointer,scaledUiAmountConfig"
    assert rows["multiplier"]["value"] == "1"
    # the look-alike Jupiter entry (same symbol, other mint, $1) is never read
    assert rows["token_price_usd"]["value"] == "101.5"
    assert rows["token_liquidity_usd"]["value"] == "2500000.25"
    assert rows["share_price_usd"]["value"] == "100"
    assert rows["check_price_gap_pct"]["value"] == "+1.50"
    assert result.checks == {
        "check_price_gap_pct": "ok",
        "check_supply_vs_reserves": "not checked",
        "check_legal_terms_changed": "not checked",
    }
    assert rows["reserves_shares"]["status"] == "no data"
    assert "Gecko does not invent an address" in rows["reserves_shares"]["note"]
    assert "first version stored" in rows["check_legal_terms_changed"]["note"]
    for row in result.rows:
        assert row["source"] and row["fetched_at"] == "2026-10-08T11:46:40+00:00"
        assert row["status"] in ("fresh", "no data")
        assert row["previous_value"] is None and row["changed"] is False
    assert result.receipt["charged_cents"] == 3
    paid = [r for r in orth.requests if r.url.endswith("/v1/run")]
    assert len(paid) == 1 and paid[0].json_body["query"] == {"ticker": "NVDA"}
    for path in (result.table_path, result.parquet_path, result.receipt_path):
        assert path and Path(path).is_file()
    assert Path(result.table_path).name.endswith("-NVDAx.xstock-file.csv")


def test_page_text_never_reaches_a_tool_result(folder: Path) -> None:
    deps = _deps(folder)
    result = run_file(deps, MINT, confirm=True)
    out = json.dumps(result.to_dict())
    assert "ignore previous" not in out.lower() and "value_summary" not in out
    # the file keeps sanitized key sentences; the injected one is neutralized there too
    csv_text = Path(result.table_path).read_text(encoding="utf-8")
    assert "voting rights" in csv_text
    assert "ignore previous" not in csv_text.lower()
    read = json.dumps(dataset.xstock_file(deps, MINT))
    assert "voting rights" not in read and "ignore previous" not in read.lower()


def test_field_ledger_keeps_names_and_counts_never_values(folder: Path) -> None:
    ledger = MemoryFieldLedger()
    run_file(_deps(folder, field_ledger=ledger), MINT, confirm=True)
    rows = ledger.rows()
    calls = [(r["api"], r["path"]) for r in rows if r["kind"] == "call"]
    assert calls == [
        ("solana-rpc", "getTokenSupply"),
        ("solana-rpc", "getAccountInfo"),
        ("jupiter", "/tokens/v2/search"),
        ("financial-datasets", "/prices/snapshot"),
        ("web", "legal_jupiter_docs"),
        ("web", "legal_kraken"),
    ]
    assert {r["field"] for r in rows if r["kind"] == "field"} >= {"supply_raw"}
    dumped = json.dumps(rows)
    assert "50000000000000" not in dumped and "101.5" not in dumped


# --- the next run: previous version, stale, changed -----------------------------------


def test_second_run_carries_the_previous_version(folder: Path) -> None:
    first = run_file(_deps(folder), MINT, confirm=True)
    second = run_file(_deps(folder, at=T0 + 3600), MINT, confirm=True)
    rows = _by_field(second)
    assert rows["supply_raw"]["previous_value"] == "50000000000000"
    assert rows["supply_raw"]["previous_run_id"] == first.run_id
    assert rows["supply_raw"]["changed"] is False
    assert second.checks["check_legal_terms_changed"] == "ok"
    assert rows["check_legal_terms_changed"]["value"] == "unchanged"


def test_a_failed_source_goes_stale_with_the_last_good_value(folder: Path) -> None:
    first = run_file(_deps(folder), MINT, confirm=True)
    down = MadeUpChain(fail=frozenset({"getTokenSupply"}))
    second = run_file(_deps(folder, at=T0 + 3600, chain=down), MINT, confirm=True)
    rows = _by_field(second)
    assert rows["supply_raw"]["status"] == "stale"
    assert rows["supply_raw"]["value"] == "50000000000000"
    assert rows["supply_raw"]["stale_since"] == second.run_id
    assert first.run_id in rows["supply_raw"]["note"]
    assert rows["mint_authority"]["status"] == "fresh"  # the other call still answered
    # the price gap does not read supply, so it still has a verdict
    assert rows["check_price_gap_pct"]["status"] == "fresh"


def test_moved_legal_page_turns_the_check_red_and_shows_in_changes(
    folder: Path,
) -> None:
    first = run_file(_deps(folder), MINT, confirm=True)
    web = MadeUpWeb()
    kraken = LEGAL_PAGES[1].url
    web.pages[kraken] = (
        200,
        made_up_page("Made-up Kraken", "Holders now have a legal claim."),
    )
    second = run_file(_deps(folder, at=T0 + 3600, web=web), MINT, confirm=True)
    rows = _by_field(second)
    assert rows["legal_kraken"]["changed"] is True
    assert "changed since run" in rows["legal_kraken"]["model_summary"]
    assert second.checks["check_legal_terms_changed"] == "red"
    assert "legal_kraken" in rows["check_legal_terms_changed"]["model_summary"]
    diff = dataset.xstock_changes_since(_deps(folder), first.run_id)
    assert diff["against_run"] == second.run_id
    moved = {c["field"]: c for c in diff["changes"]}
    assert "legal_kraken" in moved and moved["legal_kraken"]["mint"] == MINT
    assert "legal_jupiter_docs" not in moved


def test_a_footer_change_is_not_a_terms_change(folder: Path) -> None:
    run_file(_deps(folder), MINT, confirm=True)
    web = MadeUpWeb()
    for page in LEGAL_PAGES:
        web.pages[page.url] = (200, made_up_page(f"Made-up {page.label}", stamp="2"))
    second = run_file(_deps(folder, at=T0 + 3600, web=web), MINT, confirm=True)
    assert second.checks["check_legal_terms_changed"] == "ok"


# --- checks -------------------------------------------------------------------------


def test_gap_above_the_threshold_is_red(folder: Path) -> None:
    result = run_file(_deps(folder), TSLAX.mint, confirm=True)
    assert _by_field(result)["check_price_gap_pct"]["value"] == "+5.00"
    assert result.checks["check_price_gap_pct"] == "red"
    tight = run_file(
        _deps(folder, at=T0 + 1, gap_threshold_pct=Decimal("1")), MINT, confirm=True
    )
    assert tight.checks["check_price_gap_pct"] == "red"


def test_supply_vs_reserves_runs_when_a_reserves_reader_exists(folder: Path) -> None:
    def short(_x: Any) -> ReservesRead:
        return ReservesRead(Decimal("400000"), "made-up reserves feed")

    result = run_file(_deps(folder, reserves=short), MINT, confirm=True)
    rows = _by_field(result)
    assert rows["reserves_shares"]["value"] == "400000"
    assert result.checks["check_supply_vs_reserves"] == "red"
    assert rows["check_supply_vs_reserves"]["value"] == "-100000"


def test_a_failed_paid_call_costs_its_fact_not_the_run(folder: Path) -> None:
    class Broken(RecordedXstockOrthogonal):
        def __call__(self, req: PreparedRequest) -> tuple[int, Any]:
            if req.url.endswith("/v1/run"):
                self.requests.append(req)
                return 500, {}
            return super().__call__(req)

    result = run_file(_deps(folder, transport=Broken()), MINT, confirm=True)
    rows = _by_field(result)
    assert rows["share_price_usd"]["status"] == "no data"
    assert result.checks["check_price_gap_pct"] == "not checked"
    assert rows["supply_raw"]["status"] == "fresh"
    assert any("/prices/snapshot" in n for n in result.notices)


# --- readers --------------------------------------------------------------------------


def test_chain_reader_tells_absent_from_unread_and_applies_a_due_multiplier() -> None:
    chain = MadeUpChain(multiplier="1.0", new_multiplier="1.05", effective_ts=100)

    def call(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        resp = chain(url, method, params)
        if method == "getAccountInfo":
            info = resp["result"]["value"]["data"]["parsed"]["info"]
            info["freezeAuthority"] = None
        return resp

    read = read_mint("https://rpc.example", MINT, call, now_ts=200)
    assert read.freeze_authority == NO_AUTHORITY
    assert read.multiplier == Decimal("1.05")
    assert read.supply_scaled() == Decimal("525000.00000000")
    pending = read_mint(
        "https://rpc.example",
        MINT,
        MadeUpChain(new_multiplier="2", effective_ts=900),
        now_ts=200,
    )
    assert (
        pending.multiplier == Decimal("1.0")
        and "takes effect" in pending.multiplier_note
    )
    down = read_mint(
        "https://rpc.example",
        MINT,
        MadeUpChain(fail=frozenset({"getAccountInfo"})),
        now_ts=0,
    )
    assert down.mint_authority is None and down.multiplier is None
    assert down.supply_raw == "50000000000000"


def test_jupiter_reader_reads_only_this_mint() -> None:
    def fetch(url: str) -> tuple[int, str]:
        return 200, json.dumps([{"id": "Other", "usdPrice": 5, "liquidity": 1}])

    market = read_market("https://jup.example/x", MINT, fetch)
    assert market.usd_price is None and "no entry" in market.reason
    limited = read_market("https://jup.example/x", MINT, lambda _u: (429, ""))
    assert limited.usd_price is None and "429" in limited.reason


def test_share_price_reader_is_defensive() -> None:
    assert read_share_price({"data": {"price": "12.5"}}, "NVDA").price == Decimal(
        "12.5"
    )
    wrong = read_share_price(
        {"data": {"snapshot": {"ticker": "AMD", "price": 1}}}, "NVDA"
    )
    assert wrong.price is None and "different ticker" in wrong.reason
    assert (
        read_share_price({"data": {"snapshot": {"price": "n/a"}}}, "NVDA").price is None
    )


def test_legal_reader_hashes_the_main_text_and_refuses_a_shell() -> None:
    markup = made_up_page("Made-up")
    text = page_text(markup)
    assert "Date.now" not in text and "footer" not in text and "menu" not in text
    assert text_sha256(text) == text_sha256(
        page_text(made_up_page("Made-up", stamp="9"))
    )
    assert "This invented page is not equivalent" in key_sentences(text)[0]
    shell = read_legal(LEGAL_PAGES[0], lambda _u: (200, "<div id=root></div>"))
    assert shell.sha256 is None and "none of the expected legal terms" in shell.reason
    gone = read_legal(LEGAL_PAGES[0], lambda _u: (404, ""))
    assert gone.sha256 is None and "404" in gone.reason


# --- the wire and the config: refused before any socket --------------------------------


def _private(_host: str) -> list[str]:
    return ["10.0.0.7"]


def test_live_reads_go_through_netguard() -> None:
    with pytest.raises(UnsafeUrlError):
        live_public_fetch("http://docs.example/x")
    with pytest.raises(UnsafeUrlError):
        live_public_fetch("https://docs.example/x", resolver=_private)
    with pytest.raises(UnsafeUrlError):
        live_rpc_call("http://rpc.example", "getTokenSupply", [MINT])
    with pytest.raises(UnsafeUrlError):
        live_rpc_call(
            "https://rpc.example", "getTokenSupply", [MINT], resolver=_private
        )


def test_config_urls_are_checked_and_a_paid_rpc_key_never_lands_in_a_file(
    folder: Path,
) -> None:
    with pytest.raises(LivingFileConfigError):
        xstock_deps_for("recorded", rpc_url="http://rpc.example")
    with pytest.raises(LivingFileConfigError):
        jupiter_url("https://jup.example/search?query=NVDAx", MINT)
    deps = _deps(folder, rpc_url="https://rpc.example/?api-key=SECRETKEY")
    result = run_file(deps, MINT, confirm=True)
    for path in (result.table_path, result.receipt_path):
        assert "SECRETKEY" not in Path(path).read_text(encoding="utf-8")
    assert "SECRETKEY" not in json.dumps(result.to_dict())


def test_the_key_may_read_the_share_price_and_nothing_near_it() -> None:
    body = {"api": "financial-datasets", "path": "/prices/snapshot"}
    check_allowed(RUN_OP, {**body, "query": {"ticker": "NVDA"}})
    with pytest.raises(NotAllowedError):
        check_allowed(RUN_OP, {**body, "query": {"ticker": "NVDA", "limit": "9"}})


def test_this_job_keeps_its_own_field_ledger(tmp_path: Path) -> None:
    ledger = FileFieldLedger(tmp_path, "xstock-field-ledger")
    assert ledger.jsonl_path.name == "xstock-field-ledger.jsonl"
    assert FileFieldLedger(tmp_path).jsonl_path.name == "field-ledger.jsonl"


# --- the read API and the MCP tools ---------------------------------------------------


def test_surface_serves_the_stored_file_and_changes(folder: Path) -> None:
    deps = _deps(folder)
    surface = CommenterListSurface(deps_for("recorded", folder=folder), None, deps)
    names = [t["name"] for t in surface.list_tools()]
    assert names[-2:] == ["xstock_file", "xstock_changes_since"]
    empty = surface.call_tool("xstock_file", {"mint": MINT})
    assert empty["rows"] == [] and empty["run_id"] is None
    first = run_file(deps, MINT, confirm=True)
    run_file(_deps(folder, at=T0 + 60), MINT, confirm=True)
    got = surface.call_tool("xstock_file", {"mint": MINT})
    assert (
        got["runs_stored"] == 2 and got["checks"]["check_legal_terms_changed"] == "ok"
    )
    assert all("value_summary" not in row for row in got["rows"])
    changes = surface.call_tool("xstock_changes_since", {"run_id": first.run_id})
    # the only move: the legal check went from "first version" to "unchanged"
    assert [c["field"] for c in changes["changes"]] == ["check_legal_terms_changed"]
    assert changes["since_run"] == first.run_id
    missing = surface.call_tool("xstock_changes_since", {"run_id": "nope"})
    assert missing["code"] == "run_not_found" and "xstock_file" in missing["error"]
    bad = surface.call_tool("xstock_file", {"mint": "x"})
    assert bad["code"] == "unknown_mint"


def test_a_recorded_surface_never_reads_live_runs(folder: Path) -> None:
    run_file(_deps(folder), MINT, confirm=True)
    rows = stored_rows(folder)
    assert rows and {r["mode"] for r in rows} == {"recorded"}
    live = xstock_deps_for(
        "live", folder=folder, ledger=MemoryLedger(), field_ledger=MemoryFieldLedger()
    )
    assert dataset.xstock_file(live, MINT)["rows"] == []


def test_cli_recorded_run_and_plan_only_in_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.util

    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "xstock_file_cli", Path(__file__).parent.parent / "scripts" / "xstock_file.py"
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["--mode", "recorded", "--mint", MINT]) == 0
    out = capsys.readouterr().out
    assert "[recorded]" in out and "gap +1.50%" in out and "[ok]" in out
    assert (tmp_path / "gecko-lists").is_dir()

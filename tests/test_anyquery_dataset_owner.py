"""The owner's dataset tools on /anyquery/mcp, and the run_rows / run_changes rename.

FakeBilling, MemoryStore and the scripted runner from the surface tests: no money, no
model, no source. A refresh charges through run_query's own path, once.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from gecko.anyquery.hosted import AnyQuerySurface
from gecko.anyquery.service import AnyQueryDeps, call_anyquery_tool
from gecko.anyquery.snapshots import LocalFolderSink, SnapshotError, snapshot_key
from gecko.anyquery.surface import ANYQUERY_TOOLS, RENAMED_TOOLS
from tests.test_anyquery_surface import (
    Clock,
    ACCOUNT,
    OTHER,
    ScriptedRun,
    _deps,
    _plan_and_run,
)

NAME = "funding"


def _c(deps: AnyQueryDeps, tool: str, account: str = ACCOUNT, **args: Any) -> dict:
    # Not the surface tests' _call: its ``name`` parameter is the tool's name, and
    # save_bundle and create_dataset both take an argument called name.
    return call_anyquery_tool(deps, account, tool, args)


def _charges(deps: AnyQueryDeps) -> int:
    return sum(1 for e in deps.store.ledger(ACCOUNT, limit=500) if e.kind == "charge")


def _bundled(deps: AnyQueryDeps) -> str:
    planned, ran = _plan_and_run(deps)
    saved = _c(deps, "save_bundle", plan_id=planned["plan"]["plan_id"], name=NAME)
    assert "error" not in saved, saved
    return str(ran["run"]["run_id"])


def test_create_dataset_from_a_paid_run_spends_nothing_and_makes_v1(
    tmp_path: Path,
) -> None:
    deps = dataclasses.replace(
        _deps(tmp_path), dataset_sink=LocalFolderSink(tmp_path / "snap")
    )
    run_id = _bundled(deps)
    charges, runs = _charges(deps), len(deps.store.runs(ACCOUNT, limit=50))
    out = _c(deps, "create_dataset", bundle=NAME, from_run=run_id)
    assert out["status"] == "adopted" and out["spent"] == "nothing"
    assert out["version"]["n"] == 1 and out["version"]["snapshot_status"] == "written"
    assert out["dataset"]["endpoint"] == "/d/funding/mcp"
    assert out["dataset"]["fields"] == ["funding", "headcount"]
    assert _charges(deps) == charges
    assert len(deps.store.runs(ACCOUNT, limit=50)) == runs
    listed = _c(deps, "datasets")["datasets"]
    assert [d["dataset_id"] for d in listed] == [NAME] and listed[0]["version"] == 1
    assert _c(deps, "datasets", account=OTHER)["datasets"] == []


def test_create_dataset_without_a_run_waits_for_refresh(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    _bundled(deps)
    out = _c(deps, "create_dataset", bundle=NAME, name="leads-q4")
    assert out["status"] == "no_version_yet" and out["version"] is None
    assert "refresh_dataset" in out["next"]
    again = _c(deps, "create_dataset", bundle=NAME, name="leads-q4")
    assert again["code"] == "dataset_exists"
    assert _c(deps, "create_dataset", bundle="nope")["code"] == "bundle_not_found"
    assert _c(deps, "create_dataset", bundle=NAME, name="Bad Name")["code"] == (
        "bad_arguments"
    )


def test_a_credited_run_is_never_adopted_and_leaves_no_dataset(tmp_path: Path) -> None:
    deps = _deps(tmp_path, runner=ScriptedRun([{}]))
    planned, ran = _plan_and_run(deps)
    assert ran["run"]["payment_status"] == "credited"
    _c(deps, "save_bundle", plan_id=planned["plan"]["plan_id"], name=NAME)
    out = _c(deps, "create_dataset", bundle=NAME, from_run=ran["run"]["run_id"])
    assert out["code"] == "run_withheld"
    assert _c(deps, "datasets")["datasets"] == []


def test_refresh_quotes_first_then_charges_once_and_adopts(tmp_path: Path) -> None:
    clock = Clock()
    deps = _deps(tmp_path, clock=clock)
    run_id = _bundled(deps)
    _c(deps, "create_dataset", bundle=NAME, from_run=run_id)
    # A week later: run ids order by time, and two runs in one second order by chance.
    clock.now = "2026-10-15T12:00:00Z"
    charges, runs = _charges(deps), len(deps.store.runs(ACCOUNT, limit=50))

    quoted = _c(deps, "refresh_dataset", dataset_id=NAME, confirm=False)
    assert quoted["status"] == "needs_confirmation" and quoted["spent"] == "nothing"
    assert "refresh_dataset" in quoted["next"]
    assert _charges(deps) == charges
    assert len(deps.store.runs(ACCOUNT, limit=50)) == runs

    args: dict[str, Any] = {
        "dataset_id": NAME,
        "quote_id": quoted["quote"]["quote_id"],
        "confirm": True,
    }
    done = _c(deps, "refresh_dataset", **args)
    assert done["status"] == "refreshed", done
    assert done["version"]["n"] == 2 and done["changes"] == 1  # acme's funding
    assert "rows" not in done
    assert _charges(deps) == charges + 1

    again = _c(deps, "refresh_dataset", **args)
    assert again["status"] == "refreshed" and again["repeated"] is True
    assert again["version"]["n"] == 2 and again["changes"] == 0
    assert _charges(deps) == charges + 1


def test_refresh_refuses_what_the_dataset_does_not_hold(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    run_id = _bundled(deps)
    _c(deps, "create_dataset", bundle=NAME, from_run=run_id)
    out = _c(
        deps, "refresh_dataset", dataset_id=NAME, confirm=False, subjects=["initech"]
    )
    assert out["code"] == "subjects_not_in_dataset"
    out = _c(deps, "refresh_dataset", dataset_id="nope", confirm=False)
    assert out["code"] == "dataset_not_found"
    out = _c(deps, "refresh_dataset", dataset_id=NAME, confirm="yes")
    assert out["code"] == "bad_arguments"
    assert _c(deps, "refresh_dataset", account=OTHER, dataset_id=NAME, confirm=False)[
        "code"
    ] == ("dataset_not_found")


def test_a_subset_refresh_adds_its_plan_and_keeps_the_table_whole(
    tmp_path: Path,
) -> None:
    deps = _deps(tmp_path)
    run_id = _bundled(deps)
    _c(deps, "create_dataset", bundle=NAME, from_run=run_id)
    quoted = _c(
        deps, "refresh_dataset", dataset_id=NAME, confirm=False, subjects=["acme"]
    )
    done = _c(
        deps,
        "refresh_dataset",
        dataset_id=NAME,
        subjects=["acme"],
        quote_id=quoted["quote"]["quote_id"],
        confirm=True,
    )
    assert done["status"] == "refreshed" and done["version"]["n"] == 2
    held = deps.store.dataset(ACCOUNT, NAME)
    assert held is not None and len(held.plan_ids) == 2
    assert len(deps.store.dataset_rows(ACCOUNT, NAME)) == 2


# -- the rename ---------------------------------------------------------------------------------


def test_the_old_names_still_answer_and_say_the_new_one(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    _first, ran = _plan_and_run(deps)
    old = _c(deps, "dataset", run_id=ran["run"]["run_id"])
    new = _c(deps, "run_rows", run_id=ran["run"]["run_id"])
    assert old["renamed"] == "run_rows" and old["rows"] == new["rows"]
    changes = _c(deps, "changes_since", run_id=ran["run"]["run_id"])
    assert changes["renamed"] == "run_changes" and "changes" in changes


def test_the_old_names_are_hidden_from_every_list(tmp_path: Path) -> None:
    listed = {t["name"] for t in ANYQUERY_TOOLS}
    assert not listed & set(RENAMED_TOOLS)
    assert set(RENAMED_TOOLS.values()) <= listed
    surface = AnyQuerySurface(_deps(tmp_path))
    assert not {t["name"] for t in surface.list_tools()} & set(RENAMED_TOOLS)
    assert surface.hidden_tools == frozenset(RENAMED_TOOLS)
    out = surface.call_tool("dataset", {}, account=ACCOUNT)
    assert out["renamed"] == "run_rows" and out["code"] == "no_runs"


@pytest.fixture
def _gate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GECKO_GATED_SURFACES",
        "GECKO_REQUIRE_KEY",
        "GECKO_BOOTCAMP_TEAMS",
        "MONGODB_URI",
        "PRIVY_APP_ID",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("_gate_env")
def test_an_old_name_works_over_mcp_though_it_is_not_listed(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient

    from tests.test_anyquery_surface import _gate_app, _tool_call

    deps = _deps(tmp_path)
    _plan_and_run(deps)
    app, key = _gate_app(deps, ["anyquery"])
    with TestClient(app) as client:
        out = _tool_call(client, key, "dataset", {})
    assert out["renamed"] == "run_rows" and len(out["rows"]) == 4


# -- a snapshot that fails after the charge never hides the adoption ---------------------


@dataclasses.dataclass(frozen=True)
class FlakySink(LocalFolderSink):
    """A local sink that can be switched to fail as S3 does: a put, or a list."""

    down: dict[str, bool] = dataclasses.field(default_factory=dict)

    def put_once(self, key: str, data: bytes, metadata: dict[str, str]) -> str:
        if self.down.get("put"):
            raise SnapshotError("S3 put failed (SlowDown, 503)")
        return super().put_once(key, data, metadata)

    def any_under(self, prefix: str) -> bool:
        if self.down.get("list"):
            raise SnapshotError("S3 list failed (AccessDenied, 403)")
        return super().any_under(prefix)


def _confirmed_refresh(deps: AnyQueryDeps, clock: Clock) -> dict[str, Any]:
    clock.now = "2026-10-15T12:00:00Z"
    quoted = _c(deps, "refresh_dataset", dataset_id=NAME, confirm=False)
    args = {"dataset_id": NAME, "quote_id": quoted["quote"]["quote_id"]}
    return {**_c(deps, "refresh_dataset", confirm=True, **args), "_args": args}


def _versions(deps: AnyQueryDeps) -> list[tuple[int, str]]:
    held = deps.store.dataset(ACCOUNT, NAME)
    assert held is not None
    return [(v.n, v.snapshot_status) for v in held.versions]


def test_a_snapshot_clash_after_the_charge_still_reports_the_adoption(
    tmp_path: Path,
) -> None:
    clock = Clock()
    sink = LocalFolderSink(tmp_path / "snap")
    deps = dataclasses.replace(_deps(tmp_path, clock=clock), dataset_sink=sink)
    _c(deps, "create_dataset", bundle=NAME, from_run=_bundled(deps))
    sink.put_once(snapshot_key(ACCOUNT, NAME, 2), b"other", {"rows_sha256": "other"})
    charges = _charges(deps)

    done = _confirmed_refresh(deps, clock)
    assert done["status"] == "refreshed", done
    assert done["version"]["n"] == 2 and done["version"]["snapshot_status"] == "pending"
    assert done["snapshot"]["code"] == "snapshot_clash"
    assert _charges(deps) == charges + 1
    assert _versions(deps) == [(1, "written"), (2, "pending")]


def test_a_sink_failure_after_the_charge_is_an_answer_not_a_raise(
    tmp_path: Path,
) -> None:
    clock = Clock()
    sink = FlakySink(tmp_path / "snap")
    deps = dataclasses.replace(_deps(tmp_path, clock=clock), dataset_sink=sink)
    run_id = _bundled(deps)
    sink.down["put"] = True

    made = _c(deps, "create_dataset", bundle=NAME, from_run=run_id)
    assert made["status"] == "adopted", made
    assert made["version"]["snapshot_status"] == "pending"
    assert made["snapshot"]["code"] == "snapshot_unavailable"
    assert "S3" not in json.dumps(made)  # the sink's words never reach the caller

    charges = _charges(deps)
    done = _confirmed_refresh(deps, clock)
    assert done["status"] == "refreshed", done
    assert done["snapshot"]["code"] == "snapshot_unavailable"
    assert _charges(deps) == charges + 1
    assert _versions(deps) == [(1, "pending"), (2, "pending")]

    # Back up: replaying the same confirmed call charges nothing and writes them.
    sink.down["put"] = False
    again = _c(deps, "refresh_dataset", confirm=True, **done["_args"])
    assert again["status"] == "refreshed" and again["repeated"] is True
    assert "snapshot" not in again and _charges(deps) == charges + 1
    assert _versions(deps) == [(1, "written"), (2, "written")]


def test_a_sink_that_cannot_list_refuses_create_without_a_raise(
    tmp_path: Path,
) -> None:
    sink = FlakySink(tmp_path / "snap")
    deps = dataclasses.replace(_deps(tmp_path), dataset_sink=sink)
    run_id = _bundled(deps)
    sink.down["list"] = True
    out = _c(deps, "create_dataset", bundle=NAME, from_run=run_id)
    assert out["code"] == "snapshot_unavailable" and "S3" not in out["error"]
    assert _c(deps, "datasets")["datasets"] == []

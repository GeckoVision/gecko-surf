"""The hosted xStock file: two read tools over stored runs, behind a Gecko key.

Founder rulings, 8 Oct 2026: ``/xstock/mcp`` reuses the bootcamp mount's key gate (#585),
and the share price stays in the hosted file. The mount reads stored runs only: it holds
no Orthogonal key, has a zero cap, and cannot reach a paid or ``--lists`` tool.

Offline: the runs are written by the real job against the recorded fakes, the gate is the
real Gecko-key resolver over an in-memory registry, and the transport is Starlette's
in-process TestClient. No wire, no key, no money.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")
pytest.importorskip("pyarrow")

from starlette.testclient import TestClient  # noqa: E402

from gecko.http_server import build_multi_surface_app  # noqa: E402
from gecko.keyregistry import InMemoryKeyRegistry, hash_key, mint_key  # noqa: E402
from gecko.livingfile import dataset  # noqa: E402
from gecko.livingfile.deps import xstock_deps_for  # noqa: E402
from gecko.livingfile.fixture import (  # noqa: E402
    MadeUpChain,
    MadeUpWeb,
    RecordedXstockOrthogonal,
)
from gecko.livingfile.hosted import (  # noqa: E402
    XSTOCK_ROOT_ENV,
    XstockReadSurface,
    build_xstock_read_surface,
)
from gecko.livingfile.job import run_file  # noqa: E402
from gecko.livingfile.xstocks import NVDAX  # noqa: E402
from gecko.orthogonal import surface as lists_surface  # noqa: E402
from gecko.serve_mcp import XSTOCK_SURFACE, default_gated_surfaces  # noqa: E402

T0 = 1791460000.0  # 2026-10-08, made up
MINT = NVDAX.mint
ACCOUNT = "did:gecko:judge"
READ_TOOLS = {dataset.FILE_TOOL, dataset.CHANGES_TOOL}

#: Every tool the ``--lists`` surface serves that is not one of the two reads: the paid
#: runs, their plans, and the supplier watch. None of them may answer on this mount.
LISTS_ONLY_TOOLS = sorted(
    {t["name"] for t in lists_surface._TOOLS} | set(lists_surface.WATCH_TOOLS)
)

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        XSTOCK_ROOT_ENV,
        "GECKO_GATED_SURFACES",
        "GECKO_REQUIRE_KEY",
        "GECKO_BOOTCAMP_TEAMS",
        "GECKO_COURSE_ROOT",
        "MONGODB_URI",
        "PRIVY_APP_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def _record_run(folder: Path, at: float) -> str:
    deps = xstock_deps_for(
        "recorded",
        folder=folder,
        clock=lambda: at,
        rpc_call=MadeUpChain(),
        public_fetch=MadeUpWeb(),
        transport=RecordedXstockOrthogonal(),
    )
    return run_file(deps, MINT, confirm=True).run_id


@pytest.fixture
def runs(tmp_path: Path) -> tuple[Path, list[str]]:
    folder = tmp_path / "xstock-runs"
    ids = [_record_run(folder, T0), _record_run(folder, T0 + 3600)]
    return folder, ids


def _surface(folder: Path) -> XstockReadSurface:
    surface = build_xstock_read_surface(folder)
    assert surface is not None
    return surface


# --- the mount appears only when there are runs to serve ---------------------------


def test_no_root_configured_means_no_surface() -> None:
    assert build_xstock_read_surface() is None


def test_a_root_with_no_runs_means_no_surface(tmp_path: Path) -> None:
    assert build_xstock_read_surface(tmp_path / "missing") is None
    tmp_path.joinpath("empty").mkdir()
    assert build_xstock_read_surface(tmp_path / "empty") is None


def test_other_jobs_runs_are_not_xstock_runs(tmp_path: Path) -> None:
    """A folder holding only supplier-watch files would mount a surface that answers
    'no stored file' to every mint: a broken deploy that looks like a correct answer."""
    folder = tmp_path / "lists"
    folder.mkdir()
    (folder / "20261008-082250.supplier-watch.parquet").write_bytes(b"not ours")
    assert build_xstock_read_surface(folder) is None


def test_a_corrupt_run_keeps_the_mount_absent_instead_of_crashing_the_host(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "runs"
    folder.mkdir()
    (folder / "20261008-224606-NVDAx.xstock-file.parquet").write_bytes(b"garbage")
    assert build_xstock_read_surface(folder) is None


def test_the_hosted_list_has_no_xstock_mount_without_runs() -> None:
    import gecko.serve_mcp as serve_mcp

    names = [name for name, _ in serve_mcp._build_surfaces("block")]
    assert XSTOCK_SURFACE not in names


def test_the_hosted_list_mounts_xstock_when_the_root_holds_runs(
    runs: tuple[Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    import gecko.serve_mcp as serve_mcp

    monkeypatch.setenv(XSTOCK_ROOT_ENV, str(runs[0]))
    surfaces = dict(serve_mcp._build_surfaces("block"))
    assert isinstance(surfaces[XSTOCK_SURFACE], XstockReadSurface)


# --- what the mount serves: two reads, nothing that spends --------------------------


def test_only_the_two_read_tools_are_listed(runs: tuple[Path, list[str]]) -> None:
    tools = _surface(runs[0]).list_tools()
    assert {t["name"] for t in tools} == READ_TOOLS
    assert all(t["annotations"]["readOnlyHint"] for t in tools)


@pytest.mark.parametrize("name", LISTS_ONLY_TOOLS)
def test_no_paid_or_lists_tool_answers(runs: tuple[Path, list[str]], name: str) -> None:
    result = _surface(runs[0]).call_tool(name, {"confirm": True, "sheet": "x.csv"})
    assert result["code"] == "unknown_tool"


def test_the_mount_holds_no_key_and_cannot_spend(
    runs: tuple[Path, list[str]],
) -> None:
    surface = _surface(runs[0])
    base = surface.deps.base
    assert base.per_run_cap == 0 and base.total_cap == 0
    assert base.api.session.auth_headers() == {}
    surface.call_tool(dataset.FILE_TOOL, {"mint": MINT})
    surface.call_tool(dataset.CHANGES_TOOL, {"run_id": runs[1][0]})
    assert base.ledger.total_cents() == 0


def test_the_file_comes_from_the_latest_stored_run(
    runs: tuple[Path, list[str]],
) -> None:
    folder, ids = runs
    result = _surface(folder).call_tool(dataset.FILE_TOOL, {"mint": MINT})
    assert result["run_id"] == ids[-1]
    assert result["runs_stored"] == 2
    assert result["mode"] == "recorded"
    fields = {r["field"] for r in result["rows"]}
    assert "share_price_usd" in fields  # founder ruling: the share price stays
    assert "stored run" in result["served_from"]


def test_changes_since_compares_the_two_stored_runs(
    runs: tuple[Path, list[str]],
) -> None:
    folder, ids = runs
    result = _surface(folder).call_tool(dataset.CHANGES_TOOL, {"run_id": ids[0]})
    assert result["against_run"] == ids[1]


def test_an_unknown_mint_is_a_refusal_not_a_crash(
    runs: tuple[Path, list[str]],
) -> None:
    result = _surface(runs[0]).call_tool(dataset.FILE_TOOL, {"mint": "nope"})
    assert result["code"] == "unknown_mint"


def test_the_mount_serves_live_runs_when_the_root_holds_them(
    runs: tuple[Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot in the image is two live runs. The surface serves the mode the runs
    carry, live first, so the hosted file is not filtered down to nothing."""
    import gecko.livingfile.hosted as hosted

    rows = [{"mode": "recorded"}, {"mode": "live"}]
    monkeypatch.setattr(hosted.store, "stored_rows", lambda _folder: rows)
    surface = build_xstock_read_surface(runs[0])
    assert surface is not None and surface.deps.mode == "live"


# --- the gate: the bootcamp mount's, reused ------------------------------------------


def test_the_xstock_mount_is_gated_by_default() -> None:
    gated = default_gated_surfaces()
    assert XSTOCK_SURFACE in gated
    assert "bootcamp" in gated  # the same set the class mount is gated by


def _registry(surfaces: list[str]) -> tuple[InMemoryKeyRegistry, str]:
    registry = InMemoryKeyRegistry()
    key = mint_key()
    registry.store_key(
        key_hash=hash_key(key), account_id=ACCOUNT, label="judge", surfaces=surfaces
    )
    return registry, key


def _app(folder: Path, registry: InMemoryKeyRegistry) -> Any:
    return build_multi_surface_app(
        [(XSTOCK_SURFACE, _surface(folder))],
        allowed_hosts=["testserver"],
        mode="live",
        require_gecko_key=True,
        gated_surfaces=default_gated_surfaces(),
        key_registry=registry,
    )


def _headers(key: str | None) -> dict[str, str]:
    return {**_HEADERS, **({"Authorization": f"Bearer {key}"} if key else {})}


def _rpc(client: TestClient, key: str, method: str, params: dict[str, Any]) -> str:
    init = client.post(f"/{XSTOCK_SURFACE}/mcp", json=_INIT, headers=_headers(key))
    assert init.status_code == 200, init.text
    headers = {**_headers(key), "mcp-session-id": init.headers["mcp-session-id"]}
    client.post(
        f"/{XSTOCK_SURFACE}/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    response = client.post(
        f"/{XSTOCK_SURFACE}/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
        headers=headers,
    )
    return response.text


def _message(raw: str) -> dict[str, Any]:
    for line in raw.splitlines():
        if line.startswith("data:"):
            return dict(json.loads(line[len("data:") :].strip()))
    raise AssertionError(f"no SSE message: {raw[:400]}")


def test_no_key_is_a_401(runs: tuple[Path, list[str]]) -> None:
    registry, _key = _registry([XSTOCK_SURFACE])
    with TestClient(_app(runs[0], registry)) as client:
        resp = client.post(f"/{XSTOCK_SURFACE}/mcp", json=_INIT, headers=_HEADERS)
    assert resp.status_code == 401
    assert resp.json()["reason"] == "missing_token"


def test_a_key_for_another_surface_does_not_open_this_one(
    runs: tuple[Path, list[str]],
) -> None:
    registry, key = _registry(["bootcamp"])
    with TestClient(_app(runs[0], registry)) as client:
        resp = client.post(f"/{XSTOCK_SURFACE}/mcp", json=_INIT, headers=_headers(key))
    assert resp.status_code in (401, 403)
    assert key not in resp.text


def test_a_granted_key_reads_the_file(runs: tuple[Path, list[str]]) -> None:
    folder, ids = runs
    registry, key = _registry([XSTOCK_SURFACE])
    with TestClient(_app(folder, registry)) as client:
        raw = _rpc(
            client,
            key,
            "tools/call",
            {"name": dataset.FILE_TOOL, "arguments": {"mint": MINT}},
        )
    result = json.loads(_message(raw)["result"]["content"][0]["text"])
    assert result["run_id"] == ids[-1]
    assert result["rows"]


def test_a_granted_key_sees_only_the_two_reads(runs: tuple[Path, list[str]]) -> None:
    registry, key = _registry([XSTOCK_SURFACE])
    with TestClient(_app(runs[0], registry)) as client:
        raw = _rpc(client, key, "tools/list", {})
    names = {t["name"] for t in _message(raw)["result"]["tools"]}
    assert names == READ_TOOLS


# --- the image: the env, the extra and the snapshot agree ------------------------------

_REPO = Path(__file__).resolve().parents[1]

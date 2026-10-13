"""The dataset endpoint ``/d/<dataset_id>/mcp`` and its downloads, fully offline.

MemoryStore and the fake Mongo, in-memory key registries, the real HTTP stack in
process. No money, no model, no source, and no key leaves a test.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from gecko.anyquery.dataset_download import Download, dataset_download
from gecko.anyquery.dataset_hosted import DatasetSurface, dataset_mount
from gecko.anyquery.dataset_read import DatasetCaller
from gecko.anyquery.dataset_surface import call_dataset_tool
from gecko.anyquery.dataset_tools import ANSWER_CAP, DATASET_TOOLS, DEFAULT_PAGE
from gecko.anyquery.datasets import DatasetDeps, adopt_run, new_dataset
from gecko.anyquery.reader_keys import (
    InMemoryReaderKeyRegistry,
    mint_reader_key,
    resolve_dataset_access,
)
from gecko.anyquery.store import Store, StoreUnavailableError
from gecko.anyquery.surface import FORBIDDEN_ARGUMENTS, NO_ACCOUNT
from gecko.keyregistry import (
    GeckoKeyResolver,
    InMemoryKeyRegistry,
    RegistryAllowlist,
    hash_key,
    mint_key,
)
from tests.anyquery_dataset_fixtures import (
    ACCT,
    NOW,
    OTHER,
    R1,
    R2,
    STORES,
    SUBJECTS,
    fact,
    plan,
    run,
    seed,
)

LEADS = "lead-contacts"
EMPTY = "board-members"
BIG = "big-table"
PHONE = "+44 20 7946 0001"
ROOT = Path(__file__).resolve().parents[1]


class World:
    """ACCT owns lead-contacts (v1 from R1) and an empty dataset; OTHER owns its own
    lead-contacts. One owner key, one reader key on lead-contacts."""

    def __init__(self, make_store: Callable[[], Store]) -> None:
        self.store = make_store()
        seed(self.store)
        seed(self.store, customer=OTHER)
        for customer in (ACCT, OTHER):
            self.store.create_dataset(
                new_dataset(
                    plan(customer), dataset_id=LEADS, bundle=LEADS, mode="live", now=NOW
                )
            )
            adopt_run(DatasetDeps(store=self.store), customer, LEADS, R1)
        self.store.create_dataset(
            new_dataset(plan(), dataset_id=EMPTY, bundle=LEADS, mode="live", now=NOW)
        )
        self.keys = InMemoryKeyRegistry()
        self.owner_key = mint_key()
        self.keys.store_key(
            key_hash=hash_key(self.owner_key),
            account_id=ACCT,
            label="owner",
            surfaces=["anyquery"],
        )
        self.readers = InMemoryReaderKeyRegistry()
        self.reader_key, _ = mint_reader_key(
            self.readers, self.store, owner=ACCT, dataset_id=LEADS, label="analyst"
        )

    def caller(self, token: str | None, dataset_id: str = LEADS) -> DatasetCaller:
        access = (
            resolve_dataset_access(
                token,
                readers=self.readers,
                resolve_owner=GeckoKeyResolver(self.keys),
                owner_enabled=RegistryAllowlist(self.keys).is_enabled,
            )
            if token
            else None
        )
        return DatasetCaller(dataset_id, access)

    def call(
        self, token: str | None, name: str, dataset_id: str = LEADS, **args: Any
    ) -> dict[str, Any]:
        return call_dataset_tool(self.store, self.caller(token, dataset_id), name, args)


@pytest.fixture(params=sorted(STORES))
def world(request: pytest.FixtureRequest) -> World:
    return World(STORES[request.param])


def _phone_cell(answer: dict[str, Any]) -> dict[str, Any]:
    row = next(r for r in answer["rows"] if r["subject"] == SUBJECTS[0])
    return dict(row["cells"]["mobile_phone"])


# -- who may read what ----------------------------------------------------------------------


def test_no_key_is_no_account_with_the_way_in(world: World) -> None:
    out = world.call(None, "rows")
    assert out["code"] == NO_ACCOUNT and "gecko_rk_" in out["error"]
    assert world.call("gecko_sk_not-a-key", "schema")["code"] == NO_ACCOUNT


def test_a_missing_dataset_and_one_out_of_scope_answer_the_same(world: World) -> None:
    out_of_scope = world.call(world.reader_key, "schema", dataset_id=EMPTY)
    missing = world.call(world.reader_key, "schema", dataset_id="no-such-thing")
    owner_missing = world.call(world.owner_key, "schema", dataset_id="no-such-thing")
    assert out_of_scope == missing == owner_missing
    assert out_of_scope["code"] == "dataset_not_found"


def test_the_owner_never_reads_another_customers_dataset_of_the_same_name(
    world: World,
) -> None:
    mine = world.call(world.owner_key, "rows", reveal_pii=True)
    theirs = world.store.dataset_rows(OTHER, LEADS)
    assert len(mine["rows"]) == len(theirs) == 2
    # Both tables hold the same fake values; the scope proof is that the owner key
    # resolves to ACCT, and a reader key never reaches OTHER at all.
    assert world.caller(world.owner_key).access.customer == ACCT  # type: ignore[union-attr]


def test_a_reader_key_gets_masked_rows_even_when_it_asks(world: World) -> None:
    plain = world.call(world.reader_key, "rows")
    asked = world.call(world.reader_key, "rows", reveal_pii=True)
    for answer in (plain, asked):
        shown = _phone_cell(answer)["value_untrusted"]
        assert PHONE not in json.dumps(answer) and shown.endswith("0001")
        assert answer["pii_masked"] is True
    assert "notice" not in plain
    assert "cannot reveal" in asked["notice"]


def test_the_owner_reveals_only_when_asked(world: World) -> None:
    masked = world.call(world.owner_key, "rows")
    whole = world.call(world.owner_key, "rows", reveal_pii=True)
    assert PHONE not in json.dumps(masked) and masked["pii_masked"] is True
    assert _phone_cell(whole)["value_untrusted"] == PHONE
    assert whole["pii_masked"] is False and "notice" not in whole


def test_the_tool_list_offers_reveal_only_to_a_key_that_may(world: World) -> None:
    surface = DatasetSurface.__new__(DatasetSurface)
    reader = surface.list_tools(principal=world.caller(world.reader_key))
    owner = surface.list_tools(principal=world.caller(world.owner_key))
    nobody = surface.list_tools()
    assert [t["name"] for t in reader] == ["schema", "rows", "changes_since", "sources"]
    for tools, offered in ((reader, False), (nobody, False), (owner, True)):
        for tool in tools:
            if tool["name"] in ("rows", "changes_since"):
                assert ("reveal_pii" in tool["inputSchema"]["properties"]) is offered
    # The projection never edits the shared definitions.
    assert "reveal_pii" in DATASET_TOOLS[1]["inputSchema"]["properties"]


def test_no_tool_takes_a_forbidden_argument_and_all_are_read_only() -> None:
    for tool in DATASET_TOOLS:
        schema = tool["inputSchema"]
        assert schema["additionalProperties"] is False
        assert not set(schema["properties"]) & FORBIDDEN_ARGUMENTS, tool["name"]
        assert tool["annotations"]["readOnlyHint"] is True


def test_bad_and_forbidden_arguments_are_refused_not_ignored(world: World) -> None:
    assert world.call(world.owner_key, "rows", fields=["x"])["code"] == (
        "forbidden_argument"
    )
    out = world.call(world.owner_key, "rows", page=2)
    assert out["code"] == "bad_arguments" and "columns" in out["error"]
    out = world.call(world.owner_key, "rows", columns=["salary"])
    assert out["code"] == "unknown_column" and "job_title" in out["error"]
    assert world.call(world.owner_key, "rows", subject="nobody")["code"] == (
        "unknown_subject"
    )
    assert world.call(world.owner_key, "rows", limit=26)["code"] == "bad_arguments"


def test_an_owner_tool_asked_here_points_at_the_anyquery_mount(world: World) -> None:
    out = world.call(world.owner_key, "refresh_dataset", dataset_id=LEADS)
    assert out["code"] == "unknown_tool" and "/anyquery/mcp" in out["error"]


def test_a_store_error_is_store_unavailable_and_says_nothing_else(world: World) -> None:
    class Down:
        def dataset(self, *_a: Any, **_k: Any) -> None:
            raise StoreUnavailableError("mongodb+srv://user:secret@host")

    caller = world.caller(world.owner_key)
    out = call_dataset_tool(Down(), caller, "schema", {})  # type: ignore[arg-type]
    assert out["code"] == "store_unavailable" and "secret" not in out["error"]


# -- the four tools ---------------------------------------------------------------------------


def test_schema_shows_fields_subjects_and_versions_but_no_run_or_plan(
    world: World,
) -> None:
    out = world.call(world.reader_key, "schema")
    assert [f["name"] for f in out["fields"]] == [
        "work_email",
        "mobile_phone",
        "job_title",
        "current_company",
    ]
    assert {f["name"]: f["pii"] for f in out["fields"]}["mobile_phone"] is True
    assert out["subjects"] == list(SUBJECTS) and out["version"] == 1
    assert out["versions"][0]["counts"] == {"no data": 3, "repaired": 5}
    text = json.dumps(out)
    assert R1 not in text and "plan-a" not in text and "local://" not in text


def test_rows_cells_carry_status_source_slug_and_why_no_data(world: World) -> None:
    out = world.call(world.owner_key, "rows", columns=["work_email", "job_title"])
    assert out["version"] == 1 and out["next_cursor"] is None
    row = out["rows"][0]
    assert set(row["cells"]) == {"work_email", "job_title"}
    assert row["cells"]["job_title"] == {
        "value_untrusted": "analyst",
        "status": "repaired",
        "source": "fiber",
        "fetched_at": "2026-10-12T19:24:15Z",
    }
    # Keys with no value are left out; a cell with no data says why.
    assert row["cells"]["work_email"] == {"status": "no data", "note": "no source"}
    only = world.call(world.owner_key, "rows", status="no data")
    assert all(
        c["status"] == "no data" for r in only["rows"] for c in r["cells"].values()
    )


def test_an_empty_dataset_says_who_fills_it(world: World) -> None:
    out = world.call(world.owner_key, "rows", dataset_id=EMPTY)
    assert out["code"] == "no_version_yet" and "refresh_dataset" in out["error"]
    assert world.call(world.owner_key, "schema", dataset_id=EMPTY)["version"] is None


def _adopt_second_run(world: World) -> None:
    a = SUBJECTS[0]
    world.store.start_run(run(R2))
    world.store.write_facts(
        ACCT,
        [
            fact(R2, a, "work_email", "repaired", "ada@engine.example"),
            fact(R2, a, "mobile_phone", "no data", note="source down"),
            fact(R2, a, "job_title", "repaired", "lead analyst"),
            fact(R2, a, "current_company", "repaired", "engine co"),
        ],
    )
    adopt_run(DatasetDeps(store=world.store), ACCT, LEADS, R2)


def test_changes_since_names_each_kind_of_move(world: World) -> None:
    _adopt_second_run(world)
    out = world.call(world.owner_key, "changes_since", version=1)
    assert out["from_version"] == 1 and out["to_version"] == 2
    kinds = {(c["subject"], c["field"]): c["change"] for c in out["changes"]}
    a = SUBJECTS[0]
    assert kinds == {
        (a, "work_email"): "added",
        (a, "mobile_phone"): "went_stale",
        (a, "job_title"): "changed",
    }
    # Contact details stay masked in a diff too, for a reader whatever it asks.
    reader = world.call(world.reader_key, "changes_since", version=1, reveal_pii=True)
    assert "ada@engine.example" not in json.dumps(reader) and reader["pii_masked"]
    assert world.call(world.owner_key, "changes_since", version=3)["code"] == (
        "version_not_found"
    )
    assert world.call(world.owner_key, "changes_since")["code"] == "bad_arguments"
    same = world.call(world.owner_key, "changes_since", version=2)
    assert same["changes"] == [] and same["next_cursor"] is None


def test_a_cursor_from_an_older_version_is_version_changed(world: World) -> None:
    first = world.call(world.owner_key, "rows", limit=1)
    assert first["next_cursor"] is not None
    _adopt_second_run(world)
    out = world.call(world.owner_key, "rows", cursor=first["next_cursor"])
    assert out["code"] == "version_changed"
    assert world.call(world.owner_key, "rows", cursor="%%%")["code"] == "bad_arguments"


def test_sources_counts_what_each_source_filled_in_this_dataset(world: World) -> None:
    out = world.call(world.reader_key, "sources", column="job_title")
    assert [c["column"] for c in out["columns"]] == ["job_title"]
    assert out["columns"][0]["asked"] == 2
    assert world.call(world.reader_key, "sources", column="salary")["code"] == (
        "unknown_column"
    )
    assert world.call(world.reader_key, "sources", sources=["x"])["code"] == (
        "forbidden_argument"
    )


# -- the page and the cap -----------------------------------------------------------------------


def _big(world: World, *, text_len: int = 190) -> None:
    names = tuple(f"detail_{n}" for n in range(8))
    who = tuple(f"subject {n:02d}" for n in range(25))
    big = plan(plan_id="plan-big", subjects=who, fields=names)
    world.store.save_plan(big)
    world.store.start_run(run(R1.replace("867e15", "b16b16"), plan_id="plan-big"))
    run_id = R1.replace("867e15", "b16b16")
    world.store.write_facts(
        ACCT,
        [
            fact(
                run_id,
                s,
                f,
                "repaired",
                (f"{s} {f} " + "long upstream text " * 12)[:text_len],
                plan_id="plan-big",
            )
            for s in who
            for f in names
        ],
    )
    world.store.create_dataset(
        new_dataset(big, dataset_id=BIG, bundle=BIG, mode="live", now=NOW)
    )
    adopt_run(DatasetDeps(store=world.store), ACCT, BIG, run_id)


def _walk(world: World, **args: Any) -> list[dict[str, Any]]:
    pages = [world.call(world.owner_key, "rows", dataset_id=BIG, **args)]
    while pages[-1].get("next_cursor"):
        pages.append(
            world.call(
                world.owner_key,
                "rows",
                dataset_id=BIG,
                cursor=pages[-1]["next_cursor"],
                **args,
            )
        )
    return pages


def test_rows_page_ten_by_default_and_walk_every_subject_once(world: World) -> None:
    _big(world, text_len=40)
    pages = _walk(world)
    assert len(pages[0]["rows"]) == DEFAULT_PAGE
    seen = [r["subject"] for p in pages for r in p["rows"]]
    assert seen == sorted(seen) and len(seen) == len(set(seen)) == 25


def test_no_answer_passes_the_character_cap_even_at_the_largest_page(
    world: World,
) -> None:
    _big(world)
    pages = _walk(world, limit=25)
    # 25 rows of 8 long cells do not fit: the cap ends the page early, with a cursor.
    assert len(pages[0]["rows"]) < 25 and pages[0]["next_cursor"]
    for page in pages:
        assert len(json.dumps(page, default=str)) <= ANSWER_CAP
    assert sum(len(p["rows"]) for p in pages) == 25


# -- downloads ------------------------------------------------------------------------------------


def _download(world: World, token: str | None, fmt: Any, reveal: bool) -> Any:
    return dataset_download(world.store, world.caller(token), fmt, reveal=reveal)


def _parquet_values(body: bytes) -> dict[tuple[str, str], Any]:
    pq = pytest.importorskip("pyarrow.parquet")
    table = pq.read_table(io.BytesIO(body))
    return {(r["subject"], r["field"]): r["value"] for r in table.to_pylist()}


def test_a_reader_downloads_masked_data_whatever_it_asks(world: World) -> None:
    for fmt in ("json", "parquet"):
        got = _download(world, world.reader_key, fmt, reveal=True)
        assert isinstance(got, Download)
        assert PHONE.encode() not in got.body
    body = json.loads(_download(world, world.reader_key, "json", True).body)
    assert body["pii_masked"] is True and body["version"] == 1
    assert len(body["rows"]) == 8  # long: one row per cell


def test_only_the_owners_key_gets_the_unmasked_parquet(world: World) -> None:
    masked = _download(world, world.owner_key, "parquet", reveal=False)
    whole = _download(world, world.owner_key, "parquet", reveal=True)
    assert isinstance(masked, Download) and isinstance(whole, Download)
    key = (SUBJECTS[0], "mobile_phone")
    assert _parquet_values(masked.body)[key] != PHONE
    assert _parquet_values(whole.body)[key] == PHONE
    assert whole.filename == f"{LEADS}-v1.parquet"


def test_a_download_refuses_like_the_tools(world: World) -> None:
    assert _download(world, None, "json", False)["code"] == NO_ACCOUNT
    other = dataset_download(
        world.store, world.caller(world.reader_key, EMPTY), "json", reveal=False
    )
    assert other["code"] == "dataset_not_found"  # type: ignore[index]


# -- nothing here can pay ---------------------------------------------------------------------------


def test_the_endpoint_never_imports_anything_that_pays() -> None:
    probe = (
        "import json, sys\n"
        "import gecko.anyquery.dataset_hosted, gecko.anyquery.dataset_surface\n"
        "import gecko.anyquery.dataset_download\n"
        "print(json.dumps(sorted(k for k in sys.modules if k.startswith('gecko'))))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    loaded = set(json.loads(done.stdout.strip().splitlines()[-1]))
    forbidden = {
        "gecko.anyquery.billing",
        "gecko.anyquery.solana_billing",
        "gecko.anyquery.payment_tx",
        "gecko.anyquery.settlement",
        "gecko.anyquery.settlement_tx",
        "gecko.anyquery.run",
        "gecko.anyquery.execute",
        "gecko.anyquery.service",
        "gecko.anyquery.hosted",
        "gecko.anyquery.planner",
        "gecko.anyquery.model_planner",
        "gecko.anyquery.upstream_x402",
        "gecko.anyquery.upstream_spend",
        "gecko.anyquery.dataset_owner",
    }
    assert not loaded & forbidden, sorted(loaded & forbidden)
    assert "gecko.anyquery.dataset_hosted" in loaded


# -- through the real HTTP stack, in process ---------------------------------------------------

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


def _app(world: World) -> Any:
    from gecko.http_server import build_multi_surface_app
    from gecko.serve_mcp import default_gated_surfaces

    mount = dataset_mount(world.store, keys=world.keys, readers=world.readers)
    assert mount is not None
    return build_multi_surface_app(
        [mount],
        allowed_hosts=["testserver"],
        mode="live",
        require_gecko_key=True,
        gated_surfaces=default_gated_surfaces(),
        key_registry=world.keys,
    )


def _rpc(
    client: Any, key: str | None, method: str, params: dict[str, Any], ds: str = LEADS
) -> dict[str, Any]:
    url = f"/d/{ds}/mcp"
    auth = dict(_HEADERS)
    if key:
        auth["Authorization"] = f"Bearer {key}"
    init = client.post(url, json=_INIT, headers=auth)
    assert init.status_code == 200, init.text
    headers = {**auth, "mcp-session-id": init.headers["mcp-session-id"]}
    client.post(
        url,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    raw = client.post(
        url,
        json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
        headers=headers,
    ).text
    if key:
        assert key not in raw
    for line in raw.splitlines():
        if line.startswith("data:"):
            return dict(json.loads(line[len("data:") :].strip()))
    raise AssertionError(f"no SSE message: {raw[:400]}")


def _tool(client: Any, key: str | None, name: str, args: dict, ds: str = LEADS):
    message = _rpc(client, key, "tools/call", {"name": name, "arguments": args}, ds)
    return json.loads(message["result"]["content"][0]["text"])


@pytest.mark.usefixtures("_gate_env")
@pytest.mark.parametrize("store_kind", sorted(STORES))
def test_the_endpoint_over_http_reads_as_the_key_proves(store_kind: str) -> None:
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient

    world = World(STORES[store_kind])
    with TestClient(_app(world)) as client:
        listed = _rpc(client, world.reader_key, "tools/list", {})["result"]["tools"]
        rows_tool = next(t for t in listed if t["name"] == "rows")
        assert "reveal_pii" not in rows_tool["inputSchema"]["properties"]
        owner_list = _rpc(client, world.owner_key, "tools/list", {})["result"]["tools"]
        assert (
            "reveal_pii"
            in next(t for t in owner_list if t["name"] == "rows")["inputSchema"][
                "properties"
            ]
        )

        reader_rows = _tool(client, world.reader_key, "rows", {"reveal_pii": True})
        assert PHONE not in json.dumps(reader_rows) and reader_rows["pii_masked"]
        owner_rows = _tool(client, world.owner_key, "rows", {"reveal_pii": True})
        assert _phone_cell(owner_rows)["value_untrusted"] == PHONE
        assert _tool(client, None, "schema", {})["code"] == NO_ACCOUNT
        assert _tool(client, world.reader_key, "schema", {}, ds=EMPTY)["code"] == (
            "dataset_not_found"
        )
        # Hidden, not a -32602: the pointer to the owner's mount is the answer.
        pointer = _tool(client, world.owner_key, "refresh_dataset", {})
        assert pointer["code"] == "unknown_tool" and "/anyquery/mcp" in pointer["error"]

        auth = {"Authorization": f"Bearer {world.reader_key}"}
        got = client.get(f"/d/{LEADS}.json?reveal_pii=true", headers=auth)
        assert got.status_code == 200 and PHONE not in got.text
        assert got.headers["cache-control"] == "no-store"
        owner = {"Authorization": f"Bearer {world.owner_key}"}
        whole = client.get(f"/d/{LEADS}.parquet?reveal_pii=true", headers=owner)
        assert whole.status_code == 200
        assert _parquet_values(whole.content)[(SUBJECTS[0], "mobile_phone")] == PHONE
        assert client.get(f"/d/{LEADS}.json").status_code == 401
        assert client.get(f"/d/{EMPTY}.json", headers=auth).status_code == 404
        assert client.get("/d/lead-contacts/other").status_code == 404
        assert client.post(f"/d/{LEADS}.json", headers=auth).status_code == 405


@pytest.mark.usefixtures("_gate_env")
@pytest.mark.parametrize("store_kind", sorted(STORES))
@pytest.mark.parametrize("last_lister", ["owner", "reader", "nobody"])
def test_a_tool_list_for_one_key_never_decides_another_keys_call(
    store_kind: str, last_lister: str
) -> None:
    # The MCP server keeps ONE tool cache for every session. The list is projected per
    # key, so whichever key listed last must not decide what the next key may send.
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient

    world = World(STORES[store_kind])
    keys = {"owner": world.owner_key, "reader": world.reader_key, "nobody": None}
    with TestClient(_app(world)) as client:
        for who in [k for k in keys if k != last_lister] + [last_lister]:
            _rpc(client, keys[who], "tools/list", {})
        owner_rows = _tool(client, world.owner_key, "rows", {"reveal_pii": True})
        assert _phone_cell(owner_rows)["value_untrusted"] == PHONE
        owner_diff = _tool(
            client, world.owner_key, "changes_since", {"version": 1, "reveal_pii": True}
        )
        assert owner_diff["changes"] == []
        reader_rows = _tool(client, world.reader_key, "rows", {"reveal_pii": True})
        assert PHONE not in json.dumps(reader_rows) and reader_rows["pii_masked"]
        assert "cannot reveal" in reader_rows["notice"]
        # The surface still refuses what no schema takes.
        bad = _tool(client, world.reader_key, "rows", {"page": 2})
        assert bad["code"] == "bad_arguments"


def test_no_mount_without_a_store_or_a_key_registry() -> None:
    readers = InMemoryReaderKeyRegistry()
    keys = InMemoryKeyRegistry()
    assert dataset_mount(None, keys=keys, readers=readers) is None
    store = STORES["memory"]()
    assert dataset_mount(store, keys=None, readers=readers) is None
    assert dataset_mount(store, keys=keys, readers=None) is None
    mounted = dataset_mount(store, keys=keys, readers=readers)
    assert mounted is not None and mounted[0] == "d"

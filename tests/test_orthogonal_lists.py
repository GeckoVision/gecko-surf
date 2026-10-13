"""The Orthogonal commenter-list job, offline. No wire, no key, no money.

Every test runs the real job against an injected transport: either the hand-written
``RecordedOrthogonal`` (which bills like the real thing) or a tiny scripted fake.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from gecko.caller import PreparedRequest
from gecko.orthogonal import CommenterListSurface, deps_for, plan_list, run_list
from gecko.orthogonal.api import OrthogonalApi
from gecko.orthogonal.errors import (
    NO_HANDLE_COLUMN_NOTICE,
    BadPostUrlError,
    NoKeyError,
    NotAllowedError,
    NotConfirmedError,
    OutOfCreditsError,
    SheetOutsideFolderError,
    SpendCapError,
    UpstreamError,
    redact,
)
from gecko.orthogonal.extract import Commenter, dollars_to_cents, page_facts
from gecko.orthogonal.fixture import SAMPLE_SHEET_CSV, RecordedOrthogonal
from gecko.orthogonal.match import classify
from gecko.orthogonal.paginate import fetch_commenters
from gecko.orthogonal.post import canonical_post_url, post_id_from_url
from gecko.orthogonal.record import LIST_SUFFIX, prune_outputs
from gecko.orthogonal.session import orthogonal_session
from gecko.orthogonal.sheet import read_customers
from gecko.orthogonal.spec import (
    BALANCE_OP,
    DETAILS_OP,
    RUN_OP,
    check_allowed,
)
from gecko.orthogonal.spend import FileLedger, MemoryLedger, SpendGuard
from gecko.toolerror import is_upstream_failure

POST = "https://www.instagram.com/p/MadeUp123/"
FAKE_KEY = "orth_live_FAKEFAKEFAKE0123456789"


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    lists = tmp_path / "gecko-lists"
    lists.mkdir()
    (lists / "clientes.csv").write_text(SAMPLE_SHEET_CSV, encoding="utf-8")
    return lists


class FakeKeyring:
    """A credential backend holding one made-up key."""

    name = "keyring"

    def __init__(self, value: str | None = FAKE_KEY) -> None:
        self.value = value

    def available(self) -> bool:
        return True

    def get(self, ref: Any) -> str | None:
        return self.value if ref.api == "orthogonal" else None


def _run_page(
    request_id: str, charged: int, comments: list[Any], cursor: str | None
) -> dict[str, Any]:
    return {
        "success": True,
        "priceCents": charged,
        "quotedPriceCents": 2,
        "requestId": request_id,
        "billing": {"requestId": request_id, "chargedPriceCents": charged},
        "data": {"comments": comments, "cursor": cursor},
    }


class Scripted:
    """Answers ``/v1/run`` from a list of replies; balance and details as fixed."""

    def __init__(self, runs: list[tuple[int, Any]], balance: str = "$1.00") -> None:
        self.runs = list(runs)
        self.balance = balance
        self.requests: list[PreparedRequest] = []

    def __call__(self, req: PreparedRequest) -> tuple[int, Any]:
        self.requests.append(req)
        if req.url.endswith("/v1/credits/balance"):
            return 200, {"balance": self.balance}
        if req.url.endswith("/v1/details"):
            return 200, {"price": "$0.02"}
        return self.runs.pop(0) if self.runs else (200, _run_page("r", 0, [], None))

    def run_requests(self) -> list[PreparedRequest]:
        return [r for r in self.requests if r.url.endswith("/v1/run")]


# --- the recorded end-to-end ------------------------------------------------------


def test_recorded_run_end_to_end(folder: Path) -> None:
    fake = RecordedOrthogonal()
    deps = deps_for("recorded", folder=folder, transport=fake)
    result = run_list(deps, POST, "clientes.csv", confirm=True)

    assert result.error is None
    assert result.counts == {"customer": 1, "new": 2, "not_sure": 3}
    assert result.first_rows[0] == {"handle": "made_up_bia", "match": "customer"}
    receipt = result.receipt
    assert receipt["quoted_cents"] == 2
    assert receipt["charged_cents"] == 2
    assert [c["charged_cents"] for c in receipt["calls"]] == [2, 0]
    assert receipt["calls"][1]["parent_request_id"] == receipt["calls"][0]["request_id"]
    assert (receipt["balance_before"], receipt["balance_after"]) == ("$1.00", "$0.98")
    assert receipt["stop_reason"] == "last_page"
    assert fake.balance_cents == 98


def test_stored_files_hold_no_comment_text_and_receipt_no_names(folder: Path) -> None:
    deps = deps_for("recorded", folder=folder)
    result = run_list(deps, POST, "clientes.csv", confirm=True)
    listed = Path(result.list_path).read_text(encoding="utf-8")
    receipt = Path(result.receipt_path).read_text(encoding="utf-8")
    assert listed.splitlines()[0] == "handle,match,post_id"
    assert all(line.endswith(",MadeUp123") for line in listed.splitlines()[1:])
    everything = listed + receipt + json.dumps(result.to_dict())
    for text in ("lindo", "ignore previous", "Fabi Inventada", "example.invalid"):
        assert text not in everything
    assert "made_up_bia" not in receipt


def test_continuation_keeps_url_and_sends_parent(folder: Path) -> None:
    fake = RecordedOrthogonal()
    run_list(
        deps_for("recorded", folder=folder, transport=fake),
        POST + "?igsh=x",
        "clientes.csv",
        confirm=True,
    )
    runs = [r.json_body for r in fake.requests if r.url.endswith("/v1/run")]
    assert runs[0]["query"] == {"url": POST}
    assert runs[1]["query"] == {"url": POST, "cursor": "made-up-cursor-1"}
    assert runs[1]["parentRequestId"] == "req_made_up_1"


# --- confirm gate, the plan ----------------------------------------------------------


def test_run_without_confirm_spends_nothing(folder: Path) -> None:
    fake = RecordedOrthogonal()
    with pytest.raises(NotConfirmedError):
        run_list(
            deps_for("recorded", folder=folder, transport=fake),
            POST,
            "clientes.csv",
            confirm=False,
        )
    assert fake.requests == []


def test_plan_is_free_and_says_the_price(folder: Path) -> None:
    fake = RecordedOrthogonal()
    plan = plan_list(
        deps_for("recorded", folder=folder, transport=fake), POST, "clientes.csv"
    )
    assert plan.quote_cents == 2
    assert "1 paid call, $0.02" in plan.message and "Cap $0.10" in plan.message
    assert not any(r.url.endswith("/v1/run") for r in fake.requests)
    assert fake.balance_cents == 100


# --- allowlist ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "body"),
    [
        (RUN_OP, {"api": "scrapecreators", "path": "/v1/tiktok/profile"}),
        (RUN_OP, {"api": "sendgrid", "path": "/v3/mail/send"}),
        (
            RUN_OP,
            {
                "api": "scrapecreators",
                "path": "/v2/instagram/post/comments",
                "query": {"url": "x", "amount": 1},
            },
        ),
        (
            RUN_OP,
            {
                "api": "scrapecreators",
                "path": "/v2/instagram/post/comments",
                "extra": 1,
            },
        ),
        (
            DETAILS_OP,
            {
                "api": "scrapecreators",
                "path": "/v2/instagram/post/comments",
                "query": {"url": "x"},
            },
        ),
        (BALANCE_OP, {"api": "scrapecreators"}),
        ("orthogonal_other", {}),
    ],
)
def test_allowlist_refuses(op: str, body: dict[str, Any]) -> None:
    with pytest.raises(NotAllowedError):
        check_allowed(op, body)


def test_refused_call_never_reaches_the_transport() -> None:
    fake = Scripted([])
    api = OrthogonalApi(transport=fake, session=orthogonal_session(FakeKeyring()))
    with pytest.raises(NotAllowedError):
        api.call(RUN_OP, {"api": "scrapecreators", "path": "/v1/tiktok/profile"})
    assert fake.requests == []


# --- the key: keyring only, pinned host, never in an error -------------------------


def test_live_session_sends_bearer_to_the_pinned_host_only() -> None:
    fake = Scripted([])
    api = OrthogonalApi(transport=fake, session=orthogonal_session(FakeKeyring()))
    api.call(BALANCE_OP)
    req = fake.requests[0]
    assert req.url == "https://api.orthogonal.com/v1/credits/balance"
    assert req.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert FAKE_KEY not in repr(orthogonal_session(FakeKeyring()))


def test_no_key_message(folder: Path) -> None:
    fake = Scripted([])
    deps = deps_for(
        "live",
        folder=folder,
        transport=fake,
        session=orthogonal_session(FakeKeyring(None)),
        ledger=MemoryLedger(),
    )
    with pytest.raises(NoKeyError) as caught:
        plan_list(deps, POST, "clientes.csv")
    assert "gecko auth set orthogonal" in caught.value.message
    assert fake.requests == []


def test_default_session_is_keyring_only() -> None:
    session = orthogonal_session()
    assert [b.name for b in session.resolver.backends] == ["keyring"]
    assert session.scheme == "bearer"


def test_errors_redact_the_key() -> None:
    assert FAKE_KEY not in redact(
        f"boom {FAKE_KEY} and Authorization: Bearer {FAKE_KEY}"
    )
    assert FAKE_KEY not in str(UpstreamError(f"upstream said {FAKE_KEY}"))


def test_rejected_key_is_not_reported_as_missing() -> None:
    def deny(req: PreparedRequest) -> tuple[int, Any]:
        return 401, {"error": f"bad key {FAKE_KEY}"}

    api = OrthogonalApi(transport=deny, session=orthogonal_session(FakeKeyring()))
    with pytest.raises(UpstreamError) as caught:
        api.call(BALANCE_OP)
    assert "rejected" in caught.value.message and FAKE_KEY not in caught.value.message


# --- credits ------------------------------------------------------------------------


def test_low_balance_refuses_before_any_paid_call(folder: Path) -> None:
    fake = Scripted([], balance="$0.01")
    with pytest.raises(OutOfCreditsError) as caught:
        run_list(
            deps_for("recorded", folder=folder, transport=fake),
            POST,
            "clientes.csv",
            confirm=True,
        )
    assert (
        "$0.01 left" in caught.value.message
        and "Nothing was spent" in caught.value.message
    )
    assert fake.run_requests() == []


def test_402_is_the_out_of_credits_message(folder: Path) -> None:
    fake = Scripted([(402, {})])
    with pytest.raises(OutOfCreditsError):
        run_list(
            deps_for("recorded", folder=folder, transport=fake),
            POST,
            "clientes.csv",
            confirm=True,
        )


# --- pagination ---------------------------------------------------------------------


def _guard(cap: int = 10) -> SpendGuard:
    return SpendGuard(MemoryLedger(), per_run_cap=cap)


def _api(fake: Any) -> OrthogonalApi:
    return OrthogonalApi(transport=fake, session=orthogonal_session(FakeKeyring()))


def test_repeated_cursor_stops() -> None:
    page = [{"user": {"username": "made_up_x"}}]
    fake = Scripted(
        [
            (200, _run_page("r1", 2, page, "c1")),
            (200, _run_page("r2", 0, page, "c2")),
            (200, _run_page("r3", 0, page, "c1")),
            (200, _run_page("r4", 0, page, "c9")),
        ]
    )
    result = fetch_commenters(_api(fake), POST, _guard(), 2)
    assert result.stop_reason == "repeated_cursor"
    assert len(fake.run_requests()) == 3


def test_page_cap_is_ten() -> None:
    page = [{"user": {"username": "made_up_x"}}]
    fake = Scripted(
        [
            (200, _run_page(f"r{i}", 2 if i == 0 else 0, page, f"c{i}"))
            for i in range(30)
        ]
    )
    result = fetch_commenters(_api(fake), POST, _guard(), 2)
    assert result.stop_reason == "page_cap"
    assert len(fake.run_requests()) == 10


def test_charged_continuation_aborts_and_keeps_the_charge(folder: Path) -> None:
    page = [{"user": {"username": "made_up_x"}}]
    fake = Scripted(
        [
            (200, _run_page("r1", 2, page, "c1")),
            (200, _run_page("r2", 2, page, "c2")),
            (200, _run_page("r3", 0, page, None)),
        ]
    )
    result = run_list(
        deps_for("recorded", folder=folder, transport=fake),
        POST,
        "clientes.csv",
        confirm=True,
    )
    assert result.code == "continuation_charged"
    assert result.receipt["charged_cents"] == 4
    assert len(fake.run_requests()) == 2
    assert is_upstream_failure(result.to_dict())


def test_fixture_bills_a_continuation_without_parent() -> None:
    """The fixture is a falsifier: drop parentRequestId and the job must catch the bill."""
    fake = RecordedOrthogonal()

    class DropParent:
        def __call__(self, req: PreparedRequest) -> tuple[int, Any]:
            if isinstance(req.json_body, dict):
                req.json_body.pop("parentRequestId", None)
            return fake(req)

    result = fetch_commenters(_api(DropParent()), POST, _guard(), 2)
    assert result.stop_reason == "continuation_charged"


def test_per_run_cap_stops_before_the_call() -> None:
    page = [{"user": {"username": "made_up_x"}}]
    fake = Scripted(
        [(200, _run_page(f"r{i}", 2 if i == 0 else 0, page, f"c{i}")) for i in range(5)]
    )
    result = fetch_commenters(_api(fake), POST, _guard(cap=3), 2)
    # 2 charged; one more full-price bound (2 + 2 = 4) would cross 3, so page 2 is never sent.
    assert result.stop_reason == "spend_cap"
    assert len(fake.run_requests()) == 1


def test_unknown_shape_is_not_reported_as_private(folder: Path) -> None:
    fake = Scripted(
        [(200, {"priceCents": 2, "requestId": "r1", "data": {"items": []}})]
    )
    result = run_list(
        deps_for("recorded", folder=folder, transport=fake),
        POST,
        "clientes.csv",
        confirm=True,
    )
    assert result.code == "upstream_error" and "items" in (result.error or "")
    assert result.receipt["charged_cents"] == 2
    assert not any("private" in n for n in result.notices)


def test_zero_comments_says_private_and_the_charge(folder: Path) -> None:
    fake = Scripted([(200, _run_page("r1", 2, [], None))])
    result = run_list(
        deps_for("recorded", folder=folder, transport=fake),
        POST,
        "clientes.csv",
        confirm=True,
    )
    assert (
        "This post returned 0 comments. It may be private. You were charged $0.02."
        in result.notices
    )


def test_missing_price_counts_the_quote() -> None:
    reply = _run_page("r1", 0, [], None)
    del reply["priceCents"], reply["billing"]["chargedPriceCents"]
    result = fetch_commenters(_api(Scripted([(200, reply)])), POST, _guard(), 2)
    assert result.calls[0].charged_cents == 2 and result.calls[0].charge_assumed


# --- the ledger ---------------------------------------------------------------------


def test_file_ledger_persists_and_caps(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path / "ledger.json")
    SpendGuard(ledger).record(98)
    assert FileLedger(tmp_path / "ledger.json").total_cents() == 98
    with pytest.raises(SpendCapError):
        SpendGuard(FileLedger(tmp_path / "ledger.json")).check_before(3)


def test_corrupt_ledger_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(SpendCapError):
        FileLedger(path).total_cents()


def test_live_run_writes_the_file_ledger(folder: Path, tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path / "ledger.json")
    deps = deps_for(
        "live",
        folder=folder,
        transport=RecordedOrthogonal(),
        session=orthogonal_session(FakeKeyring()),
        ledger=ledger,
    )
    run_list(deps, POST, "clientes.csv", confirm=True)
    assert ledger.total_cents() == 2


# --- the sheet ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["../clientes.csv", "/etc/passwd", "sub/../../x.csv", ""]
)
def test_sheet_outside_folder_is_refused(folder: Path, name: str) -> None:
    with pytest.raises(SheetOutsideFolderError) as caught:
        read_customers(name, folder)
    assert "I can only read files in" in caught.value.message


def test_symlink_out_of_folder_is_refused(folder: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.csv"
    outside.write_text("instagram\nx\n", encoding="utf-8")
    os.symlink(outside, folder / "link.csv")
    with pytest.raises(SheetOutsideFolderError):
        read_customers("link.csv", folder)


def test_no_handle_column_marks_everyone_not_sure(folder: Path) -> None:
    (folder / "nomes.csv").write_text(
        "nome;email\nAna;a@example.invalid\n", encoding="utf-8"
    )
    result = run_list(
        deps_for("recorded", folder=folder), POST, "nomes.csv", confirm=True
    )
    assert NO_HANDLE_COLUMN_NOTICE in result.notices
    assert result.counts["customer"] == 0 and result.counts["new"] == 0


def test_handles_normalize_from_urls_and_at_signs(folder: Path) -> None:
    (folder / "h.csv").write_text(
        "Instagram Handle\n@Ana.Silva\nhttps://www.instagram.com/bob_x/?hl=pt\n",
        encoding="utf-8",
    )
    assert read_customers("h.csv", folder).handles == {"ana.silva", "bob_x"}


# --- the matcher --------------------------------------------------------------------


def test_matcher_classes(folder: Path) -> None:
    sheet = read_customers("clientes.csv", folder)
    assert classify(Commenter("Made_Up_Bia"), sheet)[1] == "customer"
    assert classify(Commenter("made_up_bio"), sheet)[1] == "not_sure"
    assert classify(Commenter("madeup.caio"), sheet)[1] == "not_sure"
    assert classify(Commenter("x", "Fabi  Inventada"), sheet)[1] == "not_sure"
    assert classify(Commenter(None), sheet) == ("(no handle)", "not_sure")
    assert classify(Commenter("someone_else"), sheet)[1] == "new"


def test_page_facts_reads_the_measured_shape() -> None:
    facts = page_facts(
        {
            "priceCents": 0,
            "quotedPriceCents": 2,
            "billing": {
                "requestId": "b1",
                "parentRequestId": "p",
                "continuation": True,
            },
            "data": {
                "comments": [{"user": {"handle": "h1"}, "text": "t"}],
                "cursor": "c",
            },
        }
    )
    assert facts.request_id == "b1" and facts.charged_cents == 0
    assert facts.commenters == (Commenter("h1"),) and facts.cursor == "c"


def test_dollars_to_cents() -> None:
    assert dollars_to_cents("$1.00") == 100
    assert dollars_to_cents("$0.5") == 50
    assert dollars_to_cents("lots") is None


# --- post url, retention, the MCP surface ------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://www.instagram.com/p/MadeUp123/",
        "https://evil.example/p/MadeUp123/",
        "https://www.instagram.com/made_up_bia/",
        "https://user@www.instagram.com/p/MadeUp123/",
        "javascript:alert(1)",
    ],
)
def test_bad_post_urls(url: str) -> None:
    with pytest.raises(BadPostUrlError):
        post_id_from_url(url)


def test_reel_url_and_canonical_form() -> None:
    assert post_id_from_url("https://instagram.com/reel/Ab_c-12/") == "Ab_c-12"
    assert canonical_post_url("https://instagram.com/reel/Ab_c-12?igsh=z") == (
        "https://www.instagram.com/reel/Ab_c-12/"
    )


def test_prune_removes_only_our_old_files(folder: Path) -> None:
    old = folder / f"p-20200101-000000{LIST_SUFFIX}"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (0, 0))
    assert prune_outputs(folder, now=40 * 86400.0) == 1
    assert not old.exists() and (folder / "clientes.csv").exists()


def test_surface_tools_and_errors(folder: Path) -> None:
    surface = CommenterListSurface(deps_for("recorded", folder=folder))
    assert [t["name"] for t in surface.list_tools()] == [
        "plan_commenter_list",
        "build_commenter_list",
    ]
    refused = surface.call_tool(
        "build_commenter_list", {"post_url": POST, "customers": "clientes.csv"}
    )
    assert refused["code"] == "not_confirmed" and is_upstream_failure(refused)
    outside = surface.call_tool(
        "plan_commenter_list", {"post_url": POST, "customers": "../x.csv"}
    )
    assert outside["code"] == "sheet_outside_folder"
    done = surface.call_tool(
        "build_commenter_list",
        {"post_url": POST, "customers": "clientes.csv", "confirm": True},
    )
    assert done["counts"]["customer"] == 1 and not is_upstream_failure(done)
    assert "RECORDED" in surface.instructions


def test_defaults_are_the_founder_caps(folder: Path) -> None:
    live = deps_for("live", folder=folder, session=orthogonal_session(FakeKeyring()))
    assert (live.per_run_cap, live.total_cap, live.max_pages) == (10, 100, 10)
    assert isinstance(live.ledger, FileLedger)
    assert isinstance(deps_for("recorded", folder=folder).ledger, MemoryLedger)


def test_serve_lists_refuses_http_and_a_spec(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gecko import serve

    assert serve.main(["--lists"]) == 2
    assert "stdio only" in capsys.readouterr().err
    assert serve.main(["--lists", "--stdio", "spec.json"]) == 2

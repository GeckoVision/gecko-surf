"""GAP D — a Gecko SELF-CHECK is not an agent call, so it must not share the denominator.

``verify-docs --live`` walks every operation on a surface and calls it with arguments
Gecko itself synthesized (``validator.example_args``). When a required path param has no
spec example and no DECLARED value domain, the placeholder is invented — so the 404 that
comes back says "the id I made up is not a real entity", not "this endpoint is broken",
and it says nothing at all about whether an AGENT can call this API first try.

That row is nevertheless ``mode="live"`` → ``source="observed"``, which is the exact
label ``telemetry.aggregate`` uses as the published first-call-correct DENOMINATOR. So a
self-check against a healthy API drives the published rate toward zero with calls no agent
ever made.

The fix is PATH segregation (``corpus.selfcheck_sibling``), the same posture as
``synthetic_sibling`` / ``simulated_sibling``: a reader of the main corpus that has never
heard of the self-check tier can never see a self-check row — it fails CLOSED. An in-band
tag would fail OPEN (every reader must remember to filter it out; forgetting = a corrupt
metric). The rows stay ``observed`` and stay REHYDRATABLE, because they are honest wire
evidence — they just live in their own file and their own denominator.

Falsifier-first, offline, $0: one injected transport is the whole fake.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gecko import corpus, verify
from gecko.caller import PreparedRequest
from gecko.client import AgentApiClient

# A spec whose only op takes a REQUIRED path param with no example and no declared value
# domain — so ``example_args`` must invent the id, and the live 404 is un-attributable.
SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/widgets/{widget_id}": {
            "get": {
                "operationId": "getWidget",
                "summary": "fetch one widget by id",
                "parameters": [
                    {
                        "name": "widget_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


def _not_found(_req: PreparedRequest) -> tuple[int, Any]:
    """The upstream a healthy API gives for an invented id."""
    return 404, {"error": "no such widget"}


def _client(corpus_path: Path) -> AgentApiClient:
    """A client with corpus capture ON — i.e. a consumer that opted into the corpus and
    then ran a self-check on the same surface."""
    return AgentApiClient(
        SPEC,
        base_url="https://api.example.com",
        session=None,
        live_transport=_not_found,
        corpus_path=corpus_path,
        surface_id="example.com",
    )


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- the bug --------------------------------------------------------------------------


def test_live_verify_docs_writes_no_row_into_the_main_corpus(tmp_path: Path) -> None:
    """The reproduction: a self-check 404 on an INVENTED id must not land in the main
    corpus, because that file is the observed first-call-correct denominator."""
    corpus_path = tmp_path / "corpus.jsonl"
    client = _client(corpus_path)

    report = verify.verify_docs(client, mode="live")

    # The verdict itself is unchanged by this fix: an invented arg downgrades the 404 to
    # UNVERIFIED (we never publish "no such endpoint" off our own made-up id).
    assert report["report"]["getWidget"]["status"] == "UNVERIFIED"

    assert _rows(corpus_path) == [], (
        "a Gecko self-check landed in the main corpus — it would count as an agent call "
        "in the published observed first-call-correct denominator"
    )


def test_live_verify_docs_row_lands_in_the_selfcheck_sibling(tmp_path: Path) -> None:
    """Segregated, not dropped: the evidence is kept, in its own file and denominator."""
    corpus_path = tmp_path / "corpus.jsonl"
    client = _client(corpus_path)

    verify.verify_docs(client, mode="live")

    rows = _rows(corpus.selfcheck_sibling(corpus_path))
    assert len(rows) == 1, "the self-check outcome must still be recorded, elsewhere"
    row = rows[0]
    assert row["operation_id"] == "getWidget"
    assert row["status"] == 404
    # Still honest wire evidence — the row is NOT relabelled to hide it from a reader.
    assert row["source"] == "observed"
    assert row["mode"] == "live"


def test_selfcheck_rows_stay_rehydratable_and_reusable_across_runs(
    tmp_path: Path,
) -> None:
    """Operating principle #2 — a stored corpus must tolerate cross-request reuse.

    The self-check file is a permanent, re-readable corpus: a LATER, unrelated run loads
    it through the same ``load_observed_corpus`` seam (no run/session id gates it) and gets
    real wire outcomes back. Segregation must not make the evidence unreachable."""
    corpus_path = tmp_path / "corpus.jsonl"
    verify.verify_docs(_client(corpus_path), mode="live")

    reloaded = verify.load_observed_corpus(corpus.selfcheck_sibling(corpus_path))

    assert set(reloaded) == {"getWidget"}
    assert reloaded["getWidget"].status == 404
    assert reloaded["getWidget"].source == "observed"


def test_repeated_verify_runs_append_to_one_stable_selfcheck_file(
    tmp_path: Path,
) -> None:
    """No per-run scoping: two runs accumulate in the SAME file (the trap this lane is
    told to avoid is a corpus that silently disappears behind a request id)."""
    corpus_path = tmp_path / "corpus.jsonl"
    verify.verify_docs(_client(corpus_path), mode="live")
    verify.verify_docs(_client(corpus_path), mode="live")

    assert len(_rows(corpus.selfcheck_sibling(corpus_path))) == 2
    assert _rows(corpus_path) == []


# --- the mechanism ---------------------------------------------------------------------


def test_selfcheck_sibling_is_co_located_and_idempotent(tmp_path: Path) -> None:
    """Same shape as ``synthetic_sibling`` / ``simulated_sibling``, and re-deriving from
    an already-segregated path cannot walk back to the main corpus."""
    main = tmp_path / "sub" / "corpus.jsonl"
    sibling = corpus.selfcheck_sibling(main)

    assert sibling == tmp_path / "sub" / "selfcheck.jsonl"
    assert corpus.selfcheck_sibling(sibling) == sibling
    assert sibling != corpus.synthetic_sibling(main)
    assert sibling != corpus.simulated_sibling(main)


def test_scope_restores_the_main_path_even_when_the_run_raises(tmp_path: Path) -> None:
    """The redirect is bounded to the self-check: a client handed to verify keeps its own
    corpus path afterwards, including on an exception."""
    corpus_path = tmp_path / "corpus.jsonl"
    client = _client(corpus_path)

    with pytest.raises(RuntimeError):
        with verify.selfcheck_corpus(client):
            assert client._corpus_path == corpus.selfcheck_sibling(corpus_path)
            raise RuntimeError("boom")

    assert client._corpus_path == corpus_path


def test_scope_is_a_no_op_when_capture_is_off(tmp_path: Path) -> None:
    """Corpus capture is opt-in; with it off the scope must not invent a file."""
    client = AgentApiClient(
        SPEC,
        base_url="https://api.example.com",
        session=None,
        live_transport=_not_found,
        surface_id="example.com",
    )
    with verify.selfcheck_corpus(client):
        assert client._corpus_path is None
    assert list(Path(tmp_path).glob("*.jsonl")) == []


# --- the sibling command with the same shape --------------------------------------------


def test_authcheck_live_probe_never_enables_corpus_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``gecko auth test --live`` is the other Gecko-initiated live call. It builds its own
    client and must keep corpus capture OFF — wiring a corpus path there would reopen this
    hole through a second door."""
    from gecko import authcheck, client as client_mod

    seen: dict[str, Any] = {}
    real = client_mod.AgentApiClient

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(client_mod, "AgentApiClient", spy)
    authcheck.live_probe(SPEC, "example.com", live_transport=_not_found)

    assert seen.get("corpus_path") is None, (
        "authcheck must not write agent-denominator rows for a Gecko-initiated probe"
    )

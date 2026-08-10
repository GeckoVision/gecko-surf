"""The first-call-correct honesty bug: a self-healed call is NOT first-call-correct.

``client._call_live`` retries ONCE on 401/403 from a refreshable session. Before this
fix it fired a single capture carrying the FINAL status with ``attempt`` left at its
default of ``1``, and ``corpus.outcome_from`` computes
``first_call_correct = ok and attempt == 1`` — so a call that only worked on the SECOND
attempt was persisted as first-call-correct. That corrupts the headline metric at its
source, and it corrupts it in the flattering direction.

Falsifier-first, offline, $0: one injected transport that 401s once then 200s, and the
persisted corpus row is read back off disk. No mocks — the transport IS the fake.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gecko.caller import PreparedRequest
from gecko.client import AgentApiClient

SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/ping": {
            "get": {
                "operationId": "ping",
                "summary": "ping the service",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"ok": {"type": "boolean"}},
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}


class FakeRefreshable:
    """Minimal RefreshableSession: the bearer advances on ``invalidate()``.

    ``["STALE", "FRESH"]`` models a rejected-then-valid credential. It performs no
    network of its own — the retry is driven entirely through the injected transport.
    """

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = list(tokens)
        self._i = 0
        self.invalidate_calls = 0

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens[self._i]}"}

    def invalidate(self) -> None:
        self.invalidate_calls += 1
        if self._i < len(self._tokens) - 1:
            self._i += 1

    def expires_at(self) -> float | None:
        return None


def _client(session: Any, transport: Any, corpus_path: Path) -> AgentApiClient:
    return AgentApiClient(
        SPEC,
        base_url="https://api.example.com",
        session=session,
        live_transport=transport,
        corpus_path=corpus_path,
    )


def _rows(corpus_path: Path) -> list[dict[str, Any]]:
    if not corpus_path.exists():
        return []
    return [
        json.loads(line)
        for line in corpus_path.read_text().splitlines()
        if line.strip()
    ]


# --- the bug -------------------------------------------------------------------------


def test_self_healed_call_is_captured_as_attempt_two_and_not_first_call_correct(
    tmp_path: Path,
) -> None:
    corpus_path = tmp_path / "corpus.jsonl"
    seen: list[str] = []

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        bearer = req.headers.get("Authorization", "")
        seen.append(bearer)
        if "STALE" in bearer:
            return 401, {"error": "token expired"}
        return 200, {"ok": True}

    session = FakeRefreshable(["STALE", "FRESH"])
    client = _client(session, transport, corpus_path)
    result = client.call("ping", {}, mode="live")

    # The agent still gets its successful result — the honesty fix is about what we
    # PERSIST, not about the call's outcome.
    assert result["status"] == 200
    assert seen == ["Bearer STALE", "Bearer FRESH"]  # bounded-once, unchanged

    rows = _rows(corpus_path)
    assert len(rows) == 1, "still exactly ONE row per call — no intermediate 401 row"
    row = rows[0]
    assert row["status"] == 200
    assert row["ok"] is True
    assert row["attempt"] == 2
    assert row["first_call_correct"] is False


def test_no_retry_stays_attempt_one_and_first_call_correct(tmp_path: Path) -> None:
    """The non-retried path is untouched: a clean 200 is still attempt=1 / FCC."""
    corpus_path = tmp_path / "corpus.jsonl"

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        return 200, {"ok": True}

    session = FakeRefreshable(["FRESH"])
    client = _client(session, transport, corpus_path)
    client.call("ping", {}, mode="live")

    rows = _rows(corpus_path)
    assert len(rows) == 1
    assert rows[0]["attempt"] == 1
    assert rows[0]["first_call_correct"] is True
    assert session.invalidate_calls == 0


def test_self_healed_failure_is_attempt_two_and_not_first_call_correct(
    tmp_path: Path,
) -> None:
    """A retried call that lands on a NON-auth failure is attempt=2 and not FCC.

    Guards the case where the retry changes the status without succeeding: ``ok`` is
    already False, but ``attempt`` must still tell the truth so the retry rate is
    recoverable from the corpus.
    """
    corpus_path = tmp_path / "corpus.jsonl"

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        bearer = req.headers.get("Authorization", "")
        if "STALE" in bearer:
            return 401, {"error": "token expired"}
        return 404, {"error": "gone"}

    session = FakeRefreshable(["STALE", "FRESH"])
    client = _client(session, transport, corpus_path)
    result = client.call("ping", {}, mode="live")

    assert result["status"] == 404
    rows = _rows(corpus_path)
    assert len(rows) == 1
    assert rows[0]["attempt"] == 2
    assert rows[0]["ok"] is False
    assert rows[0]["first_call_correct"] is False


def test_capture_never_carries_the_auth_header_or_the_retry_body(
    tmp_path: Path,
) -> None:
    """Redaction is structural, but assert it on the retry path specifically: neither
    the token nor the intermediate 401 body may appear anywhere in the persisted row."""
    corpus_path = tmp_path / "corpus.jsonl"

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        if "STALE" in req.headers.get("Authorization", ""):
            return 401, {"error": "token expired"}
        return 200, {"ok": True}

    client = _client(FakeRefreshable(["STALE", "FRESH"]), transport, corpus_path)
    client.call("ping", {}, mode="live")

    raw = corpus_path.read_text()
    for forbidden in ("STALE", "FRESH", "Authorization", "Bearer", "token expired"):
        assert forbidden not in raw

"""Tests for the gecko subcommand dispatcher (gecko.cli).

Offline, $0: backward-compat dispatch (bare `gecko <spec>` == `gecko serve <spec>`),
`gecko test` on a fixture spec, and `gecko from-docs` on the committed static doc.
"""

from __future__ import annotations

import json
from pathlib import Path

from gecko import cli, serve

_FIX = Path(__file__).resolve().parent / "fixtures"
PEGANA = str(_FIX / "pegana_openapi.json")
SAMPLE_DOCS = str(_FIX / "sample_docs.html")

# A tiny all-pass spec (a no-required op + a required-body op), written to a temp file
# so `gecko test` has a local, deterministic target with no network.
_TINY_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Tiny", "version": "1"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/widgets": {
            "get": {
                "operationId": "listWidgets",
                "summary": "list widgets",
                "responses": {
                    "200": {
                        "content": {"application/json": {"schema": {"type": "object"}}}
                    }
                },
            },
            "post": {
                "operationId": "createWidget",
                "summary": "create a widget",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {"name": {"type": "string"}},
                            }
                        }
                    },
                },
                "responses": {
                    "201": {
                        "content": {"application/json": {"schema": {"type": "object"}}}
                    }
                },
            },
        }
    },
}


def _write_tiny(tmp_path: Path) -> str:
    spec = tmp_path / "tiny.json"
    spec.write_text(json.dumps(_TINY_SPEC), encoding="utf-8")
    return str(spec)


# --- backward-compat dispatch ------------------------------------------------


def test_bare_spec_and_serve_split_to_same_action() -> None:
    # The load-bearing backward-compat guarantee: both forms parse to the same
    # (command, rest) and therefore the same downstream call.
    assert cli._default_to_serve([PEGANA]) == ("serve", [PEGANA])
    assert cli._default_to_serve(["serve", PEGANA]) == ("serve", [PEGANA])
    assert cli._default_to_serve([PEGANA]) == cli._default_to_serve(["serve", PEGANA])


def test_known_subcommands_and_help_route_correctly() -> None:
    assert cli._default_to_serve(["test", "x.json"]) == ("test", ["x.json"])
    assert cli._default_to_serve(["from-docs", "d.html"]) == ("from-docs", ["d.html"])
    assert cli._default_to_serve(["-h"]) == ("help", [])
    assert cli._default_to_serve([]) == ("help", [])


def test_bare_and_serve_reach_serve_identically(monkeypatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(serve, "serve_http", lambda client, **k: captured.append(k))

    assert cli.main([PEGANA, "--port", "9123", "--name", "pegana"]) == 0
    assert cli.main(["serve", PEGANA, "--port", "9123", "--name", "pegana"]) == 0
    assert len(captured) == 2
    assert captured[0] == captured[1]  # identical serve_http invocation
    assert captured[0]["server_name"] == "pegana"


# --- gecko test --------------------------------------------------------------


def test_test_subcommand_all_pass(tmp_path, capsys) -> None:
    rc = cli.main(["test", _write_tiny(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "checks passed (recorded mode)" in out
    assert "[PASS]" in out and "[FAIL]" not in out


def test_test_subcommand_writes_module(tmp_path, capsys) -> None:
    out_file = tmp_path / "test_api_firstcall.py"
    rc = cli.main(["test", _write_tiny(tmp_path), "-o", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    assert "def test_first_call_well_formed" in out_file.read_text(encoding="utf-8")


# --- gecko from-docs ---------------------------------------------------------


def test_from_docs_recovers_and_comprehends(capsys) -> None:
    rc = cli.main(["from-docs", SAMPLE_DOCS])
    out = capsys.readouterr().out
    assert rc == 0
    assert "recovered 3 candidate operation(s)" in out
    for op in ("sendBundle", "getTipAccounts", "getTipFloor"):
        assert op in out
    assert "comprehended draft -> 3 agent tool(s)" in out


def test_from_docs_writes_draft(tmp_path, capsys) -> None:
    out_file = tmp_path / "draft.json"
    rc = cli.main(["from-docs", SAMPLE_DOCS, "-o", str(out_file)])
    assert rc == 0
    draft = json.loads(out_file.read_text(encoding="utf-8"))
    assert set(draft["paths"]) == {"/sendBundle", "/getTipAccounts", "/getTipFloor"}


def test_from_docs_rejects_unsafe_url(capsys) -> None:
    rc = cli.main(["from-docs", "http://169.254.169.254/docs"])
    assert rc == 2
    assert "unsafe" in capsys.readouterr().err.lower()


def test_test_rejects_unsafe_url(capsys) -> None:
    rc = cli.main(["test", "http://169.254.169.254/openapi.json"])
    assert rc == 2
    assert "unsafe" in capsys.readouterr().err.lower()

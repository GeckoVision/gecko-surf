"""gecko serve --registry: fetch from the registry instead of a spec path.

The end-to-end tests below exercise ``serve.main()`` fully offline: they patch
``fetch_surface`` at its SOURCE module (``gecko.registry.client`` — ``serve.main``
imports it inside the function body, so the module attribute is what's actually
called) and patch ``serve_http`` on ``gecko.serve`` itself (imported at module
top there), so nothing ever touches the network or blocks on a real server.
"""

from typing import Any

import pytest

from gecko import serve
from gecko.registry.client import FetchedSurface, RegistryFetchError


def test_registry_and_spec_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        serve._parse_args(["./spec.json", "--registry", "colosseum"])


def test_registry_flag_parses():
    args = serve._parse_args(
        ["--registry", "colosseum", "--auth-env", "COLOSSEUM_COPILOT_PAT"]
    )
    assert args.registry == "colosseum"
    assert args.registry_url == "https://mcp.geckovision.tech"
    assert args.auth_env == "COLOSSEUM_COPILOT_PAT"
    assert args.spec is None


def test_spec_still_works_without_registry():
    args = serve._parse_args(["./spec.json"])
    assert args.spec == "./spec.json" and args.registry is None


SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "T", "version": "1"},
    "paths": {
        "/x": {
            "get": {"operationId": "getX", "responses": {"200": {"description": "ok"}}}
        }
    },
}


def _recording_serve_http(calls: list[dict[str, Any]]) -> Any:
    def _fake(client: Any, **kwargs: Any) -> None:
        calls.append({"client": client, **kwargs})

    return _fake


def test_registry_success_serves_fetched_spec(monkeypatch: Any, capsys: Any) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(serve, "serve_http", _recording_serve_http(calls))
    monkeypatch.setattr(
        "gecko.registry.client.fetch_surface",
        lambda *a, **k: FetchedSurface(
            name="demo", surface_rev="abc123", tier="free", spec=SPEC
        ),
    )

    rc = serve.main(["--registry", "demo"])

    assert rc == 0
    assert len(calls) == 1
    served_client = calls[0]["client"]
    # The client served is the one built from the FETCHED spec, not some other spec.
    assert [op.operation_id for op in served_client.operations] == ["getX"]
    out = capsys.readouterr().out
    assert "comprehended 1 operations" in out


def test_registry_fetch_error_returns_2_with_stderr(
    monkeypatch: Any, capsys: Any
) -> None:
    def _raise(*a: Any, **k: Any) -> Any:
        raise RegistryFetchError("registry unreachable and no cached copy of 'demo'")

    monkeypatch.setattr("gecko.registry.client.fetch_surface", _raise)

    rc = serve.main(["--registry", "demo"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Could not fetch surface" in err
    assert "registry unreachable" in err


def test_registry_stale_fetch_prints_notice(monkeypatch: Any, capsys: Any) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(serve, "serve_http", _recording_serve_http(calls))
    monkeypatch.setattr(
        "gecko.registry.client.fetch_surface",
        lambda *a, **k: FetchedSurface(
            name="demo", surface_rev="abc123", tier="free", spec=SPEC, stale=True
        ),
    )

    rc = serve.main(["--registry", "demo"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "stale" in out.lower()


def test_registry_auth_env_missing_returns_1(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.delenv("GECKO_TEST_MISSING_TOKEN", raising=False)

    rc = serve.main(["--registry", "demo", "--auth-env", "GECKO_TEST_MISSING_TOKEN"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "GECKO_TEST_MISSING_TOKEN" in err


def test_registry_with_emit_dir_returns_2(capsys: Any) -> None:
    rc = serve.main(["--registry", "demo", "--emit-dir", "/tmp/gecko-emit-unused"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "--emit-dir is not supported with --registry" in err

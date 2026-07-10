"""Falsifier for ``gecko doctor`` — offline, $0, deterministic.

Constructs envs (mcp present/absent via monkeypatch, credential present/absent via a
light fake resolver, cloudflared present/absent via monkeypatched ``shutil.which``) and
asserts the report's checks, recommended transport, and add command. Also proves the
JSON round-trips, carries NO secret, and that doctor makes no network call and mutates
no config.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


from gecko import cli, doctor

_SECRET = "super-secret-pat-value"


# --- light fakes (no heavy mocking) ------------------------------------------


@dataclass
class _FakeBackend:
    """A one-slot credential backend: returns ``value`` (or ``None`` for a miss)."""

    name: str
    value: str | None

    def available(self) -> bool:
        return True

    def get(self, ref: object) -> str | None:
        return self.value


class _FakeResolver:
    """Duck-typed ChainResolver: ``which_backend`` only needs ``.backends``."""

    def __init__(self, backends: list[_FakeBackend]) -> None:
        self.backends = backends


def _present() -> _FakeResolver:
    return _FakeResolver([_FakeBackend("keyring", _SECRET)])


def _absent() -> _FakeResolver:
    return _FakeResolver([_FakeBackend("env", None)])


# --- transport recommendation ------------------------------------------------


def test_stdio_is_the_default_recommendation(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_mcp_available", lambda: True)
    report = doctor.run_doctor("colosseum", resolver=_present())

    assert report.recommended_transport == "stdio"
    # The add command is the exact stdio spawn line — no port, no tunnel.
    assert report.add_command == (
        'claude mcp add colosseum -- uvx --from "gecko-surf[serve]" '
        "colosseum-mcp --stdio"
    )
    # No cloudflared probe in stdio mode.
    assert not any(c.name == "cloudflared" for c in report.checks)


def test_remote_recommends_http_and_probes_cloudflared(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_mcp_available", lambda: True)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/cloudflared")
    report = doctor.run_doctor("colosseum", remote=True, resolver=_present())

    assert report.recommended_transport == "http"
    cf = next(c for c in report.checks if c.name == "cloudflared")
    assert cf.ok is True
    assert report.add_command.startswith("claude mcp add --transport http colosseum ")


def test_remote_missing_cloudflared_warns(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_mcp_available", lambda: True)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    report = doctor.run_doctor("colosseum", remote=True, resolver=_present())

    cf = next(c for c in report.checks if c.name == "cloudflared")
    assert cf.ok is False
    assert any("cloudflared" in w for w in report.warnings)


# --- mcp presence ------------------------------------------------------------


def test_mcp_absent_flags_serve_install_hint(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_mcp_available", lambda: False)
    report = doctor.run_doctor(resolver=_absent())

    mcp = next(c for c in report.checks if c.name == "mcp")
    assert mcp.ok is False
    assert any("gecko-surf[serve]" in w for w in report.warnings)


# --- credential presence probe -----------------------------------------------


def test_credential_present_reports_backend_never_value(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_mcp_available", lambda: True)
    report = doctor.run_doctor("colosseum", resolver=_present())

    cred = next(c for c in report.checks if c.name == "credential:colosseum")
    assert cred.ok is True
    assert "keyring" in cred.detail
    # The value must never surface — anywhere in the report.
    assert _SECRET not in json.dumps(report.to_dict())


def test_credential_absent_emits_remediation(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_mcp_available", lambda: True)
    report = doctor.run_doctor("colosseum", resolver=_absent())

    cred = next(c for c in report.checks if c.name == "credential:colosseum")
    assert cred.ok is False
    assert "gecko auth set colosseum" in cred.detail


def test_no_api_skips_the_credential_check(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_mcp_available", lambda: True)
    report = doctor.run_doctor(resolver=_absent())

    assert not any(c.name.startswith("credential:") for c in report.checks)


def test_failing_command_backend_is_a_miss_not_a_crash(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_mcp_available", lambda: True)

    class _Boom:
        name = "command"

        def available(self) -> bool:
            return True

        def get(self, ref: object) -> str | None:
            from gecko.credentials import CredentialError

            raise CredentialError("command failed (exit 1)")

    report = doctor.run_doctor("colosseum", resolver=_FakeResolver([_Boom()]))
    cred = next(c for c in report.checks if c.name == "credential:colosseum")
    assert cred.ok is False


# --- JSON round-trip ---------------------------------------------------------


def test_to_dict_round_trips_and_is_well_formed(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_mcp_available", lambda: True)
    report = doctor.run_doctor("colosseum", resolver=_present())

    payload = json.loads(json.dumps(report.to_dict()))
    assert set(payload) == {
        "checks",
        "recommended_transport",
        "add_command",
        "warnings",
    }
    assert payload["recommended_transport"] == "stdio"
    for check in payload["checks"]:
        assert set(check) == {"name", "ok", "detail"}
        assert isinstance(check["ok"], bool)


# --- read-only invariants ----------------------------------------------------


def test_doctor_makes_no_network_call(monkeypatch) -> None:
    import socket
    import urllib.request

    def _no_net(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("doctor made a network call")

    monkeypatch.setattr(urllib.request, "urlopen", _no_net)
    monkeypatch.setattr(socket.socket, "connect", _no_net)
    monkeypatch.setattr(doctor, "_mcp_available", lambda: True)

    # Passing a fake resolver keeps this off any real keyring/dbus socket too.
    report = doctor.run_doctor("colosseum", resolver=_present())
    assert report.recommended_transport == "stdio"


def test_doctor_writes_no_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GECKO_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(doctor, "_mcp_available", lambda: True)

    doctor.run_doctor("colosseum", resolver=_present())
    # Read-only: doctor never creates the config home or anything under it.
    assert list(tmp_path.iterdir()) == []


# --- CLI wiring --------------------------------------------------------------


def test_cli_doctor_json_output(monkeypatch, capsys) -> None:
    monkeypatch.setattr(doctor, "_mcp_available", lambda: True)
    monkeypatch.setattr(doctor.credentials, "default_resolver", lambda: _present())
    rc = cli.main(["doctor", "colosseum", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    payload = json.loads(out)
    assert payload["recommended_transport"] == "stdio"
    assert _SECRET not in out


def test_cli_doctor_human_table(monkeypatch, capsys) -> None:
    monkeypatch.setattr(doctor, "_mcp_available", lambda: True)
    monkeypatch.setattr(doctor.credentials, "default_resolver", lambda: _absent())
    rc = cli.main(["doctor", "colosseum"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "gecko doctor" in out
    assert "claude mcp add colosseum" in out
    assert "✗" in out  # the absent credential renders as a failed check


def test_doctor_routes_via_dispatcher() -> None:
    assert cli._default_to_serve(["doctor", "colosseum"]) == (
        "doctor",
        ["colosseum"],
    )

"""Phase 3 — ``CommandBackend`` + the references-only ``config.toml`` loader.

The security deliverable: a configured fetch command hands the secret over on the
child's **stdout** via an **argv list** (``shell=False``) — never a shell, so the
value never touches history/log. A non-zero exit raises a redacted
``CredentialError`` (command name + exit code only — never stdout). The config
file holds **references only** (command strings + auth-mapping overrides) — never
a secret value. All offline, fakes only, no ``keyring``, no network.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from gecko.credentials import (
    ChainResolver,
    CommandBackend,
    CredentialBackend,
    CredentialError,
    CredentialRef,
    _run_argv,
    load_config,
)

SENTINEL = "SENTINEL-DO-NOT-LEAK"


def _result(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=""
    )


# --- CommandBackend: behaviour via an injected fake runner -------------------


def test_get_returns_stripped_stdout() -> None:
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _result(0, f"  {SENTINEL}\n")

    backend = CommandBackend(
        commands={"txodds": ["op", "read", "op://vault/txodds/cred"]},
        runner=fake_runner,
    )
    assert backend.get(CredentialRef(api="txodds")) == SENTINEL  # stripped
    # The call used an ARGV LIST (never a shell string).
    assert calls == [["op", "read", "op://vault/txodds/cred"]]
    assert isinstance(calls[0], list)


def test_default_runner_uses_argv_list_shell_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=["x"], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    _run_argv(["op", "read", "op://v/t"])

    assert isinstance(captured["argv"], list)  # argv list, never a shell string
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False  # the security invariant
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_nonzero_exit_raises_with_code_not_stdout() -> None:
    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return _result(7, SENTINEL)  # command wrote the secret to stdout, then failed

    backend = CommandBackend(commands={"txodds": ["op", "read"]}, runner=fake_runner)
    with pytest.raises(CredentialError) as exc:
        backend.get(CredentialRef(api="txodds"))
    msg = str(exc.value)
    assert "7" in msg  # the exit code is reported
    assert "op" in msg  # the command NAME is reported
    assert SENTINEL not in msg  # ...but NEVER the stdout
    assert SENTINEL not in repr(exc.value)


def test_available_true_when_a_command_is_configured() -> None:
    assert CommandBackend(commands={"txodds": ["op", "read"]}).available() is True


def test_available_false_when_no_command_configured() -> None:
    assert CommandBackend().available() is False


def test_get_miss_when_no_command_for_this_ref() -> None:
    def unreached(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("runner must not fire for an unconfigured ref")

    backend = CommandBackend(commands={"other": ["op", "read"]}, runner=unreached)
    assert backend.get(CredentialRef(api="txodds")) is None


def test_command_backend_satisfies_protocol() -> None:
    assert isinstance(CommandBackend(), CredentialBackend)


# --- config.toml loader: references ONLY -------------------------------------


def _write_config(tmp_path: object, text: str) -> str:
    home = tmp_path / ".gecko"  # type: ignore[operator]
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")
    return str(home)


def test_load_config_parses_command_array(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _write_config(
        tmp_path,
        '[credentials.txodds]\ncommand = ["op", "read", "op://vault/txodds/cred"]\n',
    )
    monkeypatch.setenv("GECKO_CONFIG_HOME", home)
    cfg = load_config()
    assert cfg.refs["txodds"].command == ["op", "read", "op://vault/txodds/cred"]
    assert cfg.commands()["txodds"] == ["op", "read", "op://vault/txodds/cred"]


def test_load_config_parses_command_string_no_shell(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A string command is split with shlex (lexical only — NEVER executed by a shell).
    home = _write_config(
        tmp_path, '[credentials.txodds]\ncommand = "op read op://vault/txodds/cred"\n'
    )
    monkeypatch.setenv("GECKO_CONFIG_HOME", home)
    cfg = load_config()
    assert cfg.refs["txodds"].command == ["op", "read", "op://vault/txodds/cred"]


def test_load_config_parses_auth_mapping_overrides(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _write_config(
        tmp_path,
        '[credentials."colosseum:alt"]\n'
        'command = ["pass", "colosseum/alt"]\n'
        'header = "Authorization"\n'
        'scheme = "bearer"\n'
        'account = "alt"\n',
    )
    monkeypatch.setenv("GECKO_CONFIG_HOME", home)
    cfg = load_config()
    ref = cfg.refs["colosseum:alt"]
    assert ref.header == "Authorization"
    assert ref.scheme == "bearer"
    assert ref.account == "alt"


def test_load_config_missing_file_is_empty(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GECKO_CONFIG_HOME", str(tmp_path / "does-not-exist"))  # type: ignore[operator]
    cfg = load_config()
    assert cfg.refs == {}
    assert cfg.commands() == {}


def test_config_holds_references_only_no_secret_value_read(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A secret-looking key in config must NEVER be read as a credential value:
    # the loader only understands references (command/header/scheme/account).
    home = _write_config(
        tmp_path,
        "[credentials.txodds]\n"
        f'token = "{SENTINEL}"\n'
        f'secret = "{SENTINEL}"\n'
        f'value = "{SENTINEL}"\n'
        'command = ["op", "read", "op://vault/txodds/cred"]\n',
    )
    monkeypatch.setenv("GECKO_CONFIG_HOME", home)
    cfg = load_config()
    # Nothing in the parsed config carries the sentinel — only the reference argv.
    assert SENTINEL not in repr(cfg)
    assert cfg.commands()["txodds"] == ["op", "read", "op://vault/txodds/cred"]
    # There is no field on the ref that could surface a config-held secret.
    ref = cfg.refs["txodds"]
    assert SENTINEL not in repr(ref)


# --- default_resolver: command in the chain + the pin ------------------------


def _config_command(value: str) -> list[str]:
    """A real, no-network argv that prints ``value`` on stdout (for wiring tests)."""
    return [sys.executable, "-c", f"import sys; sys.stdout.write({value!r})"]


def test_command_between_keyring_and_env(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gecko.credentials import default_resolver

    monkeypatch.delenv("GECKO_CRED_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "keyring", raising=False)  # keyring absent
    import json

    argv = _config_command("FROM-COMMAND")
    home = _write_config(
        tmp_path, f"[credentials.txodds]\ncommand = {json.dumps(argv)}\n"
    )
    monkeypatch.setenv("GECKO_CONFIG_HOME", home)
    monkeypatch.setenv("GECKO_CRED_TXODDS", "FROM-ENV")  # lower precedence
    resolver = default_resolver()
    # keyring absent -> command wins over env.
    assert resolver.resolve(CredentialRef(api="txodds")) == "FROM-COMMAND"


def test_pin_command_restricts_to_command_only(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from gecko.credentials import default_resolver

    # keyring present with a different value, env present too — the pin must ignore both.
    fake = _FakeKeyring()
    fake.set_password("gecko:txodds", "gecko", "FROM-KEYRING")
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setenv("GECKO_CRED_TXODDS", "FROM-ENV")

    argv = _config_command("FROM-COMMAND")
    home = _write_config(
        tmp_path, f"[credentials.txodds]\ncommand = {json.dumps(argv)}\n"
    )
    monkeypatch.setenv("GECKO_CONFIG_HOME", home)
    monkeypatch.setenv("GECKO_CRED_BACKEND", "command")
    resolver = default_resolver()
    assert resolver.resolve(CredentialRef(api="txodds")) == "FROM-COMMAND"


class _FakeKeyring:
    """In-memory stand-in for the ``keyring`` module (no OS store, no network)."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_keyring(self) -> object:
        return self

    def set_password(self, service: str, user: str, password: str) -> None:
        self._store[(service, user)] = password

    def get_password(self, service: str, user: str) -> str | None:
        return self._store.get((service, user))

    def delete_password(self, service: str, user: str) -> None:
        del self._store[(service, user)]


# --- leak suite extension: the command failure path never leaks --------------


def test_resolver_command_failure_error_is_redacted() -> None:
    def failing(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return _result(3, SENTINEL)

    backend = CommandBackend(commands={"txodds": ["op", "read"]}, runner=failing)
    resolver = ChainResolver([backend])
    with pytest.raises(CredentialError) as exc:
        resolver.resolve(CredentialRef(api="txodds"))
    assert SENTINEL not in str(exc.value)
    assert SENTINEL not in repr(exc.value)
    assert "3" in str(exc.value)  # exit code surfaced

"""The frozen-binary CA self-test: the line the binary prints and the gate that reads it.

Two units, both fully offline (no frozen build needed):

  * ``packaging/gecko_entry._ca_selftest_line`` builds ONE machine-parseable
    ``GECKO_CA ...`` line describing the bundled certifi this process resolved
    in-process (path / exists / bytes / frozen). CI runs ``GECKO_CA_SELFTEST=1
    <binary>`` and feeds that line to the gate below — the per-arch positive
    assertion that the bundled cert shipped + resolved on macOS + linux-arm64,
    where the cert-stripped Docker gate (linux-x86_64 only) cannot run. The
    ``GECKO_CA_SELFTEST`` env gate must NEVER affect normal CLI operation.
  * ``packaging/ca_selftest_check`` parses that line and decides pass/fail: it is
    the frozen binary, the cert exists, its size is plausible for a real CA
    bundle, and its path is INSIDE the PyInstaller ``_MEIPASS`` extraction dir
    (not the runner's own system/keychain trust store).

Both live in ``packaging/`` (not a package — the name clashes with the PyPI
``packaging`` lib), so they are loaded by file path, never imported.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType

import pytest

_PKG = Path(__file__).resolve().parents[1] / "packaging"


def _load(path: Path, name: str) -> ModuleType:
    """Load a ``packaging/*.py`` file by path (bypassing the ``packaging`` name clash).

    Registered in ``sys.modules`` before exec because a module-level ``@dataclass`` needs
    to resolve its own module there (as it does under ``python ca_selftest_check.py``, where
    the module is ``__main__``); the CI usage never hits this — it runs the file directly.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_entry = _load(_PKG / "gecko_entry.py", "gecko_entry_selftest_ut")
_check = _load(_PKG / "ca_selftest_check.py", "ca_selftest_check_ut")


# --- the entry builds a correct, machine-parseable line ------------------------


def _fake_certifi(monkeypatch: pytest.MonkeyPatch, where: object) -> None:
    monkeypatch.setitem(sys.modules, "certifi", types.SimpleNamespace(where=where))


def test_line_reports_a_real_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A frozen process with a real, bundled cacert.pem emits exists=1, the true byte
    size, frozen=1, and the resolved path — computed in-process (the _MEIPASS dir is
    gone by the time CI could stat it from outside)."""
    mei = tmp_path / "_MEI424242" / "certifi"
    mei.mkdir(parents=True)
    pem = mei / "cacert.pem"
    pem.write_bytes(b"x" * 60_000)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _fake_certifi(monkeypatch, lambda: str(pem))

    line = _entry._ca_selftest_line()
    assert line == f"GECKO_CA path={pem} exists=1 bytes=60000 frozen=1"


def test_line_reports_absent_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """certifi.where() points somewhere that isn't on disk -> exists=0, bytes=-1
    (no size available), so the gate can distinguish absent from empty."""
    ghost = tmp_path / "_MEI000000" / "certifi" / "cacert.pem"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _fake_certifi(monkeypatch, lambda: str(ghost))

    line = _entry._ca_selftest_line()
    assert line == f"GECKO_CA path={ghost} exists=0 bytes=-1 frozen=1"


def test_line_reports_not_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run under a plain interpreter (not frozen) -> frozen=0. The CI gate rejects this,
    so a self-test accidentally run against source can never pass for the binary."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    _fake_certifi(monkeypatch, lambda: "")
    line = _entry._ca_selftest_line()
    assert " frozen=0" in line


def test_line_survives_broken_certifi(monkeypatch: pytest.MonkeyPatch) -> None:
    """The self-test must never crash: a certifi that raises degrades to exists=0."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def boom() -> str:
        raise RuntimeError("certifi exploded")

    _fake_certifi(monkeypatch, boom)
    line = _entry._ca_selftest_line()
    assert line == "GECKO_CA path= exists=0 bytes=-1 frozen=1"


# --- the env gate must not leak into normal CLI operation ----------------------


def _fake_cli(monkeypatch: pytest.MonkeyPatch, called: dict[str, bool]) -> None:
    fake = types.SimpleNamespace(_run=lambda: called.__setitem__("ran", True))
    monkeypatch.setitem(sys.modules, "gecko.cli", fake)


def test_gate_off_dispatches_the_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With GECKO_CA_SELFTEST unset, main() runs the CLI and prints NO self-test line."""
    called: dict[str, bool] = {}
    _fake_cli(monkeypatch, called)
    monkeypatch.delenv("GECKO_CA_SELFTEST", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)

    _entry.main()

    assert called.get("ran") is True
    assert "GECKO_CA " not in capsys.readouterr().out


def test_gate_on_skips_the_cli_and_prints_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With GECKO_CA_SELFTEST=1, main() prints exactly one line and returns (exit 0)
    WITHOUT dispatching the CLI."""
    called: dict[str, bool] = {}
    _fake_cli(monkeypatch, called)
    monkeypatch.setenv("GECKO_CA_SELFTEST", "1")
    monkeypatch.delattr(sys, "frozen", raising=False)
    _fake_certifi(monkeypatch, lambda: "")

    _entry.main()

    assert called.get("ran") is None  # the CLI was NOT dispatched
    out = capsys.readouterr().out
    assert out.count("GECKO_CA ") == 1


def test_gate_only_triggers_on_exactly_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truthy-but-not-"1" value must not trigger the self-test (strict == "1")."""
    called: dict[str, bool] = {}
    _fake_cli(monkeypatch, called)
    monkeypatch.setenv("GECKO_CA_SELFTEST", "true")
    monkeypatch.delattr(sys, "frozen", raising=False)

    _entry.main()

    assert called.get("ran") is True
    assert "GECKO_CA " not in capsys.readouterr().out


# --- the gate parses the line and decides pass/fail ----------------------------

_GOOD_LINUX = (
    "GECKO_CA path=/tmp/_MEI123456/certifi/cacert.pem exists=1 bytes=220000 frozen=1"
)
_GOOD_MAC = "GECKO_CA path=/var/folders/2k/aa/T/_MEI99/certifi/cacert.pem exists=1 bytes=220000 frozen=1"
_GOOD_MAC_PRIVATE = "GECKO_CA path=/private/var/folders/2k/aa/T/_MEI99/certifi/cacert.pem exists=1 bytes=220000 frozen=1"


def test_parse_round_trips() -> None:
    t = _check.parse_line(_GOOD_LINUX)
    assert t.path == "/tmp/_MEI123456/certifi/cacert.pem"
    assert t.exists is True
    assert t.nbytes == 220000
    assert t.frozen is True


def test_parse_finds_line_among_noise() -> None:
    noisy = f"some banner\nwarning: whatever\n{_GOOD_LINUX}\ntrailing\n"
    assert _check.parse_line(noisy).path.endswith("cacert.pem")


@pytest.mark.parametrize(
    "bad", ["", "no marker here", "GECKO_CA path=/x exists=? bytes=y frozen=1"]
)
def test_parse_rejects_garbage(bad: str) -> None:
    with pytest.raises(_check.SelftestFormatError):
        _check.parse_line(bad)


@pytest.mark.parametrize("good", [_GOOD_LINUX, _GOOD_MAC, _GOOD_MAC_PRIVATE])
def test_check_passes_a_real_bundled_cert(good: str) -> None:
    """linux, macos /var/folders, and macos realpath'd /private/var/folders all pass:
    the /private symlink prefix must NOT be mistaken for a system store."""
    assert _check.check(_check.parse_line(good)) == []


def test_check_rejects_not_frozen() -> None:
    line = _GOOD_LINUX.replace("frozen=1", "frozen=0")
    reasons = _check.check(_check.parse_line(line))
    assert any("frozen" in r for r in reasons)


def test_check_rejects_absent_cert() -> None:
    line = "GECKO_CA path=/tmp/_MEI9/certifi/cacert.pem exists=0 bytes=-1 frozen=1"
    reasons = _check.check(_check.parse_line(line))
    assert any("exists" in r for r in reasons)


@pytest.mark.parametrize("nbytes", [0, 100, 49999, 50000])
def test_check_rejects_small_bundle(nbytes: int) -> None:
    """The floor is exclusive: exactly 50000 must still fail (a real bundle is ~200KB)."""
    line = (
        f"GECKO_CA path=/tmp/_MEI9/certifi/cacert.pem exists=1 bytes={nbytes} frozen=1"
    )
    reasons = _check.check(_check.parse_line(line))
    assert any("bytes" in r for r in reasons)


def test_check_accepts_just_over_floor() -> None:
    line = "GECKO_CA path=/tmp/_MEI9/certifi/cacert.pem exists=1 bytes=50001 frozen=1"
    assert _check.check(_check.parse_line(line)) == []


@pytest.mark.parametrize(
    "path",
    [
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/private/etc/ssl/cert.pem",  # macOS: /etc -> /private/etc
        "/usr/lib/ssl/cert.pem",
        "/usr/local/etc/openssl/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",
        "/System/Library/OpenSSL/cert.pem",
        "/Library/Keychains/System.keychain",
        "/System/Library/Keychains/SystemRootCertificates.keychain",
        "/usr/share/ca-certificates/mozilla/Foo.crt",
    ],
)
def test_check_rejects_system_and_keychain_paths(path: str) -> None:
    """A path that resolves to the runner's OWN trust store (no _MEIPASS marker) must
    fail — that would prove nothing about the bundled cert."""
    line = f"GECKO_CA path={path} exists=1 bytes=220000 frozen=1"
    reasons = _check.check(_check.parse_line(line))
    assert reasons  # rejected for at least one reason (no _MEI and/or system path)


def test_check_reports_every_failure_at_once() -> None:
    """A fully-broken line surfaces all reasons, so a CI log shows the whole story."""
    line = "GECKO_CA path=/etc/ssl/cert.pem exists=0 bytes=0 frozen=0"
    reasons = _check.check(_check.parse_line(line))
    assert len(reasons) >= 4


# --- producer + consumer agree (the whole loop) --------------------------------


def test_entry_line_is_accepted_by_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact line the frozen entry prints, for a realistic bundled cert, passes the
    gate — the producer and consumer cannot silently drift on the format."""
    mei = tmp_path / "_MEI777777" / "certifi"
    mei.mkdir(parents=True)
    pem = mei / "cacert.pem"
    pem.write_bytes(b"x" * 220_000)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _fake_certifi(monkeypatch, lambda: str(pem))

    line = _entry._ca_selftest_line()
    assert _check.check(_check.parse_line(line)) == []

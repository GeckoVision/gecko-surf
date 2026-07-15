"""Ordering hardening for the frozen-binary CA hook (``packaging/gecko_entry.py``).

The frozen entry must export ``SSL_CERT_FILE`` (pointing at the bundled certifi store)
BEFORE any ``gecko`` import runs — importing the package eagerly imports the engine
(``gecko/__init__.py`` -> access/client/mcp_server), and any SSL context built thereafter
must already see the bundled store. The old entry got the hook via
``from gecko._ca_bundle import ensure_ca_bundle``, which itself dragged the engine in
first; it was safe only because nothing built a context at import. These tests lock that
down:

  * importing the frozen entry must NOT import the gecko engine (so the CA env can be set
    before any gecko import);
  * importing the engine must NOT set ``SSL_CERT_FILE`` or build an SSL context on its own
    (the engine must never run the CA hook — only the entry does, and only when frozen);
  * the entry's INLINED CA logic must stay in lockstep with the unit-tested decision table
    in ``gecko/_ca_bundle.py`` (anti-drift, since it is a deliberate copy).

``packaging/`` is not a package and its name collides with the PyPI ``packaging`` lib, so
the entry is loaded by file path via importlib, never ``import packaging.gecko_entry``.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from types import ModuleType

import pytest

from gecko._ca_bundle import resolve_ca_bundle

_ENTRY_PATH = Path(__file__).resolve().parents[1] / "packaging" / "gecko_entry.py"


def _load_entry() -> ModuleType:
    """Load ``packaging/gecko_entry.py`` by path (bypassing the ``packaging`` name clash).

    The entry is side-effect-free on import (defs + a ``__main__`` guard), so exec-ing it
    just defines the functions — it does not run the hook or import the engine.
    """
    spec = importlib.util.spec_from_file_location("gecko_entry_under_test", _ENTRY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_entry = _load_entry()


# --- ordering: the entry does not drag the engine in ---------------------------


def test_entry_import_does_not_import_engine() -> None:
    """Importing the frozen entry must not import ``gecko`` — proves the CA hook can run
    before any gecko import. Run in a subprocess for a clean, gecko-free ``sys.modules``
    (the pytest process has already imported gecko many times over)."""
    code = textwrap.dedent(
        f"""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("gecko_entry_probe", r"{_ENTRY_PATH}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        leaked = [m for m in sys.modules if m == "gecko" or m.startswith("gecko.")]
        assert not leaked, f"importing the frozen entry dragged in the engine: {{leaked}}"
        assert hasattr(mod, "_apply_frozen_ca_bundle"), "entry lost its inlined CA hook"
        print("NO_ENGINE_OK")
        """
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 0, f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    assert "NO_ENGINE_OK" in r.stdout


# --- the engine never runs the hook / builds a context on its own --------------


def test_importing_engine_does_not_touch_ssl() -> None:
    """Importing the gecko engine must not set ``SSL_CERT_FILE`` and must not build an SSL
    context. Only the frozen entry sets the CA env (and only when frozen); if the engine
    ever did it eagerly, the ordering fix would be moot and a stray context could bake in
    the wrong CA path. Subprocess = a fresh interpreter with the ssl factories spied."""
    code = textwrap.dedent(
        """
        import os, ssl

        built = []
        _real_cdc = ssl.create_default_context
        def _spy_cdc(*a, **k):
            built.append("create_default_context")
            return _real_cdc(*a, **k)
        ssl.create_default_context = _spy_cdc
        ssl._create_default_https_context = _spy_cdc  # urllib/http.client default path

        before = os.environ.get("SSL_CERT_FILE")
        import gecko          # the SDK surface — eager engine import (access/client/mcp)
        import gecko.cli      # exactly what the frozen entry imports to dispatch
        after = os.environ.get("SSL_CERT_FILE")

        assert after == before, f"importing the engine set SSL_CERT_FILE: {before!r} -> {after!r}"
        assert not built, f"importing the engine built an SSL context: {built}"
        print("ENGINE_SSL_INERT_OK")
        """
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 0, f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    assert "ENGINE_SSL_INERT_OK" in r.stdout


# --- anti-drift: inlined entry logic == the unit-tested decision table ----------


def _install_fake_certifi(
    monkeypatch: pytest.MonkeyPatch, mode: str, pem: str
) -> object:
    """Wire ``import certifi`` inside the entry to a controlled fake and return the matching
    ``certifi_where`` callable to feed ``resolve_ca_bundle`` (so both see identical inputs).
    """
    if mode == "missing":
        monkeypatch.setitem(
            sys.modules, "certifi", None
        )  # import certifi -> ImportError
        return None
    where = (
        (lambda: pem)
        if mode == "ok"
        else (lambda: str(Path(pem).parent / "gone" / "cacert.pem"))
    )
    monkeypatch.setitem(sys.modules, "certifi", types.SimpleNamespace(where=where))
    return where


@pytest.mark.parametrize(
    "frozen, preset_ssl_file, certifi_mode",
    [
        (True, None, "ok"),  # frozen + clean env + real bundle -> export it
        (True, "/etc/ssl/cert.pem", "ok"),  # a user store wins -> untouched
        (True, "", "ok"),  # empty value is NOT an override -> still export the bundle
        (False, None, "ok"),  # not frozen -> no-op
        (True, None, "missing"),  # certifi not bundled -> no-op
        (True, None, "ghost"),  # bundle path absent on disk -> no-op
    ],
)
def test_entry_ca_matches_decision_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    frozen: bool,
    preset_ssl_file: str | None,
    certifi_mode: str,
) -> None:
    """The entry's inlined ``_apply_frozen_ca_bundle`` must produce exactly what the pure,
    unit-tested ``resolve_ca_bundle`` decides — the inline is a deliberate copy, so this is
    the guard that keeps the two from drifting."""
    pem = str(tmp_path / "cacert.pem")
    Path(pem).write_text("-----BEGIN CERTIFICATE-----\n")

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    if preset_ssl_file is not None:
        monkeypatch.setenv("SSL_CERT_FILE", preset_ssl_file)
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.delattr(sys, "frozen", raising=False)

    where = _install_fake_certifi(monkeypatch, certifi_mode, pem)

    before = os.environ.get("SSL_CERT_FILE")
    decision = resolve_ca_bundle(
        frozen=frozen,
        env=dict(os.environ),
        certifi_where=where,  # type: ignore[arg-type]
    )
    _entry._apply_frozen_ca_bundle()
    after = os.environ.get("SSL_CERT_FILE")

    if decision is None:
        assert after == before, (
            "entry changed SSL_CERT_FILE where the table said leave it"
        )
    else:
        assert after == decision, (
            "entry set a different SSL_CERT_FILE than the table decided"
        )

"""Local credential resolver — fetch the user's provider key at call time.

Phase 1: the $0 offline falsifier. A resolver turns a *reference* (which
credential, never the value) into a secret string, sourced from an ordered
backend chain with explicit precedence and graceful degradation. A missing
backend is a fall-through, never a crash; an all-miss raises a typed
``CredentialError`` that names only the ref, the backends tried, and a
remediation hint — never the secret.

This module lives wholly inside the runner process. No backend contacts a
Gecko host; the value never leaves the machine (invariant #1 — control plane).

Phase 2 adds the OS-keychain default (``KeyringBackend``, optional ``keyring``
extra), the ``default_resolver()`` chain factory, and the degradation-banner
helpers.

Phase 3 adds the external secret-manager ``CommandBackend`` (``op``/``vault``/
``pass``/``gcloud`` — the secret arrives on the child's stdout via an argv list,
never a shell) and the references-only ``~/.gecko/config.toml`` loader (command
strings + optional auth-mapping overrides — never a secret value). The chain
precedence is now ``keyring -> command -> env``. Still out of scope (Phase 4):
the short-lived OAuth mint. The ``ResolvedSession`` adapter and the ``gecko auth``
CLI live at their own seams (``access.py`` / ``cli.py``) and consume this module.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


logger = logging.getLogger(__name__)


class CredentialError(Exception):
    """Resolution failed.

    MUST NEVER contain the secret value — only the ref slot, the backend name(s)
    tried, and a remediation hint. The single leak test asserts this.
    """


@dataclass(frozen=True)
class CredentialRef:
    """WHICH credential to fetch — never the credential itself.

    ``api`` namespaces the entry (one machine, many providers); ``account`` scopes
    a named identity (e.g. two Colosseum PATs). Both fields are safe to log.
    """

    api: str
    account: str | None = None

    def slot(self) -> str:
        """Keychain "service" / env-suffix key: ``api`` or ``api:account``."""
        return self.api if self.account is None else f"{self.api}:{self.account}"


@runtime_checkable
class CredentialBackend(Protocol):
    """A source of provider secrets. ``get`` returns ``None`` on a miss — never an
    error — so the chain can fall through cleanly."""

    name: str  # "keyring" | "command" | "env" — for messaging only

    def available(self) -> bool:
        """Is this backend usable here (e.g. Secret Service reachable)?"""
        ...

    def get(self, ref: CredentialRef) -> str | None:
        """The secret, or ``None`` for a miss (NOT an error)."""
        ...


@dataclass
class ChainResolver:
    """Ordered backends; first non-``None`` hit wins.

    Degradation is a miss, not a crash — a headless box with no keyring simply
    falls through to the next backend. Never logs or embeds a secret.
    """

    backends: list[CredentialBackend]

    def resolve(self, ref: CredentialRef) -> str:
        tried: list[str] = []
        for backend in self.backends:
            if not backend.available():
                continue  # degradation: skip, don't crash
            tried.append(backend.name)
            try:
                hit = backend.get(ref)
            except CredentialError:
                # A DELIBERATE, redacted failure (e.g. CommandBackend: the user's secret-
                # manager command errored). This is a real signal the user must see — let
                # it propagate, don't silently fall through.
                raise
            except Exception:  # noqa: BLE001 - an UNEXPECTED backend fault is a miss
                # A present-but-broken backend (a macOS keychain that reports available()
                # but whose read raises errSecInteractionNotAllowed on an unsigned frozen
                # binary or a locked login keychain) must FALL THROUGH to the next
                # backend, exactly like an absent one. Crashing here defeated the env
                # fallback entirely: `gecko connect` died on the keychain read before it
                # could use GECKO_CRED_GECKO_IDENTITY, and the MCP client reported the
                # crash as "couldn't connect" — indistinguishable from a host problem.
                logger.warning(
                    "credential backend %r raised on read; treating as a miss",
                    backend.name,
                )
                continue
            if hit is not None:
                return hit
        # NOTE: message names the ref + backends only — never a value.
        raise CredentialError(
            f"no credential for {ref.slot()!r} "
            f"(tried: {', '.join(tried) or 'none'}). "
            f"Set one with: gecko auth set {ref.api}"
        )


def _env_key(ref: CredentialRef) -> str:
    """Canonical env var for a ref: ``GECKO_CRED_<SLOT>`` (upper, ``:``/``-`` -> ``_``).

    ``-`` is normalized because a hyphenated slot (``gecko-identity``) otherwise yields
    ``GECKO_CRED_GECKO-IDENTITY``, which POSIX shells cannot ``export`` — making the
    documented headless fallback unusable for exactly the refs that need it most.
    """
    return "GECKO_CRED_" + ref.slot().upper().replace(":", "_").replace("-", "_")


def _legacy_env_key(ref: CredentialRef) -> str:
    """The pre-normalization name (hyphens intact), still honoured on read.

    Only reachable when something sets it programmatically (``env``, a Docker
    ``-e``, or an MCP client's ``env`` block), which is precisely why dropping it
    silently would be a regression.
    """
    return "GECKO_CRED_" + ref.slot().upper().replace(":", "_")


@dataclass
class EnvBackend:
    """Env-var backend — the CI / headless fallback (leakiest, so lowest precedence).

    Reads the canonical ``GECKO_CRED_<API>`` first, then an optional configured
    legacy name (so ``TXODDS_API_TOKEN`` / ``COLOSSEUM_COPILOT_PAT`` still resolve
    for existing users). Unset or empty is a miss, never an error.
    """

    name: str = "env"
    legacy_names: dict[str, str] = field(default_factory=dict)

    def available(self) -> bool:
        # The environment is always readable; presence of a value is a per-ref miss.
        return True

    def get(self, ref: CredentialRef) -> str | None:
        value = os.environ.get(_env_key(ref))
        if value:  # non-empty canonical wins
            return value
        hyphenated = _legacy_env_key(ref)
        if hyphenated != _env_key(ref):
            value = os.environ.get(hyphenated)
            if value:
                return value
        legacy = self.legacy_names.get(ref.slot())
        if legacy:
            legacy_value = os.environ.get(legacy)
            if legacy_value:
                return legacy_value
        return None


# --- Keychain backend (Phase 2) ---------------------------------------------

# Every entry we write is namespaced under this service prefix so `gecko auth`
# never collides with another app's keychain items; the username slot is fixed.
_KEYRING_USER = "gecko"
# A names-only index of the slots we have stored, kept IN THE KEYCHAIN (not a
# dotfile) so `gecko auth list` can enumerate — keyring has no portable listing
# API. The index holds slot NAMES only (safe to log), never a value.
_INDEX_SLOT = "__index__"


def _service(slot: str) -> str:
    """Keychain service name for a slot: ``gecko:<slot>``."""
    return f"gecko:{slot}"


def _is_null_or_fail(active: Any) -> bool:
    """True when the active keyring is the fail/null backend — i.e. there is no
    real encrypted store (headless box, no Secret Service). Works for the real
    library (``keyring.backends.fail`` / ``.null``) and a light test fake alike,
    by matching only the backend class's defining module."""
    module = getattr(type(active), "__module__", "") or ""
    return module.endswith(".fail") or module.endswith(".null")


@dataclass
class KeyringBackend:
    """OS-keychain backend — the dev default (highest precedence).

    Reads/writes the encrypted OS store (macOS Keychain, Linux Secret Service,
    Windows Credential Manager) via the optional ``keyring`` library. The import
    is guarded: absent the ``[credentials]`` extra, ``available()`` is ``False``
    and the chain degrades to env — a plain install still runs.
    """

    name: str = "keyring"
    # Injected in tests (a light fake keyring interface); None => import the real
    # library lazily. A module reference, never a secret, so it is repr-safe.
    module: Any = None

    def _keyring(self) -> Any:
        if self.module is not None:
            return self.module
        try:
            import keyring
        except ImportError:
            return None
        return keyring

    def available(self) -> bool:
        mod = self._keyring()
        if mod is None:
            return False
        try:
            active = mod.get_keyring()
        except Exception:  # noqa: BLE001 - a broken backend means unavailable, not fatal
            return False
        return not _is_null_or_fail(active)

    def get(self, ref: CredentialRef) -> str | None:
        """The stored secret, or ``None`` on a miss. A locked keychain raises the
        library's own (secret-free) error, which the runner surfaces prefixed —
        distinct from a miss, per the spec's degradation contract."""
        mod = self._keyring()
        if mod is None:
            return None
        return mod.get_password(_service(ref.slot()), _KEYRING_USER)

    # -- write helpers (used by `gecko auth`) --------------------------------

    def _require(self) -> Any:
        """Return the module, or raise a redacted error if no keychain is usable.
        The error names only the remediation — never a ref value or a secret."""
        mod = self._keyring()
        if mod is None or not self.available():
            raise CredentialError(
                "no OS keychain available (keyring not installed, or no Secret "
                "Service on this box). Install it: pip install "
                "'gecko-surf[credentials]', or use the env fallback: "
                "export GECKO_CRED_<API>=..."
            )
        return mod

    @staticmethod
    def _keyring_error_types() -> tuple[type[BaseException], ...]:
        """The keyring exception types to map to CredentialError — resolved lazily so a
        machine without ``keyring`` installed never imports it. Empty tuple if absent."""
        try:
            from keyring.errors import KeyringError

            return (KeyringError,)
        except Exception:  # noqa: BLE001 - no keyring => nothing keyring-specific to catch
            return ()

    def selftest(self) -> tuple[bool, str]:
        """Actually WRITE→READ→DELETE a probe value, returning ``(works, detail)``.

        ``available()`` only proves a keychain *backend* is present — it returns True for a
        keychain that then REFUSES every write (an unsigned frozen macOS binary →
        errSecInteractionNotAllowed -25244). This round-trip is the only honest check of
        whether the store actually works, and it surfaces the real OS error when it
        doesn't. The probe uses a dedicated slot and never touches a real credential; it is
        cleaned up whether the read succeeds or not."""
        if not self.available():
            return False, "no keychain backend available"
        probe = CredentialRef(api="gecko-selftest")
        token = "gecko-keychain-probe"
        mod = self._keyring()
        try:
            mod.set_password(_service(probe.slot()), _KEYRING_USER, token)
        except Exception as exc:  # noqa: BLE001 - report the real reason, don't crash
            return False, f"write refused: {type(exc).__name__}: {exc}"
        try:
            got = mod.get_password(_service(probe.slot()), _KEYRING_USER)
        except Exception as exc:  # noqa: BLE001
            got = None
            detail = f"read refused: {type(exc).__name__}: {exc}"
        else:
            detail = "round-trip ok" if got == token else "read back a different value"
        finally:
            try:
                mod.delete_password(_service(probe.slot()), _KEYRING_USER)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        return (got == token), detail

    def store(self, ref: CredentialRef, secret: str) -> None:
        """Write ``secret`` to the keychain under ``gecko:<slot>``; require a
        usable keychain first. The secret is passed straight to the OS store and
        is never logged or echoed.

        A keychain that is *present* but *refuses the write* (macOS
        ``errSecInteractionNotAllowed`` -25244 from an unsigned frozen binary, a locked
        keychain, or a non-interactive session) raises a :class:`keyring.errors.KeyringError`.
        That is NOT an ``OSError``, so it used to escape every caller's ``except`` and
        crash the CLI mid-login — after the token had already been minted. We map it to a
        redacted :class:`CredentialError` so callers degrade (env fallback / clear message)
        instead of a traceback. The error carries only the OS status, never the secret."""
        mod = self._require()
        try:
            mod.set_password(_service(ref.slot()), _KEYRING_USER, secret)
            self._index_add(ref.slot())
        except self._keyring_error_types() as exc:
            raise CredentialError(
                f"the OS keychain refused the write ({type(exc).__name__}). On macOS this "
                "is often an unsigned binary or a locked keychain; use the env fallback "
                f"instead: export {_env_key(ref)}=<value>"
            ) from None

    def delete(self, ref: CredentialRef) -> bool:
        """Delete the keychain entry; idempotent. Returns whether one existed."""
        mod = self._require()
        existed = mod.get_password(_service(ref.slot()), _KEYRING_USER) is not None
        if existed:
            mod.delete_password(_service(ref.slot()), _KEYRING_USER)
        self._index_remove(ref.slot())
        return existed

    def list_slots(self) -> list[str]:
        """The names-only index of stored slots (never a value)."""
        mod = self._keyring()
        if mod is None:
            return []
        raw = mod.get_password(_service(_INDEX_SLOT), _KEYRING_USER)
        if not raw:
            return []
        try:
            slots = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return sorted(str(s) for s in slots) if isinstance(slots, list) else []

    def _write_index(self, slots: set[str]) -> None:
        mod = self._keyring()
        if mod is None:
            return
        mod.set_password(
            _service(_INDEX_SLOT), _KEYRING_USER, json.dumps(sorted(slots))
        )

    def _index_add(self, slot: str) -> None:
        self._write_index(set(self.list_slots()) | {slot})

    def _index_remove(self, slot: str) -> None:
        self._write_index(set(self.list_slots()) - {slot})


# --- Command backend (Phase 3: external secret managers) ---------------------


def _run_argv(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a configured fetch command as an ARGV LIST — never ``shell=True``.

    ``shell=False`` (the default, made explicit here as the security invariant)
    keeps the secret off any shell, out of history and logs: the value arrives on
    the child's stdout only. The command *string* the user configured is a
    reference (``op read op://...``), not the secret.
    """
    return subprocess.run(argv, shell=False, capture_output=True, text=True)


# Injectable transport seam (light-fake in tests); default is the real subprocess.
CommandRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


@dataclass
class CommandBackend:
    """External secret-manager backend — runs the user's configured fetch command.

    ``commands`` maps a ref slot to an argv list (a *reference*, e.g.
    ``["op", "read", "op://vault/txodds/cred"]``), loaded from ``config.toml``.
    ``get`` runs it, ``.strip()``s stdout, and holds the value in memory only — it
    is never logged. A non-zero exit is a real error (not a miss): it raises a
    ``CredentialError`` naming the command and exit code ONLY — never the stdout.
    """

    name: str = "command"
    commands: dict[str, list[str]] = field(default_factory=dict)
    runner: CommandRunner = field(default=_run_argv, repr=False)

    def available(self) -> bool:
        # Usable iff at least one fetch command is configured; the per-ref presence
        # check happens in ``get`` (a miss falls through, as the chain expects).
        return bool(self.commands)

    def get(self, ref: CredentialRef) -> str | None:
        argv = self.commands.get(ref.slot())
        if not argv:
            return None  # no command configured for this ref => miss, not error
        result = self.runner(list(argv))
        if result.returncode != 0:
            # Redact-before-raise: command NAME + exit code only, never the stdout.
            raise CredentialError(
                f"credential command {argv[0]!r} for {ref.slot()!r} "
                f"failed (exit {result.returncode})."
            )
        return result.stdout.strip()


# --- config.toml loader (Phase 3: references ONLY, never a secret) ------------


@dataclass(frozen=True)
class SurfaceRef:
    """Non-secret references for one credential slot, loaded from ``config.toml``.

    Holds a fetch-command argv (a REFERENCE like ``op read op://...``) plus
    optional auth-mapping overrides. **No secret value is ever stored here** — the
    loader only understands these reference fields; any other key is ignored.
    """

    command: list[str] | None = None
    header: str | None = None
    scheme: str | None = None
    account: str | None = None


@dataclass(frozen=True)
class GeckoConfig:
    """Parsed ``~/.gecko/config.toml`` — references only (missing file => empty)."""

    refs: dict[str, SurfaceRef] = field(default_factory=dict)

    def commands(self) -> dict[str, list[str]]:
        """Slot -> fetch-command argv, for building a ``CommandBackend``."""
        return {slot: r.command for slot, r in self.refs.items() if r.command}


def config_home() -> Path:
    """Directory holding ``config.toml`` (``GECKO_CONFIG_HOME`` override, else
    ``~/.gecko``). The override keeps tests hermetic and CI deterministic."""
    override = os.environ.get("GECKO_CONFIG_HOME")
    return Path(override) if override else Path.home() / ".gecko"


def config_path() -> Path:
    """Path to the references-only config file (holds no secrets)."""
    return config_home() / "config.toml"


def _parse_command(raw: Any) -> list[str] | None:
    """A command reference as an argv list. A TOML array is used verbatim; a string
    is split with ``shlex`` (lexical only — NEVER handed to a shell)."""
    if isinstance(raw, list):
        return [str(part) for part in raw]
    if isinstance(raw, str) and raw.strip():
        return shlex.split(raw)
    return None


def _str_or_none(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def load_config(path: Path | None = None) -> GeckoConfig:
    """Load references from ``config.toml``. Missing file => empty config (not an
    error). Reads ONLY reference fields (command/header/scheme/account); a
    secret-looking key is never read as a credential value.
    """
    target = path or config_path()
    if not target.exists():
        return GeckoConfig(refs={})
    with target.open("rb") as fh:
        data = tomllib.load(fh)
    section = data.get("credentials", {})
    refs: dict[str, SurfaceRef] = {}
    if isinstance(section, dict):
        for slot, entry in section.items():
            if not isinstance(entry, dict):
                continue
            refs[str(slot)] = SurfaceRef(
                command=_parse_command(entry.get("command")),
                header=_str_or_none(entry.get("header")),
                scheme=_str_or_none(entry.get("scheme")),
                account=_str_or_none(entry.get("account")),
            )
    return GeckoConfig(refs=refs)


# --- Resolver factory + audit helpers (Phase 2) ------------------------------

# Legacy env names so existing users keep resolving without a re-set. These map a
# slot to the pre-Gecko var the provider's own docs told users to export.
_LEGACY_ENV_NAMES: dict[str, str] = {
    "txodds": "TXODDS_API_TOKEN",
    "colosseum": "COLOSSEUM_COPILOT_PAT",
}


def default_resolver() -> ChainResolver:
    """The dev-default chain: OS keychain, then external command hook, then env.

    Precedence ``keyring -> command -> env`` (the spec table): the keychain is the
    safest and human-set, a configured command hook is an explicit team choice, and
    env is last (leakiest / most likely stale). ``GECKO_CRED_BACKEND`` pins the
    chain to a single backend for deterministic CI / debugging (``keyring`` |
    ``command`` | ``env``) so CI never accidentally reads a developer keychain.
    """
    keyring_backend = KeyringBackend()
    command_backend = CommandBackend(commands=load_config().commands())
    env_backend = EnvBackend(legacy_names=dict(_LEGACY_ENV_NAMES))
    pin = os.environ.get("GECKO_CRED_BACKEND", "").strip().lower()
    if pin == "keyring":
        return ChainResolver([keyring_backend])
    if pin == "command":
        return ChainResolver([command_backend])
    if pin == "env":
        return ChainResolver([env_backend])
    return ChainResolver([keyring_backend, command_backend, env_backend])


def env_var_name(ref: CredentialRef) -> str:
    """The canonical env var for a ref (``GECKO_CRED_<SLOT>``) — non-secret, for
    remediation hints and the degradation banner."""
    return _env_key(ref)


def ref_from_slot(slot: str) -> CredentialRef:
    """Inverse of ``CredentialRef.slot()``: ``api`` or ``api:account``."""
    api, _, account = slot.partition(":")
    return CredentialRef(api=api, account=account or None)


def which_backend(ref: CredentialRef, resolver: ChainResolver) -> str | None:
    """The name of the backend that WOULD resolve ``ref`` (first available hit),
    or ``None`` if nothing would. Reads the value internally to test for a hit but
    never returns, logs, or exposes it — for the ``auth list`` audit."""
    for backend in resolver.backends:
        if not backend.available():
            continue
        if backend.get(ref) is not None:
            return backend.name
    return None


def env_visible_names() -> list[str]:
    """Names of ``GECKO_CRED_*`` vars set in the environment (the config pin
    excluded) — names only, never values — so ``auth list`` can show env refs."""
    return sorted(
        key
        for key in os.environ
        if key.startswith("GECKO_CRED_") and key != "GECKO_CRED_BACKEND"
    )


# --- Degradation banner (Phase 2) --------------------------------------------

# Printed verbatim when a keychain is present but locked; carries no secret.
KEYCHAIN_LOCKED_HINT = "auth: keychain is locked — unlock it and retry."


def no_credential_message(ref: CredentialRef) -> str:
    """The actionable 'nothing resolved' line (desktop + CI paths). No secret."""
    return (
        f"no credential for {ref.slot()!r}. On a desktop: gecko auth set {ref.api}. "
        f"In CI/headless: export {env_var_name(ref)}=... (see docs/credentials)."
    )


def keyring_fallback_banner(ref: CredentialRef, resolver: ChainResolver) -> str | None:
    """The once-at-start line when the keychain is down but env WILL answer.

    Returns ``None`` when the keychain is healthy, absent from the chain, or when
    nothing resolves (the runner raises ``CredentialError`` on first call then).
    Emitted once at runner start, never per call.
    """
    keyring_backends = [b for b in resolver.backends if isinstance(b, KeyringBackend)]
    if not keyring_backends or any(b.available() for b in keyring_backends):
        return None
    env = next((b for b in resolver.backends if isinstance(b, EnvBackend)), None)
    if env is not None and env.get(ref) is not None:
        return (
            f"auth: keyring unavailable (no Secret Service); "
            f"using {env_var_name(ref)} from env."
        )
    return None

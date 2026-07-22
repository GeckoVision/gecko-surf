"""Gecko-key access control — verify a login identity + a founder allowlist.

Layer 1 of the Gecko-hosted paid-API endpoint (design: ``private/gecko-key-paid-
endpoint-design.md``). Pure access control, **no custody, no payment, no signing**:
it answers "is this Gecko key allowed to reach the hosted surface?" and nothing more.

The **Gecko key** is the sealed login identity token (``gecko login`` →
``login.IDENTITY_REF``; a Privy access-token JWT or our registry key). This module
never verifies the token's cryptography itself — that is a server-side concern
(JWKS / registry lookup, network) plugged in behind the :data:`AccountResolver`
seam. Keeping it a seam holds two invariants at once:

* **API-agnostic engine** — the real verifier lives at the transport edge, injected;
  the core stays offline-falsifiable (Pattern B: the free local simulation ships first).
* **Default-deny** — an unresolved token, or a resolved account that the founder has
  not enabled, is denied. Empty allowlist ⇒ nobody in.

Control-plane / redact-before-raise: a token value is NEVER logged, returned, stored,
or placed in an :class:`AuthDecision` or error. The allowlist store holds only the
**stable, non-secret account id** (the login identity's subject — e.g. the Privy user
id / email, same class as the plaintext ``identity.json``), never the token.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "AccountResolver",
    "Allowlist",
    "AuthDecision",
    "FileAllowlist",
    "KeyAuthError",
    "KeyGate",
    "SurfaceGrants",
    "SurfaceScopedAllowlist",
    "authorize",
    "deny_all_resolver",
    "scope_gate",
]

#: Why a decision landed. Carries the account (never the token) so a 403 can name the
#: reason without echoing a secret. ``ok`` is the only allow reason.
DecisionReason = Literal["ok", "missing_token", "invalid_token", "not_enabled"]


class KeyAuthError(Exception):
    """An allowlist-store operation failed.

    MUST NEVER contain a token value — it names only the account/path/reason. The
    leak suite asserts this.
    """


@dataclass(frozen=True)
class AuthDecision:
    """The outcome of :func:`authorize`. Holds the resolved **account id** (a stable,
    non-secret identifier) and a ``reason`` — **never** the token that produced it.

    ``account`` is ``None`` when no valid account could be resolved (missing/invalid
    token); it is populated on a ``not_enabled`` denial so the founder can see *who*
    to enable without any secret leaving the seam.
    """

    allowed: bool
    account: str | None
    reason: DecisionReason


#: token -> stable, non-secret account id, or ``None`` if the token is invalid.
#: The real implementation (Privy JWKS verify / registry lookup) is server-side and
#: injected; the core never sees the verification internals. A resolver MUST NOT log,
#: echo, or persist the token it is handed.
AccountResolver = Callable[[str], str | None]


@runtime_checkable
class Allowlist(Protocol):
    """The founder-controlled per-developer enablement store (the swappable seam).

    Only ``is_enabled`` is needed to gate a call; the local :class:`FileAllowlist` adds
    write helpers for the ``gecko keys`` ops command. A hosted store (MongoDB / env)
    implements this same read contract. It holds only non-secret **account ids**.
    """

    def is_enabled(self, account: str) -> bool:
        """Is this account id enabled? Default-deny: unknown ⇒ ``False``."""
        ...


@runtime_checkable
class SurfaceGrants(Protocol):
    """WHICH gated surfaces an account may reach — the founder's per-surface grant.

    Separate from :class:`Allowlist` on purpose. ``is_enabled`` answers "is this account
    live at all" (one switch, revokes everything); ``may_access`` answers "may it reach
    THIS paid surface". Without the split, enabling a developer for a future API #2
    silently hands them API #1 as well.
    """

    def may_access(self, account: str, surface: str) -> bool:
        """Has the founder granted ``account`` this surface? Default-deny."""
        ...


@dataclass(frozen=True)
class SurfaceScopedAllowlist:
    """An :class:`Allowlist` view for ONE gated surface: enabled **and** granted it.

    Fail-closed twice over. An account that is not enabled is denied without consulting
    grants; and an inner store that cannot express grants at all denies every gated
    surface rather than silently degrading to a bare ``is_enabled`` check — a store
    swapped in without grant support must lock the paid doors, not open them.
    """

    inner: Allowlist
    surface: str

    def is_enabled(self, account: str) -> bool:
        if not self.inner.is_enabled(account):
            return False
        if not isinstance(self.inner, SurfaceGrants):
            # Names the surface + store type only — never an account or a token.
            logger.warning(
                "allowlist %s cannot express per-surface grants; denying %r",
                type(self.inner).__name__,
                self.surface,
            )
            return False
        return self.inner.may_access(account, self.surface)


def scope_gate(gate: KeyGate, surface: str) -> KeyGate:
    """The same gate, narrowed to one surface. Used per mount at the transport edge."""
    return KeyGate(
        resolve_account=gate.resolve_account,
        allowlist=SurfaceScopedAllowlist(gate.allowlist, surface),
    )


def deny_all_resolver(_token: str) -> None:
    """A fail-closed :data:`AccountResolver` — resolves nothing, so every key is denied.

    The safe default when a hosted deployment turns the gate on but has not yet wired a
    real token verifier: fail closed (403 everyone) rather than fail open. Never logs
    the token.
    """
    return None


def authorize(
    identity_token: str | None,
    *,
    resolve_account: AccountResolver,
    allowlist: Allowlist,
) -> AuthDecision:
    """Decide whether a presented Gecko key may reach the gated surface. **Default-deny.**

    1. No/blank token ⇒ deny (``missing_token``).
    2. Token that resolves to no account ⇒ deny (``invalid_token``).
    3. Account resolved but not enabled by the founder ⇒ deny (``not_enabled``).
    4. Otherwise allow (``ok``).

    Pure and non-raising: it returns an :class:`AuthDecision`, never an exception, and
    NEVER puts the token in the decision or logs it (redact-before-raise).
    """
    if not identity_token or not identity_token.strip():
        return AuthDecision(allowed=False, account=None, reason="missing_token")
    account = resolve_account(identity_token)
    if account is None or not account.strip():
        return AuthDecision(allowed=False, account=None, reason="invalid_token")
    if not allowlist.is_enabled(account):
        return AuthDecision(allowed=False, account=account, reason="not_enabled")
    return AuthDecision(allowed=True, account=account, reason="ok")


@dataclass(frozen=True)
class KeyGate:
    """Bundles the two seams a transport gate needs: the token verifier + the allowlist.

    A frozen value object so the HTTP layer can hold one per app and call
    :meth:`decide` per request. Carries no request state and no secret.
    """

    resolve_account: AccountResolver
    allowlist: Allowlist

    def decide(self, identity_token: str | None) -> AuthDecision:
        return authorize(
            identity_token,
            resolve_account=self.resolve_account,
            allowlist=self.allowlist,
        )


# --- Local allowlist store (founder ops) -------------------------------------

#: The local allowlist file (non-secret account ids only). Sits under the same config
#: home as ``identity.json``; ``GECKO_CONFIG_HOME`` redirects it (hermetic tests).
_ALLOWLIST_FILENAME = "gecko-keys.json"


def _default_allowlist_path() -> Path:
    override = os.environ.get("GECKO_CONFIG_HOME")
    home = Path(override) if override else Path.home() / ".gecko"
    return home / _ALLOWLIST_FILENAME


@dataclass
class FileAllowlist:
    """A local, founder-run allowlist backed by a small JSON file of **account ids**.

    Account ids (the login identity's subject — Privy user id / email) are NON-SECRET
    identifiers of the same class as the already-plaintext ``identity.json``; a token
    NEVER touches this file, so a plain 0600 JSON set is appropriate (invariant #1: no
    secret at rest). The :class:`Allowlist` Protocol is the seam for the hosted
    MongoDB/env store used in production.
    """

    path: Path | None = None

    def _file(self) -> Path:
        return self.path if self.path is not None else _default_allowlist_path()

    def _load(self) -> dict[str, object]:
        target = self._file()
        if not target.exists():
            return {}
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Name the path only — the file holds no secret, but stay consistent.
            raise KeyAuthError(f"could not read allowlist at {target}") from exc
        return data if isinstance(data, dict) else {}

    def _read(self) -> set[str]:
        accounts = self._load().get("accounts")
        return {str(a) for a in accounts} if isinstance(accounts, list) else set()

    def _read_grants(self) -> dict[str, set[str]]:
        """``{account: {surface, ...}}`` — non-secret ids and mount names only."""
        raw = self._load().get("grants")
        if not isinstance(raw, dict):
            return {}
        out: dict[str, set[str]] = {}
        for account, surfaces in raw.items():
            if isinstance(surfaces, list):
                out[str(account)] = {str(s) for s in surfaces}
        return out

    def _write(
        self, accounts: set[str], grants: dict[str, set[str]] | None = None
    ) -> None:
        target = self._file()
        target.parent.mkdir(parents=True, exist_ok=True)
        # Read-modify-write: enable/disable must never drop the grant map, and
        # grant/revoke must never drop the enabled set.
        keep = self._read_grants() if grants is None else grants
        payload = (
            json.dumps(
                {
                    "accounts": sorted(accounts),
                    "grants": {a: sorted(s) for a, s in sorted(keep.items()) if s},
                },
                indent=2,
            )
            + "\n"
        )
        target.write_text(payload, encoding="utf-8")
        # Non-secret, but keep it owner-only for tidiness/consistency with the config dir.
        try:
            target.chmod(0o600)
        except OSError:  # pragma: no cover - platform without chmod semantics
            pass

    def is_enabled(self, account: str) -> bool:
        return bool(account) and account in self._read()

    def may_access(self, account: str, surface: str) -> bool:
        """Default-deny: no grant record ⇒ no gated surface, however enabled."""
        if not account or not surface:
            return False
        return surface in self._read_grants().get(account, set())

    def grant(self, account: str, surface: str) -> bool:
        """Grant ``surface`` to ``account``; ``True`` if newly added (idempotent)."""
        account = _require_account(account)
        surface = _require_surface(surface)
        grants = self._read_grants()
        held = grants.setdefault(account, set())
        if surface in held:
            return False
        held.add(surface)
        self._write(self._read(), grants)
        return True

    def revoke(self, account: str, surface: str) -> bool:
        """Revoke ``surface`` from ``account``; ``True`` if it had been granted."""
        account = _require_account(account)
        surface = _require_surface(surface)
        grants = self._read_grants()
        if surface not in grants.get(account, set()):
            return False
        grants[account].discard(surface)
        self._write(self._read(), grants)
        return True

    def grants_for(self, account: str) -> list[str]:
        return sorted(self._read_grants().get(account, set()))

    def enable(self, account: str) -> bool:
        """Enable ``account``; returns ``True`` if it was newly added (idempotent)."""
        account = _require_account(account)
        accounts = self._read()
        if account in accounts:
            return False
        accounts.add(account)
        self._write(accounts)
        return True

    def disable(self, account: str) -> bool:
        """Disable ``account``; returns ``True`` if it had been enabled (idempotent)."""
        account = _require_account(account)
        accounts = self._read()
        if account not in accounts:
            return False
        accounts.discard(account)
        self._write(accounts)
        return True

    def accounts(self) -> list[str]:
        """The enabled account ids, sorted — **never** any token."""
        return sorted(self._read())


def _require_surface(surface: str) -> str:
    """A grant names a mount, so it must look like one — never a path or a URL."""
    surface = (surface or "").strip()
    if not surface or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", surface):
        raise KeyAuthError(
            f"invalid surface name {surface!r} — expected a mount name like 'birdeye'"
        )
    return surface


def _require_account(account: str) -> str:
    account = (account or "").strip()
    if not account:
        raise KeyAuthError("account id must be a non-empty identifier")
    return account

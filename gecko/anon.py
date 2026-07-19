"""Anon-first identity — make the funnel answerable PER PERSON, not just per run.

The persistent, PII-free ``install_id`` (``~/.gecko/install_id``, a random uuid4 —
``onboard.read_or_create_install_id``) becomes the single **anonymous user identity**.
Our CLI sends it as a header on the hosted connects it configures; the SERVER hashes it
into the ``account`` event field. One opaque id then joins the whole funnel per person —
install ping → hosted connect → list_tools → call → first-call-correct — where
``session_id`` only ever identified a single *visit*.

Two tiers:

* **Tier 1 (automatic, anonymous).** The ``X-Gecko-Anon`` header carries the raw
  install id. It is control-plane-safe: a random uuid4, no hostname/email/machine id,
  and only a *truncated sha256* of it (:func:`account_hash`) is ever stored. This is
  :class:`~gecko.identity.SessionIdentity`'s anonymous shape finally turned on.
* **Tier 2 (login upgrade, the merge).** When a sealed ``gecko login`` identity exists
  (``login.load_identity``), we ALSO send ``X-Gecko-Account`` — a *client-computed hash*
  of the login subject (never the raw email). The server records a ``surf.identify`` row
  linking the anon ``account`` to that durable login hash. Login is an identity UPGRADE,
  never a wall: ``gecko add`` / ``serve`` stay zero-login.

Guardrails: **measurement only, never gating.** ``GECKO_TELEMETRY=off`` kills it
entirely — no header is emitted, so nothing is stamped. No PII crosses the wire (the
login subject is hashed client-side; the install id is a random uuid).

Two honest limits (documented at the call sites too):
1. A **direct third-party MCP connect** (a client wired straight at the hosted URL, not
   through our CLI) carries no header, so it stays ``session_id``-scoped only.
2. The Privy default-login token is **not server-verifiable** yet (only registry
   ``gk_live_`` keys resolve). We therefore **passthrough-hash** the login subject: the
   server trusts the client's hash for ATTRIBUTION only. Since this path never gates,
   a forged hash can at worst mis-attribute a count — never escalate access.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .identity import SessionIdentity
from .login import load_identity
from .onboard import read_or_create_install_id
from .telemetry import telemetry_enabled

__all__ = [
    "ACCOUNT_HEADER",
    "ANON_HEADER",
    "account_hash",
    "anon_connect_headers",
    "anon_identity",
]

#: The header our CLI sets on a hosted connect it configures. Carries the RAW install id
#: (PII-free); the server hashes it into ``account`` (:func:`account_hash`). Raw so the
#: hash matches the onboard ping's ``hash(install_id)`` and both join on one ``account``.
ANON_HEADER = "X-Gecko-Anon"
#: Set additionally when a ``gecko login`` identity exists. Carries a CLIENT-computed
#: hash of the login subject (never a raw email). Passthrough-hashed — see the honest
#: limit #2 in the module docstring.
ACCOUNT_HEADER = "X-Gecko-Account"

#: Opaque, PII-free prefix for a stored account hash — mirrors ``events._safe_session_id``'s
#: ``sid-`` and the install-id no-PII stance. A reviewer sees an ``acct-…`` token is a hash.
_ACCOUNT_PREFIX = "acct-"
_ACCOUNT_HEX = 16


def account_hash(raw: str) -> str:
    """Reduce any raw identifier to a stable, opaque, non-PII ``acct-<hex>`` token.

    A truncated sha256 — one-way, so neither an install id nor a login subject (which may
    be an email) is ever recoverable from a stored ``account``. Used on BOTH sides: the
    server hashes the raw install id from ``X-Gecko-Anon``; the client hashes the login
    subject before it ever leaves the machine. Same function so the two always agree.
    """
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_ACCOUNT_HEX]
    return _ACCOUNT_PREFIX + digest


def _login_subject(identity: dict[str, Any] | None) -> str | None:
    """A stable, durable subject for a logged-in identity, or ``None`` when not logged in.

    Prefers a provider subject id (``privy_user_id``/``subject``/``sub``) over the email,
    and namespaces it by issuer so two providers can never collide. Returned value is
    hashed by the caller — the raw email/subject never crosses the wire.
    """
    if not identity:
        return None
    issuer = str(identity.get("issuer") or identity.get("registry") or "").strip()
    for key in ("privy_user_id", "subject", "sub"):
        value = identity.get(key)
        if isinstance(value, str) and value:
            return f"{issuer}:{value}"
    email = identity.get("email")
    if isinstance(email, str) and email:
        return f"{issuer}:{email}"
    return None


def anon_identity(home: Path) -> SessionIdentity:
    """The anonymous ``SessionIdentity`` for this install — the ``.anonymous()`` shape,
    turned on and made STABLE (bound to the persistent install id rather than a fresh
    random suffix). The typed carrier for the anon id; the wire value is still the raw
    install id (see :func:`anon_connect_headers`)."""
    return SessionIdentity.for_install(read_or_create_install_id(home))


def anon_connect_headers(home: Path) -> dict[str, str]:
    """The headers our CLI attaches to a hosted connect it configures — or ``{}`` when
    telemetry is off (measurement-only; the opt-out kills it before anything is read).

    ``X-Gecko-Anon`` carries the raw install id (PII-free; server hashes → ``account``).
    ``X-Gecko-Account`` is added ONLY when a ``gecko login`` identity exists, carrying a
    client-side hash of the login subject (the Tier-2 upgrade; never a raw email).
    """
    if not telemetry_enabled():
        return {}
    install_id = read_or_create_install_id(home)
    headers = {ANON_HEADER: install_id}
    # The install id lives at ``<home>/.gecko/install_id``; the sealed login identity at
    # ``<home>/.gecko/identity.json`` (``login._write_identity`` / ``credentials.config_home``).
    # A non-default ``GECKO_CONFIG_HOME`` would move the latter — attribution then degrades
    # to anon-only, never breaks.
    subject = _login_subject(load_identity(home / ".gecko"))
    if subject is not None:
        headers[ACCOUNT_HEADER] = account_hash(subject)
    return headers

"""Server-side Privy email-OTP — identity for the hosted ``gecko login`` (SERVER-ONLY).

Moving identity SERVER-side (design ``private/gecko-hosted-login-design.md``): the client no
longer talks to Privy. The server holds ``PRIVY_APP_SECRET`` and runs the two-step OTP —
:meth:`start` sends a code (returns an opaque login handle), :meth:`verify` exchanges the
code for the verified identity (Privy ``sub`` / verified email). The Gecko key is minted from
that identity; Privy is an invisible server detail.

The wire calls sit behind the injected :class:`PrivyServerClient` Protocol, so the login
endpoints are fully offline-falsifiable (Pattern B) with a fake client. The REAL HTTP impl is
a **documented live-integration TODO**: Privy's server-side passwordless OTP endpoint/response
shape is not yet verified against a live tenant. Until the founder confirms it,
:class:`HttpPrivyServerClient` fails CLOSED (raises) so a misconfigured deploy never
half-authenticates.

Security: ``PRIVY_APP_SECRET`` is read server-side only (never shipped in a client ``.env``);
a code, secret, or identity token is never logged, echoed, or placed in an error.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .privy_login import PRIVY_BASE_URL

logger = logging.getLogger(__name__)

__all__ = [
    "HttpPrivyServerClient",
    "PrivyIdentity",
    "PrivyServerClient",
    "PrivyServerError",
    "privy_server_from_env",
]


class PrivyServerError(Exception):
    """A server-side Privy OTP call failed (send/verify). MUST NEVER contain a code, the app
    secret, or an identity token — names only a redacted reason."""


@dataclass(frozen=True)
class PrivyIdentity:
    """The verified identity from a successful :meth:`PrivyServerClient.verify`.

    ``subject`` is the STABLE, non-secret account id (Privy ``sub`` — the ``did:privy:…`` user
    id); ``email`` is the verified email (the fallback account id for a tenant that omits
    ``sub``). Neither is a secret — both are the same class as the plaintext ``identity.json``.
    """

    subject: str
    email: str | None = None

    def account_id(self) -> str | None:
        """The stable account id: the Privy subject, else the verified email, else ``None``."""
        if self.subject and self.subject.strip():
            return self.subject
        if self.email and self.email.strip():
            return self.email
        return None


@runtime_checkable
class PrivyServerClient(Protocol):
    """How the server runs one email→OTP identity check. Both methods raise
    :class:`PrivyServerError` (redacted) on failure; neither ever logs a code/secret/token."""

    def start(self, email: str) -> str:
        """Trigger a server-side email OTP for ``email``; return an opaque login handle."""
        ...

    def verify(self, login_id: str, code: str) -> PrivyIdentity:
        """Exchange ``code`` (for ``login_id``) for the verified :class:`PrivyIdentity`."""
        ...


@dataclass(frozen=True)
class HttpPrivyServerClient:
    """REAL server-side Privy OTP client — **LIVE-INTEGRATION TODO** (unverified wire shape).

    Intended contract (to confirm against a live Privy tenant, then implement):

      * auth: ``Authorization: Basic base64(app_id:app_secret)`` + ``privy-app-id`` header —
        ``app_secret`` is read server-side ONLY (never a shipped client).
      * start:  ``POST {base}/api/v1/passwordless/init`` ``{email}`` → an id to correlate.
      * verify: ``POST {base}/api/v1/passwordless/authenticate`` ``{email, code, ...}`` →
        ``{user: {id: "did:privy:…"}, ...}`` — the ``id`` is :attr:`PrivyIdentity.subject`.

    Until that shape is verified (Pattern B: the offline path ships first), every method fails
    CLOSED so a deploy with this client wired can never half-authenticate. Swap in the real
    urllib calls (reuse the SSRF-guarded ``login._default_post`` + a Basic-auth header) once the
    founder's live smoke confirms the endpoints.
    """

    app_id: str
    app_secret: str
    base_url: str = PRIVY_BASE_URL

    def start(self, email: str) -> str:
        raise PrivyServerError(
            "server-side Privy OTP is not wired yet (live-integration TODO); "
            "confirm the Privy server passwordless endpoints and implement HttpPrivyServerClient"
        )

    def verify(self, login_id: str, code: str) -> PrivyIdentity:
        raise PrivyServerError(
            "server-side Privy OTP is not wired yet (live-integration TODO); "
            "confirm the Privy server passwordless endpoints and implement HttpPrivyServerClient"
        )


def privy_server_from_env(
    env: dict[str, str] | None = None,
) -> PrivyServerClient | None:
    """Build the server OTP client from env, or ``None`` when Privy is not configured.

    Requires BOTH ``PRIVY_APP_ID`` and the server-only ``PRIVY_APP_SECRET``. Returning ``None``
    when either is unset/sentinel lets the login endpoints stay disabled (503) rather than
    guess. The secret is never logged.
    """
    source = os.environ if env is None else env
    app_id = (source.get("PRIVY_APP_ID") or "").strip()
    app_secret = (source.get("PRIVY_APP_SECRET") or "").strip()
    if not app_id or app_id == "__unset__":
        return None
    if not app_secret or app_secret == "__unset__":
        return None
    return HttpPrivyServerClient(app_id=app_id, app_secret=app_secret)

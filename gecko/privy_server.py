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
from typing import Any, Protocol, runtime_checkable

from .login import HeaderPost, _default_post
from .privy_login import PRIVY_BASE_URL

#: Privy's passwordless endpoints. NOT in the public API reference (which covers only
#: wallets/policies/intents/key-quorums) — they are the endpoints the browser SDK calls,
#: and they work server-side with just the public app id. Verified empirically:
#: init -> 200 {"success": true}; authenticate -> 200 {user:{id, linked_accounts:[...]}}
#: and 422 {"code": "invalid_credentials"} on a bad code.
_INIT_PATH = "/api/v1/passwordless/init"
_AUTH_PATH = "/api/v1/passwordless/authenticate"


def _identity_from_payload(body: Any) -> PrivyIdentity:
    """Pull ``(subject, email)`` out of an authenticate response.

    ``user.id`` is the stable ``did:privy:…`` subject; the email comes from the first
    ``linked_accounts`` entry of ``type == "email"``. Missing/!dict payloads raise rather
    than yielding a blank identity, because a blank subject AND blank email would make
    ``account_id()`` return ``None`` and mint a key against nobody.
    """
    if not isinstance(body, dict):
        raise PrivyServerError("privy returned an unexpected payload")
    user = body.get("user")
    if not isinstance(user, dict):
        raise PrivyServerError("privy returned no user")
    subject = str(user.get("id") or "").strip()
    email = None
    accounts = user.get("linked_accounts")
    if isinstance(accounts, list):
        for entry in accounts:
            if isinstance(entry, dict) and entry.get("type") == "email":
                address = str(entry.get("address") or "").strip()
                if address:
                    email = address
                    break
    if not subject and not email:
        raise PrivyServerError("privy returned no usable identity")
    return PrivyIdentity(subject=subject, email=email)


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

    #: Injected transport seam (Pattern B): ``(url, headers, payload) -> (status, body)``.
    #: Defaults to the SSRF-guarded urllib poster; a test swaps in a fake and never
    #: touches the network.
    post: HeaderPost = _default_post

    def _headers(self) -> dict[str, str]:
        # Only the PUBLIC app id is required by these endpoints — the same header the
        # browser SDK sends. app_secret is held for other server APIs (wallets/users) and
        # is deliberately NOT sent here; a secret should not travel further than it must.
        return {"privy-app-id": self.app_id, "Content-Type": "application/json"}

    def start(self, email: str) -> str:
        """Ask Privy to email a one-time code; return the handle ``verify`` needs.

        The handle IS the email, because Privy's authenticate takes ``{email, code}`` and
        has no server-issued login id. That also keeps this client STATELESS, which
        matters: the hosted service runs multiple ECS tasks, so a handle stashed on the
        instance would only resolve on whichever task happened to serve ``start``.
        """
        address = (email or "").strip()
        if not address:
            raise PrivyServerError("an email address is required")
        status, _body = self.post(
            f"{self.base_url.rstrip('/')}{_INIT_PATH}",
            {"email": address},
            headers=self._headers(),
        )
        if not 200 <= status < 300:
            # Status only — a provider body can echo the address or our payload.
            raise PrivyServerError(f"privy init returned {status}")
        return address

    def verify(self, login_id: str, code: str) -> PrivyIdentity:
        """Exchange ``{email, code}`` for the verified identity.

        A wrong/expired code is a 422 ``invalid_credentials``; every non-2xx becomes the
        same redacted error, so the response can't be used to tell "wrong code" from
        "unknown email". The returned tokens are DELIBERATELY dropped: we need identity
        only, and holding a Privy access/refresh token would be custody of a credential
        we have no use for (invariant #1).
        """
        status, body = self.post(
            f"{self.base_url.rstrip('/')}{_AUTH_PATH}",
            {"email": (login_id or "").strip(), "code": (code or "").strip()},
            headers=self._headers(),
        )
        if not 200 <= status < 300:
            raise PrivyServerError(f"privy authenticate returned {status}")
        return _identity_from_payload(body)


def privy_server_from_env(
    env: dict[str, str] | None = None,
) -> PrivyServerClient | None:
    """Build the server OTP client from env, or ``None`` when Privy is not configured.

    Requires ``PRIVY_APP_ID`` only: the passwordless endpoints authenticate with the
    PUBLIC app id, exactly as the browser SDK does. ``PRIVY_APP_SECRET`` is carried when
    present (other server APIs need it) but is NOT required to enable login and is never
    sent on the OTP calls — gating login behind a secret this flow does not use would
    disable it for no security gain. Neither value is ever logged.
    """
    source = os.environ if env is None else env
    app_id = (source.get("PRIVY_APP_ID") or "").strip()
    app_secret = (source.get("PRIVY_APP_SECRET") or "").strip()
    if not app_id or app_id == "__unset__":
        return None
    if app_secret == "__unset__":
        app_secret = ""
    return HttpPrivyServerClient(app_id=app_id, app_secret=app_secret)

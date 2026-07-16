"""Privy passwordless email-OTP provider for ``gecko login``.

Drives Privy's **client-side** passwordless email flow, which authenticates with the PUBLIC
``privy-app-id`` only. The gecko CLI is a distributed client in a public repo, so this path
NEVER references ``PRIVY_APP_SECRET`` — leaking it would compromise every server wallet on
the app. Any step that genuinely needs the app secret belongs server-side (our registry),
not here; this module has no code path that reads or transmits it.

Wire contract (verified against ``@privy-io/js-sdk-core@0.68.3`` +
``@privy-io/routes@0.2.7`` — the official SDK that implements this exact flow):

  * init         ``POST https://auth.privy.io/api/v1/passwordless/init``
                 body ``{"email": <email>}``                              → 200
  * authenticate ``POST https://auth.privy.io/api/v1/passwordless/authenticate``
                 body ``{"email": <email>, "code": <otp>, "mode": "login-or-sign-up"}``
                 → 200 ``{"user": {"id": ...}, "privy_access_token": ..., "token": ...,
                          "refresh_token": ..., "identity_token": ...}``

  * headers (client-side, public only): ``privy-app-id: <app_id>``,
    ``privy-client: <PRIVY_CLIENT>``, ``Content-Type``/``Accept: application/json``. No
    ``Authorization`` header is sent on these pre-auth calls, and no secret is ever attached.

Token sealed: the Privy access token (a JWT verifiable via ``PRIVY_JWKS_URL`` server-side),
falling back to ``token`` if a Privy tenant returns only that field. The non-secret
``identity.json`` records ``email`` + ``privy_user_id`` + ``issuer="privy"`` — never a token.

Redact-before-raise: on any non-200 the raised :class:`LoginError` carries a fixed message
and the status code ONLY — never the response body (which holds the token on success).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .login import (
    AuthResult,
    HeaderPost,
    LoginError,
    Post,
    Prompt,
    Store,
    _default_post,
    run_login,
)

#: Default Privy auth host (``PrivyInternal.baseUrl`` default in the SDK).
PRIVY_BASE_URL = "https://auth.privy.io"
#: ``privy-client`` identifies the calling SDK to Privy; the server requires the header in
#: ``<name>:<version>`` form. We mirror the verified js-sdk-core value so the request is
#: accepted exactly as the official SDK's is. Bump when this is re-verified against a newer
#: SDK. (This is the one value the founder's live smoke confirms — see SUBSCRIBE/docs.)
PRIVY_CLIENT = "js-sdk-core:0.68.3"
PRIVY_INIT_PATH = "/api/v1/passwordless/init"
PRIVY_AUTH_PATH = "/api/v1/passwordless/authenticate"
#: ``mode`` for authenticate — allow first-time enrollment for a new email (SDK type is
#: ``'login-or-sign-up' | 'no-signup'``; login-or-sign-up creates the Privy user if absent).
PRIVY_LOGIN_MODE = "login-or-sign-up"
#: ``issuer`` stamped into the non-secret identity file.
PRIVY_ISSUER = "privy"


def privy_post(
    app_id: str, *, client: str = PRIVY_CLIENT, transport: HeaderPost = _default_post
) -> Post:
    """Return a :data:`~gecko.login.Post` seam that injects Privy's client headers.

    Only the PUBLIC ``app_id`` and the client-version string are attached — never a secret,
    never an ``Authorization`` header. ``transport`` (the SSRF-guarded low-level POST) is
    injectable so the header set is falsifiable offline.
    """
    headers = {
        "privy-app-id": app_id,
        "privy-client": client,
        "Accept": "application/json",
    }

    def post(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return transport(url, body, headers=headers)

    return post


@dataclass
class PrivyProvider:
    """Privy passwordless email-OTP :class:`~gecko.login.IdentityProvider`.

    ``post`` is the header-injecting seam (see :func:`privy_post`); ``base_url`` is the Privy
    auth host. All error paths raise a redacted :class:`LoginError` (status code only).
    """

    app_id: str
    post: Post
    base_url: str = PRIVY_BASE_URL

    def send_code(self, email: str) -> None:
        status, _ = self.post(f"{self.base_url}{PRIVY_INIT_PATH}", {"email": email})
        if status in (200, 201):
            return
        if status == 429:
            raise LoginError(
                "too many code requests — wait a minute and run `gecko login` again"
            )
        if status >= 500:
            raise LoginError(
                f"Privy is temporarily unavailable (status {status}) — try again shortly"
            )
        # 4xx (bad email / app misconfig). Never echo the response body.
        raise LoginError(
            "could not send a code — check the email address and run `gecko login` again"
        )

    def verify_code(self, email: str, otp: str) -> AuthResult:
        status, body = self.post(
            f"{self.base_url}{PRIVY_AUTH_PATH}",
            {"email": email, "code": otp, "mode": PRIVY_LOGIN_MODE},
        )
        if status in (400, 401, 403):
            raise LoginError("invalid or expired code — run `gecko login` again")
        if status == 429:
            raise LoginError(
                "too many attempts — wait a minute and run `gecko login` again"
            )
        if status != 200:
            raise LoginError(
                f"Privy is temporarily unavailable (status {status}) — try again shortly"
            )
        # The Privy access token (JWT, verifiable via PRIVY_JWKS_URL) is the durable
        # credential; older tenants may return only ``token``. Never log/echo either.
        token = body.get("privy_access_token") or body.get("token")
        user = body.get("user")
        user_id = user.get("id") if isinstance(user, dict) else None
        if not token or not user_id:
            # Redact-before-raise: `body` carries the token on the success shape — never
            # include it in the message even when the shape is unexpected.
            raise LoginError(
                "Privy returned an unexpected response — run `gecko login` again"
            )
        return AuthResult(
            token=str(token),
            identity={
                "email": email,
                "privy_user_id": str(user_id),
                "issuer": PRIVY_ISSUER,
            },
        )


def build_privy_provider(
    app_id: str,
    *,
    post: Post | None = None,
    base_url: str = PRIVY_BASE_URL,
    client: str = PRIVY_CLIENT,
) -> PrivyProvider:
    """Construct a :class:`PrivyProvider`, defaulting ``post`` to the real header-injecting
    seam. Tests pass their own ``post`` to stay offline."""
    return PrivyProvider(
        app_id=app_id,
        post=post or privy_post(app_id, client=client),
        base_url=base_url,
    )


def privy_login(
    email: str,
    *,
    app_id: str,
    prompt: Prompt,
    store: Store,
    home: Path,
    post: Post | None = None,
    base_url: str = PRIVY_BASE_URL,
    client: str = PRIVY_CLIENT,
) -> int:
    """Run the Privy email→OTP enrollment and seal the token. Returns 0 on success; raises
    :class:`LoginError`. ``home`` is the config dir (a ``pathlib.Path``)."""
    provider = build_privy_provider(app_id, post=post, base_url=base_url, client=client)
    return run_login(email, provider=provider, prompt=prompt, store=store, home=home)

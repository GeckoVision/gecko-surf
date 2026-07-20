"""``gecko login`` client — talk ONLY to Gecko's server (Privy is a server detail).

The rewritten client for the hosted-login design: no ``PRIVY_APP_ID``, no direct Privy call.
It runs the two-step handshake against Gecko's server — ``POST /auth/login/start {email}`` →
``{login_id}``, then ``POST /auth/login/verify {login_id, code}`` → ``{api_key}`` — and seals
the returned Gecko key in the OS keychain via the shared :func:`~gecko.login.run_login`
orchestration (same sealing, same redaction guarantees).

``post`` is the injected transport seam (default: the SSRF-guarded ``login._default_post``), so
the flow is offline-falsifiable. Redact-before-raise: the ``api_key`` is sealed, never printed,
written to a file, or placed in an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .login import (
    AuthResult,
    LoginError,
    Post,
    Prompt,
    Store,
    _default_post,
    run_login,
)
from .netguard import UnsafeUrlError

#: The default hosted login server; overridable with ``gecko login --server``.
DEFAULT_LOGIN_SERVER = "https://mcp.geckovision.tech"
LOGIN_START_PATH = "/auth/login/start"
LOGIN_VERIFY_PATH = "/auth/login/verify"
#: ``issuer`` stamped into the non-secret identity file (this is a Gecko-minted key now).
GECKO_ISSUER = "gecko"


@dataclass
class HostedProvider:
    """A :class:`~gecko.login.IdentityProvider` for Gecko's own login endpoints.

    Stateful across the two steps: :meth:`send_code` stashes the server-issued ``login_id`` that
    :meth:`verify_code` must present. All error paths raise a redacted :class:`LoginError`
    (status/reason only) — never the emailed code or the returned key.
    """

    server_url: str
    post: Post
    _login_id: str | None = None

    def _base(self) -> str:
        return self.server_url.rstrip("/")

    def send_code(self, email: str) -> None:
        try:
            status, body = self.post(
                f"{self._base()}{LOGIN_START_PATH}", {"email": email}
            )
        except UnsafeUrlError as exc:
            raise LoginError(f"refusing unsafe login server URL: {exc}") from exc
        if status == 429:
            raise LoginError(
                "too many code requests — wait a minute and run `gecko login` again"
            )
        if status == 503:
            raise LoginError("hosted login is not enabled on this server yet")
        if status >= 500:
            raise LoginError(
                f"login server unavailable (status {status}) — try again shortly"
            )
        login_id = body.get("login_id") if status == 200 else None
        if not login_id:
            # 4xx (bad email, etc.); never echo the response body.
            raise LoginError(
                "could not send a code — check the email address and run `gecko login` again"
            )
        self._login_id = str(login_id)

    def verify_code(self, email: str, otp: str) -> AuthResult:
        if not self._login_id:
            raise LoginError("login was not started — run `gecko login` again")
        try:
            status, body = self.post(
                f"{self._base()}{LOGIN_VERIFY_PATH}",
                {"login_id": self._login_id, "code": otp},
            )
        except UnsafeUrlError as exc:
            raise LoginError(f"refusing unsafe login server URL: {exc}") from exc
        if status in (400, 401, 403):
            raise LoginError("invalid or expired code — run `gecko login` again")
        if status == 429:
            raise LoginError(
                "too many attempts — wait a minute and run `gecko login` again"
            )
        api_key = body.get("api_key") if status == 200 else None
        if not api_key:
            # Redact-before-raise: the success body carries the key — never echo it.
            raise LoginError(
                "login failed — run `gecko login` again (or check the server URL)"
            )
        return AuthResult(
            token=str(api_key),
            identity={"email": email, "server": self._base(), "issuer": GECKO_ISSUER},
        )


def hosted_login(
    email: str,
    *,
    server_url: str = DEFAULT_LOGIN_SERVER,
    prompt: Prompt,
    store: Store,
    home: Path,
    post: Post | None = None,
) -> int:
    """Run the Gecko hosted login (start → prompt → verify) and seal the returned key.

    Thin wrapper over :func:`~gecko.login.run_login` with a :class:`HostedProvider`. Returns 0
    on success; raises :class:`LoginError` (never leaking the code or key). ``post`` defaults to
    the SSRF-guarded transport; tests inject their own to stay offline.
    """
    provider = HostedProvider(server_url=server_url, post=post or _default_post)
    return run_login(email, provider=provider, prompt=prompt, store=store, home=home)

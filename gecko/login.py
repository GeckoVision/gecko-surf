"""`gecko login` — hosted-identity enrollment (email → OTP → sealed credential).

Runs an email→one-time-code enrollment against a pluggable **identity provider**, then
seals the returned credential in the OS keychain and records a NON-SECRET identity
reference in ``~/.gecko/identity.json``. The orchestration (validate → send code → prompt
→ verify → seal → record) is provider-agnostic; a provider only knows *how* to send a
code and *how* to turn a code into an ``AuthResult`` (a secret token + a non-secret
identity). Two providers ship: :class:`RegistryProvider` (our own registry OTP endpoint)
and, in ``privy_login``, the Privy passwordless email-OTP provider.

Why this exists: local ``gecko add`` (recorded, $0) stays **zero-login** — it's the wedge.
``gecko login`` gates only the HOSTED plane (attribution, rate-limit, hosted features), so
we can finally tell real users from crawlers. The credential lives in the keychain and never
in the config file; the identity file holds only non-secret references (email + issuer + a
subject id).

Every side effect is an injected seam so the whole flow is falsifiable offline: the
provider (HTTP), ``prompt`` (OTP entry), ``store`` (keychain), ``home`` (config dir). The
real wiring lives in ``cli._cmd_login``.

Security: the token is a secret — it is sealed, never written to a file, never logged, and
never in an exception. It is printed to stdout in exactly ONE case: when the keychain seal
fails (not installed / locked / unsigned frozen binary), we show it ONCE with the env-var
fallback instruction rather than lose a valid, already-minted key — the same one-time
display `gecko keys mint` uses. Redact-before-raise otherwise: a wrong code, a token, or a
raw provider response body must never appear in an exception message. The OTP is a one-time
code (not a durable secret) but is likewise never persisted.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .credentials import CredentialRef
from .credentials import _env_key
from .netguard import UnsafeUrlError, validate_public_url

#: The keychain slot the hosted-identity token seals into (sibling of a provider key).
IDENTITY_REF = CredentialRef(api="gecko-identity")

#: Honest User-Agent for outbound provider calls. Some providers sit behind Cloudflare,
#: which BANS the default ``Python-urllib/*`` signature outright (HTTP 403, error 1010
#: "browser_signature_banned") — sending a real UA clears it. Verified against Privy's edge.
try:
    _CLIENT_VERSION = _pkg_version("gecko-surf")
except PackageNotFoundError:  # editable/source checkout without installed metadata
    _CLIENT_VERSION = "0.0.0+dev"
_USER_AGENT = f"gecko-surf/{_CLIENT_VERSION} (+https://geckovision.tech)"

# Injected seams (defaults wired in the CLI).
Post = Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]]
Prompt = Callable[[str], str]
Store = Callable[[CredentialRef, str], bool]


class HeaderPost(Protocol):
    """Low-level JSON POST that also accepts extra request headers.

    ``_default_post`` is the canonical implementation. A provider that needs custom request
    headers (e.g. Privy's ``privy-app-id`` / ``privy-client``) layers them on top of this
    seam, keeping the SSRF guard + urllib plumbing in one place. Injected in tests so header
    construction is falsifiable with zero network.
    """

    def __call__(
        self, url: str, body: dict[str, Any], *, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]: ...


class LoginError(Exception):
    """A recoverable login failure (bad email, wrong code, unreachable/failed provider).

    MUST NEVER contain a token, an OTP, or a raw provider response body — redact-before-raise.
    The leak suite asserts this.
    """


@dataclass(frozen=True)
class AuthResult:
    """The outcome of a successful ``verify_code``: a secret token + a non-secret identity.

    ``token`` is a SECRET (sealed in the keychain, never printed/written/logged).
    ``identity`` holds only NON-SECRET references written verbatim to ``identity.json``
    (e.g. ``{"email", "issuer", "privy_user_id"}``) — a provider must never place a secret
    here. ``enrolled_at`` is added by :func:`_write_identity`.
    """

    token: str
    identity: dict[str, Any]


@runtime_checkable
class IdentityProvider(Protocol):
    """How to run one email→OTP enrollment. Both methods raise :class:`LoginError` (redacted)
    on failure; neither ever logs or echoes a token, OTP, or raw response body."""

    def send_code(self, email: str) -> None:
        """Ask the provider to send a one-time code to ``email`` (the 'init' step)."""
        ...

    def verify_code(self, email: str, otp: str) -> AuthResult:
        """Exchange ``otp`` for an :class:`AuthResult` (the 'authenticate' step)."""
        ...


def _default_post(
    url: str, body: dict[str, Any], *, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    """SSRF-validated JSON POST. Returns ``(status, parsed_json_or_empty)``.

    ``headers`` layers extra request headers over ``Content-Type: application/json`` (a
    provider passes its client/app headers here). The parsed body is returned as-is to the
    caller, which is responsible for never echoing it (it may carry a token on success).
    """
    validate_public_url(
        url
    )  # blocks private/loopback/link-local/non-http, per invariant
    data = json.dumps(body).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "User-Agent": _USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (validated)
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a JSON body
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _write_identity(home: Path, identity: dict[str, Any]) -> Path:
    """Persist NON-SECRET identity references (never the token). ``home`` is the config dir.

    ``identity`` comes straight from :class:`AuthResult` (a provider must place no secret in
    it); ``enrolled_at`` is stamped here. Writes ``identity.json`` and returns its path.
    """
    home.mkdir(parents=True, exist_ok=True)
    path = home / "identity.json"
    record = {**identity, "enrolled_at": int(time.time())}
    path.write_text(
        json.dumps(record, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_identity(home: Path) -> dict[str, Any] | None:
    """Read the non-secret identity references, or ``None`` if not logged in."""
    path = home / "identity.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run_login(
    email: str,
    *,
    provider: IdentityProvider,
    prompt: Prompt,
    store: Store,
    home: Path,
) -> int:
    """Provider-agnostic enrollment: validate → send code → prompt → verify → seal → record.

    Returns 0 on success. Raises :class:`LoginError` (never leaking a token/OTP/response
    body) on failure. The token is sealed BEFORE the identity file is written, so a keychain
    that cannot seal leaves nothing on disk.
    """
    email = email.strip()
    if "@" not in email or len(email) > 254:
        raise LoginError("enter a valid email address")

    provider.send_code(email)
    print(f"  → a one-time code was sent to {email} (if it's registered).")

    otp = prompt("Enter the code: ").strip()
    if not otp:
        raise LoginError("no code entered")

    result = provider.verify_code(email, otp)

    sealed = store(
        IDENTITY_REF, result.token
    )  # seal the secret; never printed or written to a file
    if not sealed:
        # Login SUCCEEDED — the key was minted server-side and returned once. The
        # keychain could not seal it (not installed, locked, or an unsigned frozen
        # binary that macOS blocks with errSecInteractionNotAllowed). Crashing here
        # would LOSE a valid key (it is returned exactly once); re-login just mints
        # another dangling record. So we degrade to the documented env fallback: show
        # the key ONCE with the exact export line `gecko connect` reads. This is a
        # deliberate one-time display on the user's own terminal, not a log.
        _write_identity(home, result.identity)
        print(
            f"  ✓ logged in as {email}, but the OS keychain could not seal the token.\n"
            "  Your identity key (shown ONCE — copy it now, it is never stored):\n\n"
            f"      {result.token}\n\n"
            "  Use it without a keychain by exporting it (or set it in your MCP client's\n"
            "  env block), and `gecko connect` will read it:\n\n"
            f"      export {_env_key(IDENTITY_REF)}={result.token}\n"
        )
        return 0
    path = _write_identity(home, result.identity)
    print(f"  ✓ logged in as {email} — identity token sealed in the OS keychain")
    print(f"  ✓ non-secret identity recorded at {path}")
    return 0


@dataclass
class RegistryProvider:
    """Our own registry's OTP endpoint: ``POST /registry/keys`` → code →
    ``POST /registry/keys/verify`` → ``{"key": ...}``. Uses the injected ``post`` seam."""

    registry_url: str
    post: Post

    def _base(self) -> str:
        return self.registry_url.rstrip("/")

    def send_code(self, email: str) -> None:
        try:
            # The server always answers 202 "code_sent_if_valid" (never reveals throttling).
            status, _ = self.post(f"{self._base()}/registry/keys", {"email": email})
        except UnsafeUrlError as exc:
            raise LoginError(f"refusing unsafe registry URL: {exc}") from exc
        if status not in (200, 202):
            raise LoginError(f"could not request a code (registry status {status})")

    def verify_code(self, email: str, otp: str) -> AuthResult:
        try:
            status, body = self.post(
                f"{self._base()}/registry/keys/verify", {"email": email, "otp": otp}
            )
        except UnsafeUrlError as exc:
            raise LoginError(f"refusing unsafe registry URL: {exc}") from exc
        if status != 200 or not body.get("key"):
            raise LoginError("invalid or expired code — run `gecko login` again")
        return AuthResult(
            token=str(body["key"]),
            identity={"email": email, "registry": self._base(), "issuer": "registry"},
        )


def login(
    email: str,
    *,
    registry_url: str,
    post: Post,
    prompt: Prompt,
    store: Store,
    home: Path,
) -> int:
    """Run the email→OTP enrollment against our own registry; seal the key; record identity.

    Thin wrapper over :func:`run_login` with a :class:`RegistryProvider`. Kept as a stable
    entry point (signature unchanged). Returns 0 on success; raises :class:`LoginError`.
    """
    provider = RegistryProvider(registry_url=registry_url, post=post)
    return run_login(email, provider=provider, prompt=prompt, store=store, home=home)

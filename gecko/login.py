"""`gecko login` — hosted-identity enrollment (email → OTP → sealed credential).

Drives the registry's OTP flow — ``POST /registry/keys {email}`` → a one-time code →
``POST /registry/keys/verify {email, otp}`` → an identity key — then seals the key in the
OS keychain and records a NON-SECRET identity reference in ``~/.gecko/identity.json``.

Why this exists: local ``gecko add`` (recorded, $0) stays **zero-login** — it's the wedge.
``gecko login`` gates only the HOSTED plane (attribution, rate-limit, hosted features), so
we can finally tell real users from crawlers. The credential lives in the keychain and never
in the config file; the identity file holds only the email + registry host.

Every side effect is an injected seam so the whole flow is falsifiable offline: ``post``
(HTTP), ``prompt`` (OTP entry), ``store`` (keychain), ``home`` (config dir). The real wiring
lives in ``cli._cmd_login``.

Security: the key is a secret — it is sealed, never printed, never written to a file, never
logged. The OTP is a one-time code (not a durable secret) but is likewise never persisted.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .credentials import CredentialRef
from .netguard import UnsafeUrlError, validate_public_url

#: The keychain slot the hosted-identity key seals into (sibling of a provider key).
IDENTITY_REF = CredentialRef(api="gecko-identity")

# Injected seams (defaults wired in the CLI).
Post = Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]]
Prompt = Callable[[str], str]
Store = Callable[[CredentialRef, str], bool]


class LoginError(Exception):
    """A recoverable login failure (bad email, wrong code, unreachable registry)."""


def _default_post(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """SSRF-validated JSON POST. Returns ``(status, parsed_json_or_empty)``."""
    validate_public_url(
        url
    )  # blocks private/loopback/link-local/non-http, per invariant
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
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


def _write_identity(home: Path, email: str, registry_url: str) -> Path:
    """Persist NON-SECRET identity references (never the key). ``home`` is the config dir."""
    home.mkdir(parents=True, exist_ok=True)
    path = home / "identity.json"
    path.write_text(
        json.dumps(
            {"email": email, "registry": registry_url, "enrolled_at": int(time.time())},
            indent=2,
        )
        + "\n",
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


def login(
    email: str,
    *,
    registry_url: str,
    post: Post,
    prompt: Prompt,
    store: Store,
    home: Path,
) -> int:
    """Run the email→OTP enrollment; seal the key; record the non-secret identity.

    Returns 0 on success. Raises ``LoginError`` (never leaking the key) on failure.
    """
    email = email.strip()
    if "@" not in email or len(email) > 254:
        raise LoginError("enter a valid email address")

    base = registry_url.rstrip("/")
    try:
        # The server always answers 202 "code_sent_if_valid" (never reveals throttling).
        status, _ = post(f"{base}/registry/keys", {"email": email})
    except UnsafeUrlError as exc:
        raise LoginError(f"refusing unsafe registry URL: {exc}") from exc
    if status not in (200, 202):
        raise LoginError(f"could not request a code (registry status {status})")
    print(f"  → a one-time code was sent to {email} (if it's registered).")

    otp = prompt("Enter the code: ").strip()
    if not otp:
        raise LoginError("no code entered")

    status, body = post(f"{base}/registry/keys/verify", {"email": email, "otp": otp})
    if status != 200 or not body.get("key"):
        raise LoginError("invalid or expired code — run `gecko login` again")
    key = str(body["key"])

    sealed = store(
        IDENTITY_REF, key
    )  # seal the secret; never printed or written to a file
    if not sealed:
        raise LoginError(
            "logged in, but no OS keychain is available to seal the identity key "
            "(install it: pip install 'gecko-surf[credentials]')"
        )
    path = _write_identity(home, email, base)
    print(f"  ✓ logged in as {email} — identity key sealed in the OS keychain")
    print(f"  ✓ non-secret identity recorded at {path}")
    return 0

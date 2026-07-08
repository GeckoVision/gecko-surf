"""Gecko key issuance: agent-native email OTP -> ``gk_live_`` key.

No dashboard, no human on our side. The plaintext key is returned exactly
once; only a salted hash is stored (it is OUR credential — invariant #1
concerns third-party secrets and payloads). Collections are duck-typed so
tests run against an in-memory fake and prod against Mongo.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from typing import Any

OTP_TTL_SECONDS = 600
OTP_MAX_ATTEMPTS = 3
OTP_MAX_PER_HOUR = 3

Mailer = Callable[[str, str], None]


class RegistryAuthError(Exception):
    """Raised on failed/expired/over-limit OTP or key verification.

    Messages never contain a key or a code."""


def _hash(plain: str, salt: str) -> str:
    return hashlib.sha256((salt + plain).encode("utf-8")).hexdigest()


class KeyStore:
    def __init__(
        self,
        keys_collection: Any,
        otp_collection: Any,
        mailer: Mailer,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._keys = keys_collection
        self._otps = otp_collection
        self._mail = mailer
        self._now = clock

    def start_otp(self, email: str) -> None:
        email = email.strip().lower()
        now = self._now()
        recent = self._otps.count_documents(
            {"email": email, "created": {"$gte": now - 3600}}
        )
        if recent >= OTP_MAX_PER_HOUR:
            raise RegistryAuthError("too many codes requested; try again later")
        # Supersede any still-active OTP for this email so a stale doc can't
        # win an unordered `find_one` and lock out the fresh code. Mark them
        # used rather than delete: deleting would undercount the rate limit
        # above (which counts by `created`, including exhausted docs).
        self._otps.update_many(
            {"email": email, "used": False}, {"$set": {"used": True}}
        )
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(16)
        self._otps.insert_one(
            {
                "email": email,
                "code_hash": _hash(code, salt),
                "salt": salt,
                "created": now,
                "attempts": 0,
                "used": False,
            }
        )
        self._mail(email, code)

    def verify_otp(self, email: str, otp: str) -> str:
        email = email.strip().lower()
        now = self._now()
        doc = self._otps.find_one({"email": email, "used": False})
        if doc is None:
            raise RegistryAuthError("no active code for this email")
        expired = now - doc["created"] > OTP_TTL_SECONDS
        exhausted = doc["attempts"] >= OTP_MAX_ATTEMPTS
        if expired or exhausted:
            raise RegistryAuthError("code expired; request a new one")
        if not secrets.compare_digest(_hash(otp, doc["salt"]), doc["code_hash"]):
            self._otps.update_one(
                {"email": email, "code_hash": doc["code_hash"]},
                {"$inc": {"attempts": 1}},
            )
            raise RegistryAuthError("wrong code")
        self._otps.update_one(
            {"email": email, "code_hash": doc["code_hash"]}, {"$set": {"used": True}}
        )
        plain = f"gk_live_{secrets.token_urlsafe(32)}"
        salt = secrets.token_hex(16)
        self._keys.insert_one(
            {
                "key_id": f"gkid_{secrets.token_hex(8)}",
                "email": email,
                "salt": salt,
                "hash": _hash(plain, salt),
                "surfaces": [],  # flat per-surface entitlement, granted later
                "created": now,
            }
        )
        return plain

    def check(self, plain_key: str) -> dict[str, Any] | None:
        """Constant-work verify: walk stored docs, compare salted hashes.

        Can't query by hash without the salt, so this scans all issued
        keys. Collections are small at v1 scale; revisit if that changes.
        """
        for stored in self._keys.find({}):
            if secrets.compare_digest(_hash(plain_key, stored["salt"]), stored["hash"]):
                return {k: v for k, v in stored.items() if k not in ("hash", "salt")}
        return None

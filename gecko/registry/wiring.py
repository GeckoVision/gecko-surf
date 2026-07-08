"""Env-driven wiring for the hosted registry: Mongo keys + SES OTP mail.

Fails SOFT: missing env disables issuance (503 on the endpoints) rather than
crashing the multi-surface server. Never logs URIs or key material.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .keys import KeyStore, Mailer

logger = logging.getLogger("gecko.registry")


def _ses_mailer(sender: str) -> Mailer:
    # Lazy import: boto3 is NOT a declared dependency (not in pyproject.toml's
    # `events` extra, not installed in the Docker image today) despite being
    # documented as "present in the hosted image" — keeping the import here,
    # inside the try/except in `build_keystore_from_env`, means a missing
    # boto3 fails soft (issuance disabled) instead of crashing import of this
    # module for local/dev/OSS installs that never set GECKO_OTP_FROM.
    import boto3  # type: ignore[import-not-found]  # optional dep, present in the hosted image

    ses = boto3.client("ses")

    def send(email: str, code: str) -> None:
        ses.send_email(
            Source=sender,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": "Your Gecko code"},
                "Body": {
                    "Text": {
                        "Data": (
                            f"Your Gecko verification code is {code}. "
                            "It expires in 10 minutes. An agent you run "
                            "requested a Gecko key for this email."
                        )
                    }
                },
            },
        )

    return send


def build_keystore_from_env() -> KeyStore | None:
    uri = os.environ.get("MONGODB_URI")
    sender = os.environ.get("GECKO_OTP_FROM")
    if not uri or not sender:
        if uri and not sender:
            logger.warning("registry: GECKO_OTP_FROM unset — key issuance disabled")
        return None
    try:
        from pymongo import MongoClient

        db: Any = MongoClient(uri, serverSelectionTimeoutMS=2000)["gecko_registry"]
        # Belt-and-braces TTL index over the in-logic 600s OTP expiry (keys.py
        # OTP_TTL_SECONDS). Best-effort only: Mongo TTL indexes expire BSON
        # dates, but our OTP docs store `created` as a float epoch (see
        # keys.py), so this index will NOT auto-expire existing docs until
        # `created` migrates to a BSON date. The in-logic 600s TTL remains
        # the enforced bound; this index is advisory/future-proofing.
        db["otps"].create_index("created", expireAfterSeconds=3600)
        return KeyStore(
            keys_collection=db["keys"],
            otp_collection=db["otps"],
            mailer=_ses_mailer(sender),
        )
    except Exception:  # noqa: BLE001 - registry must not take the server down
        logger.warning("registry: keystore init failed (redacted)")
        return None

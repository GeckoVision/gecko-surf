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
        # NOTE: no TTL index created here — Mongo TTL only expires BSON dates and
        # OTP docs store float epochs, so it would be inert; and an eager
        # create_index would block server start on a slow Mongo. The in-logic
        # 600s TTL is the enforced bound; add the index via an ops script once
        # `created` migrates to BSON dates.
        return KeyStore(
            keys_collection=db["keys"],
            otp_collection=db["otps"],
            mailer=_ses_mailer(sender),
        )
    except Exception:  # noqa: BLE001 - registry must not take the server down
        logger.warning("registry: keystore init failed (redacted)")
        return None

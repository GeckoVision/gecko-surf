"""Env-driven wiring for the hosted registry: Mongo keys + SES OTP mail.

Fails SOFT: missing env disables issuance (503 on the endpoints) rather than
crashing the multi-surface server. Never logs URIs or key material.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .class_wallet_store import MongoClassWalletStore
from .class_wallets import ClassWalletRegistrar
from .keys import KeyStore, Mailer
from .wallets import MongoWalletDirectory

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


def build_wallet_directory_from_env() -> MongoWalletDirectory | None:
    """The hosted ``account_id -> wallet`` directory, or ``None`` when unconfigured.

    ``None`` is the KEYLESS configuration (roadmap mode A): with no directory, the buyer
    is whatever the caller supplied, which is correct for a surface that hands back
    unsigned bytes. Absence disables the lookup; it never turns a lookup into a guess.

    Fails soft on init only. Once a directory exists, a lookup that cannot be answered
    raises rather than reporting "no binding" — see :class:`.wallets.MongoWalletDirectory`.
    """
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        return None
    try:
        from pymongo import MongoClient

        db: Any = MongoClient(uri, serverSelectionTimeoutMS=2000)["gecko_registry"]
        return MongoWalletDirectory(collection=db["wallets"])
    except Exception:  # noqa: BLE001 - registry must not take the server down
        logger.warning("registry: wallet directory init failed (redacted)")
        return None


def build_class_wallets_from_env() -> ClassWalletRegistrar | None:
    """The class-wallet registrar, or ``None`` (routes answer 503) when unconfigured.

    Keys resolve through the same ``gecko_sk_`` registry the gated MCP mounts use
    (:func:`gecko.keyregistry.registry_from_env`); registrations live in
    ``gecko_registry.class_wallets``. Fails soft like the keystore: never logs the URI.
    """
    from gecko.keyregistry import registry_from_env

    uri = os.environ.get("MONGODB_URI")
    if not uri:
        return None
    registry = registry_from_env()
    if registry is None:
        return None
    try:
        from pymongo import MongoClient

        db: Any = MongoClient(uri, serverSelectionTimeoutMS=2000)["gecko_registry"]
        store = MongoClassWalletStore(
            wallets=db["class_wallets"], challenges=db["class_wallet_challenges"]
        )
        return ClassWalletRegistrar.from_key_registry(registry, store)
    except Exception:  # noqa: BLE001 - registry must not take the server down
        logger.warning("registry: class wallet store init failed (redacted)")
        return None

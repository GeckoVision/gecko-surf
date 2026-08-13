"""The hosted ``account_id -> wallet`` directory, backed by Mongo.

The persistent half of :mod:`gecko.wallet_binding`; the model, the protocol and the
in-memory implementation live there. Collections are duck-typed exactly like
:class:`gecko.registry.keys.KeyStore`, so the tests run against a dict-backed fake and
production against Mongo with one code path.

Reads only. Enrolment (writing a binding) is a separate act with its own authentication
and is not part of the call path — see :class:`gecko.wallet_binding.WalletDirectory`.

Control plane only: opaque ids and a public key. No email is read from the document even
if one is present in the collection, and no credential is stored here at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from gecko.wallet_binding import (
    WalletBinding,
    WalletBindingError,
    binding_from_document,
)

logger = logging.getLogger("gecko.registry")

__all__ = ["MongoWalletDirectory"]


@dataclass
class MongoWalletDirectory:
    """A :class:`~gecko.wallet_binding.WalletDirectory` over one Mongo collection.

    FAILS CLOSED, LOUDLY. A driver error becomes a
    :class:`~gecko.wallet_binding.WalletBindingError`, never ``None``: returning ``None``
    would make an unreachable database look like "this account has no wallet", which
    silently downgrades an account whose buyer must be looked up into one whose buyer is
    accepted from the caller. That is precisely the substitution this record exists to
    prevent.
    """

    collection: Any

    def wallet_for(self, account_id: str) -> WalletBinding | None:
        account_id = (account_id or "").strip()
        if not account_id:
            return None
        try:
            document = self.collection.find_one({"account_id": account_id})
        except Exception:  # noqa: BLE001 - any driver failure is one fact: no answer
            # Redacted by construction: a pymongo error can carry the connection URI (and
            # therefore credentials), so neither its message nor its traceback is chained.
            logger.warning("wallet directory lookup failed (redacted)")
            raise WalletBindingError("the wallet directory could not be read") from None
        if document is None:
            return None
        return binding_from_document(document)

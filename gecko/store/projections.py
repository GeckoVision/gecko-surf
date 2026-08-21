"""The ONLY writer into the outcome collection — allowlist-first, fail-closed.

No raw-insert path exists into ``eval_outcomes``. Every write goes through
:func:`record_call_outcome`, which serialises the outcome through
:func:`gecko.corpus.to_record` (the §1 allowlist) BEFORE the store ever sees it,
then attaches only three control-plane identifiers: ``run_id`` (a uuid for the
eval run), ``api_key_id`` (the OPAQUE HASH of a caller's key — never the
plaintext), and — carried from the outcome, never a parameter — ``source``
(derived from mode upstream, so a caller cannot relabel a faked recorded 200 as
observed).

The result: the store cannot hold a response payload, a param/body value, a
secret, or a wallet↔tx binding, because the only door checks the allowlist and
the allowlist has no field for any of them.
"""

from __future__ import annotations

from gecko.corpus import CallOutcome, to_record
from gecko.keyregistry import KEY_PREFIX

from .collections import Collection


class ProjectionError(Exception):
    """Raised when a write would violate the control-plane boundary. Fail closed."""


def record_call_outcome(
    collection: Collection,
    outcome: CallOutcome,
    *,
    run_id: str,
    api_key_id: str,
) -> dict[str, object]:
    """Serialise one :class:`~gecko.corpus.CallOutcome` and write it. Returns the row.

    ``api_key_id`` must be the HASH of a key, never a plaintext ``gecko_sk_`` —
    passing a plaintext secret is rejected here so a secret can never reach the
    store even by mistake. ``source`` is taken from the outcome, never accepted
    as an argument: it is derived from mode at the outcome boundary and must stay
    that way, or the observed-only score could be gamed.
    """
    if not run_id:
        raise ProjectionError(
            "run_id is required — an unattributed outcome is not gradable"
        )
    if not api_key_id:
        raise ProjectionError(
            "api_key_id is required — an unattributed outcome is not gradable"
        )
    if api_key_id.startswith(KEY_PREFIX):
        raise ProjectionError(
            "api_key_id looks like a PLAINTEXT key; pass its hash (keyregistry.hash_key) — "
            "the store never holds a secret"
        )

    record = to_record(
        outcome
    )  # enforces the §1 allowlist; fails closed on any extra key
    # Attach control-plane identifiers only. These are ids, not payloads, and
    # they are added AFTER the allowlist check so they cannot be a smuggling path
    # for outcome fields: the allowlist already rejected anything payload-shaped.
    record["run_id"] = run_id
    record["api_key_id"] = api_key_id
    collection.insert_one(record)
    return record

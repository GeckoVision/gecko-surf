"""The network vocabulary — ONE closed set, imported everywhere, never re-spelled.

Two modules name this concept: ``corpus`` (the persisted categorical axis of the
``simulated`` tier) and ``txbind`` (the signing gate). Two vocabularies for one concept
is how a receipt ends up categorised ``mainnet`` in one module and ``unknown`` in the
other, so there is exactly one declaration and it lives here. Both import it; neither
redeclares it.

**Two PROVENANCES, one vocabulary.** The members are shared; where a value came from is
not, and the difference decides what may be done with it:

* DERIVED — :func:`network_from_label` collapses an operator's free-text honesty caveat
  into a member. The corpus may use it: a ledger row is a categorical observation, and a
  slightly-wrong category is a slightly-wrong row.
* ASSERTED — a caller states which network it is pointed at. Only this may reach the
  signing gate.

**Prose is never a gate input.** The default label on every Receipt is literally
``"simulated (fork/RPC snapshot — not mainnet)"``, so ANY substring test for "mainnet"
matches a FORK receipt and inverts the gate into an approval. That is why
:func:`network_from_label` orders ``fork`` before ``mainnet``, and why the gate compares
the structured field instead of ever calling it.

**A network is asserted, never inferred.** Not from an RPC URL either — a fork proxy
answers at any hostname, and the URL is attacker-influenceable. Absent an explicit
assertion the value is :data:`UNKNOWN_NETWORK`, and unknown approves nothing.

**The catch-all is not approvable, even against itself.** :data:`APPROVABLE_NETWORKS` is
an EXPLICIT enumeration rather than "the set minus the members we currently consider
catch-alls": a gate that approves on "membership in the vocabulary plus equality" starts
approving the day a new fail-closed bucket is added to the vocabulary, which is precisely
when it should refuse. ``unknown`` is this module's bucket and ``other`` was the corpus's
older name for the same one; neither can ever be approvable, and
:data:`LEGACY_NETWORK_ALIASES` keeps rows written under the old spelling readable.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal, Mapping, cast, get_args

__all__ = [
    "APPROVABLE_NETWORKS",
    "CATCH_ALL_SPELLINGS",
    "LEGACY_NETWORK_ALIASES",
    "NETWORKS",
    "Network",
    "UNKNOWN_NETWORK",
    "coerce_network",
    "network_from_label",
]

#: The closed set of networks a simulation can have run against. ``unknown`` is the
#: fail-closed member: it is what you get when nobody said, and it approves nothing.
Network = Literal["mainnet", "devnet", "testnet", "fork", "unknown"]

#: The same set as a runtime frozenset, DERIVED from the ``Literal`` so the two cannot
#: drift. The corpus's closed-set write gate checks against this.
NETWORKS: frozenset[str] = frozenset(get_args(Network))

#: The member that claims nothing. Any default, anywhere, must be this one.
UNKNOWN_NETWORK: Network = "unknown"

#: Every spelling that means "we could not tell" — this module's ``unknown`` and the
#: corpus's older ``other``. Nothing in here may ever be approvable.
CATCH_ALL_SPELLINGS: frozenset[str] = frozenset({"unknown", "other"})

#: The ONLY values a signing gate may approve on, enumerated explicitly (see the module
#: docstring for why this is not computed as ``NETWORKS - CATCH_ALL_SPELLINGS``).
APPROVABLE_NETWORKS: frozenset[str] = frozenset(
    {"mainnet", "devnet", "testnet", "fork"}
)

#: Spellings written by earlier code, read back as their canonical member. ``other`` was
#: the corpus's name for the fail-closed bucket before this module existed; rows carrying
#: it are already on disk and must stay readable.
LEGACY_NETWORK_ALIASES: Mapping[str, Network] = MappingProxyType({"other": "unknown"})


def _check_vocabulary_is_coherent() -> None:
    """Fail at IMPORT if the sets ever disagree — never at a signing decision.

    Not an ``assert``: assertions vanish under ``python -O``, and this one is load-bearing
    (an approvable catch-all is the exact fail-open D2 closed).
    """
    if not APPROVABLE_NETWORKS < NETWORKS:
        raise RuntimeError(
            "APPROVABLE_NETWORKS must be a strict subset of NETWORKS — a network the "
            "vocabulary does not know cannot be approved"
        )
    if APPROVABLE_NETWORKS & CATCH_ALL_SPELLINGS:
        raise RuntimeError(
            "a catch-all network is approvable — 'we could not tell' would approve "
            "against itself, which is the D2 fail-open"
        )
    if UNKNOWN_NETWORK not in NETWORKS or UNKNOWN_NETWORK not in CATCH_ALL_SPELLINGS:
        raise RuntimeError("UNKNOWN_NETWORK is not the vocabulary's fail-closed member")


_check_vocabulary_is_coherent()


def coerce_network(value: object) -> Network:
    """Narrow an operator-supplied value (CLI flag, env var, persisted row) to a member.

    Fails CLOSED: anything unrecognised — a typo, a URL, ``None``, a non-string — is
    :data:`UNKNOWN_NETWORK`, which approves nothing. It never guesses from substrings;
    ``"https://api.mainnet-beta.solana.com"`` is not ``mainnet``, it is unknown, because
    a fork proxy answers at any hostname.
    """
    if isinstance(value, str):
        if value in NETWORKS:
            return cast("Network", value)
        alias = LEGACY_NETWORK_ALIASES.get(value)
        if alias is not None:
            return alias
    return UNKNOWN_NETWORK


def network_from_label(label: str | None) -> Network:
    """Collapse a free-text honesty label into a member — CORPUS/DISPLAY ONLY.

    NEVER call this from a signing gate. It reads prose an operator wrote, and prose
    about a fork routinely names mainnet ("surfpool fork (mainnet-backed — NOT mainnet)").
    That is why the order below is by SPECIFICITY rather than convenience: ``fork`` is
    decided before ``mainnet``, or every fork label reads as mainnet. Even done right, a
    label is a sentence, not an assertion.

    Fails CLOSED to ``unknown`` for anything unrecognised, including ``None``.
    """
    text = (label or "").lower()
    if "fork" in text:
        return "fork"
    if "devnet" in text:
        return "devnet"
    if "testnet" in text:
        return "testnet"
    if "mainnet" in text:
        return "mainnet"
    return UNKNOWN_NETWORK

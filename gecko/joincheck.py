"""Cross-domain join detection — a MEASUREMENT of derived-join precision, never a gate.

A derived `feeds` edge says "the value this operation returns is the value that operation
needs". When the two sides denote different *kinds of thing*, the join is wrong in the
worst way available: it type-checks, it validates, it executes, and it returns a
plausible number.

Birdeye is the worked case. Seven parameter components are named `address`, and their own
descriptions disagree:

    tokenAddressParam      "The address of the token contract."
    traderAddressParam     "The address of a trader."
    pairAddressParam       "The address of a pair contract"
    accountAddressParam    "The address of the account."

A join from a trader's wallet into a token-price call is provably wrong, and the proof is
in the provider's own text.

**This is a heuristic and must never become a trust decision.** Free-text descriptions are
provider-authored and untrusted; a classifier over them cannot mint or refuse an edge
without handing an untrusted party control of the trust ladder. What it CAN do is give us
a precision number against a real spec without waiting for human labels — a suspect count
we can watch move. Read it as evidence, act on it through the shipped ladder.

Deliberately conservative in one direction only: a domain is reported when the text names
one **unambiguously**, and `None` otherwise. An unrecognised description is never a
contradiction. False negatives are the acceptable failure here; a false positive would
call a correct join wrong and invite someone to loosen a real control to silence it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .graph import SurfaceGraph

#: Closed vocabulary of contrasting value domains, each with the phrases that name it
#: unambiguously. Small on purpose: every entry must be a phrase whose presence settles
#: the domain, not merely hints at it. `"address"` is absent for exactly that reason —
#: it is the collision, not the discriminator.
_DOMAIN_PHRASES: dict[str, tuple[str, ...]] = {
    "token": (
        "token contract",
        "token mint",
        "mint address",
        "meme token",
        "of a token",
    ),
    "pair": ("pair contract", "pool address", "liquidity pool", "of a pair"),
    "actor": ("trader", "wallet", "the account", "an account", "owner", "holder"),
    "transaction": ("transaction hash", "signature", "tx hash"),
}

#: Domains that may never be joined to each other. Anything not listed is unconstrained —
#: absence of a rule is not evidence of compatibility, and this table only ever ADDS
#: suspicion.
_INCOMPATIBLE: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"token", "actor"}),
        frozenset({"token", "pair"}),
        frozenset({"pair", "actor"}),
        frozenset({"transaction", "token"}),
        frozenset({"transaction", "actor"}),
    }
)


@dataclass(frozen=True)
class Suspect:
    """A derived join whose two sides name incompatible value domains."""

    src: str
    dst: str
    src_domain: str
    dst_domain: str
    join: str

    @property
    def reason(self) -> str:
        return (
            f"`{self.join}` is a {self.src_domain} on the producing side and a "
            f"{self.dst_domain} on the consuming side"
        )


def domain_of(description: str | None) -> str | None:
    """The value domain a description names, or ``None`` when it does not settle one.

    ``None`` is the common and correct answer. Most descriptions do not name a domain,
    and guessing one would manufacture contradictions out of prose.
    """
    if not description:
        return None
    text = re.sub(r"\s+", " ", description.strip().lower())
    hits = {
        domain
        for domain, phrases in _DOMAIN_PHRASES.items()
        if any(phrase in text for phrase in phrases)
    }
    # Two domains in one description settles nothing — "the wallet that owns the token"
    # is about both. Ambiguity resolves to None, never to a coin flip.
    return hits.pop() if len(hits) == 1 else None


def incompatible(a: str | None, b: str | None) -> bool:
    """True only when both domains are known AND the pair is explicitly incompatible."""
    if a is None or b is None or a == b:
        return False
    return frozenset({a, b}) in _INCOMPATIBLE


def cross_domain_joins(
    graph: SurfaceGraph, descriptions: dict[str, str]
) -> list[Suspect]:
    """Derived joins whose two sides name incompatible domains, deterministic order.

    ``descriptions`` maps a node id to the spec text for that param/field. It is passed
    in rather than read off the graph because the graph deliberately does not carry
    free-text — keeping untrusted prose out of the content-addressed structure is the
    point, and this check is the one consumer that needs it.
    """
    out: list[Suspect] = []
    for edge in graph.feeds_edges(high_only=False):
        src_domain = domain_of(descriptions.get(edge.src))
        dst_domain = domain_of(descriptions.get(edge.dst))
        if not incompatible(src_domain, dst_domain):
            continue
        assert src_domain and dst_domain  # narrowed by `incompatible`
        out.append(
            Suspect(
                src=edge.src,
                dst=edge.dst,
                src_domain=src_domain,
                dst_domain=dst_domain,
                join=edge.dst.rsplit(":", 1)[-1],
            )
        )
    return sorted(out, key=lambda s: (s.src, s.dst))


def precision(graph: SurfaceGraph, descriptions: dict[str, str]) -> tuple[int, int]:
    """``(suspect_count, considered_count)`` over joins where BOTH sides name a domain.

    The denominator is deliberately not every edge: an edge whose sides name no domain is
    not evidence either way, and counting it as correct would inflate the number. This
    measures precision *on the subset we can actually judge*, and the two integers are
    reported together so nobody can quote the ratio without the sample size.
    """
    considered = 0
    suspects = 0
    for edge in graph.feeds_edges(high_only=False):
        a = domain_of(descriptions.get(edge.src))
        b = domain_of(descriptions.get(edge.dst))
        if a is None or b is None:
            continue
        considered += 1
        if incompatible(a, b):
            suspects += 1
    return suspects, considered

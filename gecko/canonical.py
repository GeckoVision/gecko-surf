"""Value-domain → canonical-example registry (single source of truth).

When a param carries a DECLARED value-domain/entity (an ``x-gecko`` hint or a customer
confirmation) but ships NO ``example``, example synthesis (verify-docs ``--live``, the
validator) has nothing but a placeholder to fill — which 404s a real path/query op and
falsely REFUTES it. For a KNOWN value domain we instead fill a real, public, STABLE
representative of that domain, so the first call is correct and the op verifies.

Honesty (project law — do not cross): a canonical is ONLY ever a real, public, stable
representative that is spec/registry METADATA — never a response payload (invariant #1),
never a fabricated or guessed value. A value domain with no entry here has NO canonical,
so synthesis keeps today's placeholder and the verdict stays honestly UNVERIFIED/REFUTED.
We never invent a plausible-looking value for an unknown domain.

The entity vocabulary is the SAME one ``graph`` / ``hints`` / ``provider_matrix`` use — a
value-domain token (e.g. ``solana-token-mint``) normalized to ``graph._norm`` form
(separators stripped, lowercased → ``solanatokenmint``). This module only adds the
domain→canonical VALUE; it invents no parallel entity namespace. Adding an entity is one
line in :data:`_CANONICAL`.
"""

from __future__ import annotations

#: The JSON-Schema marker an agent-facing param schema carries when its value domain is
#: DECLARED — the SAME key ``hints.declared_entity_hints`` reads inline (``x-gecko-entity``).
#: ``tools._input_schema`` stamps it (gated on a registered canonical); ``sample.
#: example_from_schema`` reads it to fill the canonical.
ENTITY_SCHEMA_KEY = "x-gecko-entity"

#: USDC's mainnet SPL mint — a real, public, permanent representative of the Solana
#: token-mint value domain. Registry metadata, never a response payload (invariant #1).
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

#: USDC's ticker — the same real, public, permanent asset as :data:`USDC_MINT`, in the
#: symbol value domain a surface uses when it addresses assets by ticker rather than mint.
USDC_SYMBOL = "USDC"

#: entity token (normalized) -> canonical example VALUE. Keyed on the ``graph._norm`` form
#: so ``solana-token-mint``, ``solanatokenmint`` and ``SOLANA_TOKEN_MINT`` all resolve to
#: the one canonical. Add an entity by adding ONE line.
_CANONICAL: dict[str, str] = {
    "solanatokenmint": USDC_MINT,
    "solanatokensymbol": USDC_SYMBOL,
}


def _norm_entity(entity: str) -> str:
    """Mirror of ``graph._norm`` for entity tokens: strip separators, lowercase."""
    return entity.replace("_", "").replace("-", "").lower()


def canonical_example(entity: str | None) -> str | None:
    """The canonical example VALUE for a DECLARED value-domain entity, or ``None``.

    ``None`` when ``entity`` is falsy/non-string or the domain has no registered
    canonical — the honest "no canonical, keep the placeholder" path. Never guesses.
    """
    if not entity or not isinstance(entity, str):
        return None
    return _CANONICAL.get(_norm_entity(entity))

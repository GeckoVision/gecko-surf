"""What to DO about a program error — Gecko's knowledge, and labelled as ours.

TWO KINDS OF STATEMENT, and the whole point of this module is that they never merge.
`gecko/program_errors.py` names an error from the program's OWN table, so
`VectorLimitReached (6008)` may be attributed to the program. What that means for a caller
— "the store holds at most 20 products; delete one before adding" — is nowhere in the IDL.
It is ours: measured, curated, and fallible in a way the program's declaration is not.

So it travels in its own field with `source="gecko-overlay"`, and it is never folded into
a sentence that speaks for the program. A reader who trusts the program's word and doubts
ours must be able to act on that difference, and they cannot if we blur the two.
`gecko/lifecycle.py` sets the same precedent, refusing to sweep `ZeroValueNotAllowed` into
a state taxonomy because that would be "us inventing a taxonomy and presenting it as the
program's".

THE ENGINE STAYS AGNOSTIC. Nothing here is imported by the comprehension path as a special
case: the lookup is by `(program_id, code)`, so the engine asks "is there anything known
about this?" and never knows what a storefront is. Adding a program means adding data.

EVERY ENTRY MUST BE EARNED. An entry is admissible only if it was OBSERVED — a refusal we
actually triggered and diagnosed — never inferred from a name. `VectorLimitReached` sounds
like it could mean many things; it means twenty because a fork run refused the twenty-first
product on a store with zero purchases and every byte free.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Remediation", "remediation_for"]

LET_ME_BUY = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"


@dataclass(frozen=True)
class Remediation:
    """Gecko's advice about one program error. Never the program's voice."""

    program: str
    code: int
    guidance: str
    #: How we came to know it. Stated so a reader can weigh it.
    established_by: str
    source: str = "gecko-overlay"


#: Keyed by (program, code). Observed, not inferred — see the module docstring.
_OVERLAY: dict[tuple[str, int], tuple[str, str]] = {
    (LET_ME_BUY, 6001): (
        "a product with this exact name already exists. Names are byte-exact and "
        "case-sensitive, so 'Sparkling Water' and 'Sparkling water' are different "
        "products and only an identical name collides. There is no edit instruction: to "
        "change a price, delete the product and add it again.",
        "measured on a fork — both spellings were added to one store and coexisted, and "
        "re-adding an identical name reverted",
    ),
    (LET_ME_BUY, 6002): (
        "no product with this name is on the store. Read the store first: names are "
        "byte-exact, so a near-miss in spacing or case will not match.",
        "the program's own delete/purchase paths, which look the product up by name",
    ),
    (LET_ME_BUY, 6004): (
        "the signer is not this store's authority. The authority lives inside the store "
        "account — read it rather than assuming the payer holds it.",
        "observed against live mainnet: preparing add_product with a non-authority payer "
        "reverts here before any fee is paid",
    ),
    (LET_ME_BUY, 6008): (
        "the store already holds its maximum of 20 products. This is a COUNT, not a byte "
        "budget — the account is a fixed 3,681 bytes allocated at initialize and never "
        "reallocs, but free space does not buy another slot. Delete a product before "
        "adding one, and prefer add-only sequences: because there is no edit instruction, "
        "a plan that deletes first can strand a live store half-updated.",
        "measured on a fork twice — a store with 21 purchases and a virgin store with "
        "zero purchases and all 3,681 bytes free both refused the 21st product",
    ),
}


def remediation_for(program_id: str | None, code: int | None) -> Remediation | None:
    """Gecko's guidance for this error, or None when we have nothing earned to say."""
    if program_id is None or code is None:
        return None
    entry = _OVERLAY.get((program_id, code))
    if entry is None:
        return None
    guidance, established_by = entry
    return Remediation(
        program=program_id,
        code=code,
        guidance=guidance,
        established_by=established_by,
    )

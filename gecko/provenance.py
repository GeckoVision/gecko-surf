"""The provenance ladders — single source of truth (CLAUDE.md: shared Literals
live in one canonical module; every consumer imports from here).

Two closed ladders, one per surface kind, moved verbatim from their home
modules (values and semantics unchanged — this module is a re-pointing, not a
redesign):

**API surface** (:mod:`gecko.graph`, :mod:`gecko.correlate`):

- ``Provenance`` — where a graph fact came from: ``EXTRACTED`` (the spec states
  it) / ``DECLARED`` (an explicit entity hint) / ``INFERRED`` (derived, with a
  recorded basis) / ``CLAIMED`` (asserted by an untrusted docs source, not yet
  checked against reality).
- ``VerifyStatus`` — the verified-against-reality verdict a replay lifts onto an
  op: ``VERIFIED`` / ``REFUTED`` / ``UNVERIFIED``.
- ``CrossApiProvenance`` — correlate's closed sub-ladder for cross-API edges:
  ``DECLARED`` / ``INFERRED`` (cross-API is DECLARED-only for plan-eligibility).

**Program surface** (:mod:`gecko.orquestra_comprehend`, :mod:`gecko.find_start`):

- ``ProgramProvenanceTier`` — where a generated PDA recipe came from:
  ``extracted`` (the IDL) / ``recovered`` (program source) / ``manual`` (the
  explicit overlay). The ``flagged`` state (unresolved seed / unknown program)
  is an orthogonal bit carried alongside, never a tier.
- ``AccountProvenance`` — the tag on every account of a derive plan:
  ``extracted`` (straight off the surface) / ``recovered`` (source/overlay
  rescued what the IDL drops or hides, or a declared read recipe resolves it) /
  ``flagged`` (an honest gap — never dropped, never fabricated).

Both ladders are CLOSED: adding a value is a design decision (anti-poisoning
review), not a convenience edit.
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "Provenance",
    "VerifyStatus",
    "CrossApiProvenance",
    "ProgramProvenanceTier",
    "AccountProvenance",
]

# --- API surface ladder ---------------------------------------------------------
# CLAIMED = an op asserted by an untrusted docs source (Context7 / from-docs) that has
# NOT yet been checked against the live API. It is the entry point to the VAS verified-
# against-reality tier: a CLAIMED op is lifted to a VerifyVerdict once reality answers.
Provenance = Literal["EXTRACTED", "DECLARED", "INFERRED", "CLAIMED"]

# the verified-against-reality verdict a replay lifts onto a CLAIMED op (VAS-1).
VerifyStatus = Literal["VERIFIED", "REFUTED", "UNVERIFIED"]

# correlate's closed sub-ladder: cross-API edges are DECLARED or INFERRED — never
# EXTRACTED (a single spec cannot state a cross-API fact) and never CLAIMED.
CrossApiProvenance = Literal["DECLARED", "INFERRED"]

# --- program surface ladder -----------------------------------------------------
ProgramProvenanceTier = Literal["extracted", "recovered", "manual"]

AccountProvenance = Literal["extracted", "recovered", "flagged"]

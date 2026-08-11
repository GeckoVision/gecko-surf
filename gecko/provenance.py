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

**Chain/plan status** (:mod:`gecko.find_start`):

- ``ChainStatus`` — the verdict on a MULTI-INSTRUCTION plan, one level above the
  per-account ladder: ``ordered`` (every step placed, every link carried, and the
  chain-agreement axis explicitly AGREE) / ``unresolved`` (evaluated and refused —
  a seed-dependency cycle, a link that does not hold on both endpoints, or a chain
  the landed transactions contradict) / ``not_evaluated`` (the axis was never
  checked: no evidence supplied, or evidence that could not be parsed). It is a
  verdict rather than an origin ladder, which is why it sits beside
  :data:`VerifyStatus` rather than inside either origin ladder.

  ``unresolved`` and ``not_evaluated`` stay DISTINCT because they argue for
  different remediations ("we checked and it was wrong" vs "we did not check"),
  and collapsing them would record a refusal that never happened. Neither is the
  zero value: :class:`gecko.find_start.ChainPlan` gives ``status`` no default, so
  "nobody set it" cannot read as "checked and fine".

Both ladders are CLOSED: adding a value is a design decision (anti-poisoning
review), not a convenience edit. So is ``ChainStatus``.
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "Provenance",
    "VerifyStatus",
    "CrossApiProvenance",
    "ProgramProvenanceTier",
    "AccountProvenance",
    "ChainStatus",
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

# ``cross_surface`` is the fourth account origin and the only one that cannot come from
# any program artifact: an account supplied by a DIFFERENT surface at request time.
#
# Jupiter is the case that forced it. The program surface names 9 accounts for `route`;
# the instruction that actually lands carries 25, because the other 16 are the AMM legs
# of whichever route the aggregator's HTTP API picked at that instant. No IDL can hold
# them — not because the IDL is deficient, but because the value does not exist until an
# unrelated surface answers. Labelling them `extracted` would claim the program declared
# them; labelling them `recovered` would claim we derived them from source. Neither is
# true, and an account whose origin we cannot state honestly is exactly the kind of thing
# this ladder exists to prevent.
AccountProvenance = Literal["extracted", "recovered", "cross_surface", "flagged"]

# --- chain/plan status ------------------------------------------------------------
# The verdict on a multi-instruction plan. NOT a member of either ladder above: those
# are per-account and answer "where did this come from", while this answers "can this
# sequence be executed as given". Adding a member here is the same closed-vocabulary
# design decision (anti-poisoning review), not a convenience edit.
#
# ``not_evaluated`` is the FAIL-CLOSED member and must be reachable only by an explicit
# assignment: a consumer decides on ``status == "ordered"`` (a positive test), never on
# truthiness and never on a ``!=`` denylist — every member is a non-empty, truthy string.
ChainStatus = Literal["ordered", "unresolved", "not_evaluated"]

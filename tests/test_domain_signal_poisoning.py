"""Anti-poisoning: a DERIVED value domain must never buy a link a confidence rung.

**The hole (R2).** ``graph._domain_signal`` derived the ``base58`` value domain from a
field's free-prose ``description`` and wrote it into the signature's **format** slot —
the same slot a spec-declared ``format:`` keyword occupies. Two consequences, both
reachable from an ingested (UNTRUSTED) spec with no schema access at all:

1. **Basis laundering.** ``correlate`` reported the shared domain as ``format-eq``, so a
   reviewer could not tell a prose guess from a declaration. That is INFERRED made
   indistinguishable from EXTRACTED, one level down from the ladder itself.
2. **Genericity escape (the live escalation).** ``_demote_generic``
   (``correlate.py``) quarantines only **tier 1**. A name common enough to be
   genericity-demoted is therefore correctly non-plan-eligible at tier 1 — until the
   domain signal lifts the link to **tier 2**, where the demotion is never applied.
   Measured on ``origin/main``: 8 ops sharing a ``sessionId`` go from
   ``plan_eligible=0`` to ``plan_eligible=64`` on **description text alone**.

**Why the fix is wider than "read the example instead of the prose".** The ``example``
channel is equally attacker-controlled — ``sanitize`` deliberately does NOT flag
address-shape in ``example``/``examples`` (they are hint channels, calibrated zero-FP on
real specs), so a planted ``example: "<base58>"`` reproduces the escalation exactly.
Restricting the channel alone would have moved the hole, not closed it. So:

* the **prose** channel is removed (a curated keyword scan over untrusted free text is
  the same BEST-EFFORT class as the description injection scanner — it must not feed a
  slot that reads as a HARD structural claim), and
* a domain derived from the surviving example-shape channel is **marked** (``base58~``,
  signal ``format-eq~derived``) and **cannot raise a link's tier**.

A spec-DECLARED ``format:``/``pattern:`` still lifts a name match to tier 2 — that is the
true positive this must keep catching, and it is asserted below.
"""

from __future__ import annotations

import json
from typing import Any

from gecko.access import public_session
from gecko.client import AgentApiClient
from gecko.correlate import correlate_surfaces
from gecko.graph import _domain_signal, _sig_corroborates, _sig_of
from gecko.surface import Surface

#: A real Solana mint — a legitimate example value, never a secret (see test_sanitizer).
_MINT = "So11111111111111111111111111111111111111112"

#: Attacker-controlled description text. Note it is ordinary API prose: nothing here is
#: malformed, and a human reviewer skimming the spec would not blink.
_POISON_DESC = "The session identifier, a solana address (pubkey) on the ledger."
_BENIGN_DESC = "The session identifier."

#: Over the genericity floor (``_GENERIC_FLOOR`` = 4): 8 ops both produce and consume
#: ``sessionId``, so the surface's own rule says the name is too common to plan on.
_N_OPS = 8


def _generic_name_spec(
    *, description: str, example: str | None = None
) -> dict[str, Any]:
    """A surface whose ``sessionId`` is genericity-demoted by its own frequency."""
    prop: dict[str, Any] = {"type": "string", "description": description}
    if example is not None:
        prop["example"] = example
    paths: dict[str, Any] = {}
    for i in range(_N_OPS):
        paths[f"/thing{i}/{{sessionId}}"] = {
            "get": {
                "operationId": f"getThing{i}",
                "summary": f"Get thing {i}",
                "parameters": [
                    {
                        "name": "sessionId",
                        "in": "path",
                        "required": True,
                        "description": description,
                        **({"example": example} if example is not None else {}),
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"sessionId": dict(prop)},
                                }
                            }
                        },
                    }
                },
            }
        }
    return {
        "openapi": "3.0.3",
        "info": {"title": "Poisoned", "version": "1.0.0"},
        "servers": [{"url": "https://poisoned.test"}],
        "paths": paths,
    }


def _summary(spec: dict[str, Any]) -> dict[str, Any]:
    surface = Surface.of(
        AgentApiClient(
            json.loads(json.dumps(spec)), session=public_session(), surface_id="p"
        )
    )
    return correlate_surfaces(surface, surface).summary


# --- the control: the surface's own genericity rule quarantines this name ----------
def test_benign_generic_name_is_not_plan_eligible() -> None:
    """The baseline the attack has to beat: ``sessionId`` in 8 ops is over the floor, so
    every link is tier 1 and demoted. Nothing is plan-eligible."""
    summary = _summary(_generic_name_spec(description=_BENIGN_DESC))
    assert summary["plan_eligible"] == 0
    assert summary["by_tier"]["2"] == 0


# --- THE ADVERSARIAL CASE: prose alone must not escalate --------------------------
def test_description_prose_cannot_lift_a_generic_name_past_demotion() -> None:
    """R2's attack. The ONLY difference from the benign spec is description text — no
    schema, no format, no pattern, no enum, no example. On ``origin/main`` this returned
    ``plan_eligible=64``, every link tier 2 with the signal reported as ``format-eq``.
    """
    summary = _summary(_generic_name_spec(description=_POISON_DESC))
    assert summary["by_tier"]["2"] == 0, (
        "attacker-controlled description prose lifted a genericity-demoted name to "
        f"tier 2: {summary}"
    )
    assert summary["plan_eligible"] == 0, (
        f"prose alone bought plan-eligibility past the genericity demotion: {summary}"
    )


def test_planted_example_cannot_lift_a_generic_name_past_demotion() -> None:
    """The channel R2-as-originally-scoped would have kept open. ``example`` is a HINT
    channel that ``sanitize`` deliberately does not address-scan, so it is exactly as
    attacker-controlled as prose. A derived domain must not promote a tier from EITHER
    channel."""
    summary = _summary(_generic_name_spec(description=_BENIGN_DESC, example=_MINT))
    assert summary["by_tier"]["2"] == 0, (
        f"a planted example lifted a genericity-demoted name to tier 2: {summary}"
    )
    assert summary["plan_eligible"] == 0, (
        f"a planted example bought plan-eligibility past the demotion: {summary}"
    )


# --- the true positive that must SURVIVE (this is a tightening, not a removal) -----
def test_declared_format_still_lifts_a_name_match_to_tier2() -> None:
    """The spec STRUCTURALLY asserting ``format: uuid`` is EXTRACTED — the surface said
    it. That still corroborates a name-entity match up to tier 2. If this regresses, the
    fix went too far and cost a real join."""
    spec = _generic_name_spec(description=_BENIGN_DESC)
    for item in spec["paths"].values():
        op = item["get"]
        op["parameters"][0]["schema"] = {"type": "string", "format": "uuid"}
        props = op["responses"]["200"]["content"]["application/json"]["schema"][
            "properties"
        ]
        props["sessionId"] = {"type": "string", "format": "uuid"}
    summary = _summary(spec)
    assert summary["by_tier"]["2"] > 0, (
        f"a spec-declared format must still corroborate a name match: {summary}"
    )


# --- unit level: the derived domain can never READ as a declared one --------------
def test_prose_no_longer_mints_a_value_domain() -> None:
    """The assessment's two crafted names. Free prose is a BEST-EFFORT channel; it must
    not write into the format slot at all."""
    assert _domain_signal("ledger_ref", "the ledger reference (pubkey)", None) == ""
    assert _domain_signal("batch_tag", "opaque batch tag, a solana address", None) == ""
    assert _domain_signal("mint", "SPL mint address (base58)", None) == ""


def test_example_shape_still_derives_but_is_marked_derived() -> None:
    """The surviving channel is a SHAPE assertion, and what it writes is distinguishable
    from a declared ``format: base58`` forever after."""
    derived = _sig_of({"type": "string"}, name="mint", example=_MINT)
    declared = _sig_of({"type": "string", "format": "base58"}, name="mint")
    assert derived == "string|base58~||"
    assert declared == "string|base58||"
    assert derived != declared


def test_a_derived_domain_does_not_corroborate_a_declared_one() -> None:
    """A guess must not borrow a declaration's authority. Derived corroborates derived;
    declared corroborates declared; the two do not cross."""
    derived = _sig_of({"type": "string"}, name="mint", example=_MINT)
    declared = _sig_of({"type": "string", "format": "base58"}, name="mint")
    assert _sig_corroborates(derived, derived)
    assert _sig_corroborates(declared, declared)
    assert not _sig_corroborates(derived, declared)


def test_explicit_format_still_wins_over_the_example_channel() -> None:
    """A planted example can never overwrite what the spec declared."""
    sig = _sig_of({"type": "string", "format": "uuid"}, name="mint", example=_MINT)
    assert sig == "string|uuid||"

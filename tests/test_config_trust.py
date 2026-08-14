"""Whose word is a config? The fail-open that arms the moment a provider's config loads.

Today every config is OURS — packaged in the wheel, written from an IDL we read — so "the
config holds a PDA node for this account" is real evidence and `extracted` is earned. The
architecture pass ruled that providers will HOST their own config and we will fetch it.
The instant that path exists, the same sentence becomes "the provider asserted this", and
nothing in the reader could tell the two apart: `_apply_origin_cap` is demotion-only, so an
account with no `pda_origins` entry keeps whatever the mechanics computed, and a config
supplying `pdas` while omitting `pda_origins` entirely would earn the top tier by saying
nothing at all.

The fix is NOT in `_apply_origin_cap` or `_account_step` — both are correct for the
configs they were written for, and changing them would demote our own artifacts and
rightly turn a pinned test red. It is a **trust mode on the loader**: a config we did not
author caps at `flagged` until something independent verifies it. `flagged` is not an
insult here, it is the honest word — we have not checked, and the ladder exists to say so.

The default is `external`, so a future call site that forgets to think about this gets the
safe answer. You have to ASK for the confident one.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from gecko.provider_config import (
    ConfigError,
    api_config_from_dict,
    load_packaged_provider,
)

#: A minimal program config in the packaged shape: two PDAs, no `pda_origins` at all.
#: That omission is the whole point — it is what a provider's first config will look like.
_SILENT: dict[str, Any] = {
    "api_id": "acme_swap",
    "kind": "program",
    "program": {
        "program_id": "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya",
        "pdas": {
            "pool": {
                "program_id": "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya",
                "seeds": [{"kind": "constant", "value": "pool", "encoding": "utf8"}],
            },
            "vault": {
                "program_id": "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya",
                "seeds": [{"kind": "constant", "value": "vault", "encoding": "utf8"}],
            },
        },
        "intents": [],
    },
}


def _with_origins(origins: Any) -> dict[str, Any]:
    data = copy.deepcopy(_SILENT)
    data["program"]["pda_origins"] = origins
    return data


def _program(data: dict[str, Any], **kwargs: Any) -> Any:
    config = api_config_from_dict(data, **kwargs)
    assert config.program is not None
    return config.program


# --- our own configs are unchanged -------------------------------------------


def test_a_packaged_config_still_asserts_nothing_by_omission() -> None:
    """The regression that matters most: `packaged` behaviour is EXACTLY as before. An
    empty origins map means the config caps nothing, and the mechanics decide."""
    assert _program(_SILENT, trust="packaged").pda_origins == {}


def test_the_packaged_loader_says_so_explicitly() -> None:
    """Not by default — by name. The one loader that has earned the confident answer is
    the one reading files out of our own wheel."""
    _, apis = load_packaged_provider("orquestra")

    program = apis["let_me_buy"].program
    assert program is not None
    assert program.trust == "packaged"


# --- a config we did not author ----------------------------------------------


def test_an_external_config_that_says_nothing_is_believed_about_nothing() -> None:
    """The fail-open itself. Silence used to be the strongest claim available: supply
    `pdas`, omit `pda_origins`, and every account came back `extracted`."""
    origins = _program(_SILENT, trust="external").pda_origins

    assert set(origins) == {"pool", "vault"}
    assert all(tier is None for tier in origins.values())


def test_external_is_the_default_because_forgetting_must_be_safe() -> None:
    origins = _program(_SILENT).pda_origins

    # Both halves, and the first one is not decoration: under `packaged` this map is
    # EMPTY, and `all(... for ... in {}.values())` is vacuously true — the assertion
    # below would have passed with the default flipped to the confident answer, which is
    # exactly the mutation it exists to catch.
    assert set(origins) == {"pool", "vault"}
    assert all(tier is None for tier in origins.values())


def test_a_provider_may_not_promote_its_own_config() -> None:
    """The obvious bypass, closed: declaring `extracted` in your own file is the same
    unverified assertion as declaring nothing, just louder. A tier is a thing we
    established, not a thing a file claims about itself."""
    origins = _program(
        _with_origins({"pool": "extracted"}), trust="external"
    ).pda_origins

    assert origins["pool"] is None


def test_a_provider_may_still_flag_itself() -> None:
    """Demotion is always believable — nobody talks their own claim down by mistake, and
    an honest gap declared upstream should not be argued with."""
    origins = _program(_with_origins({"pool": "manual"}), trust="external").pda_origins

    assert origins["pool"] is None  # capped at flagged, which is at or below `manual`


# --- what the agent is actually told -----------------------------------------


def test_the_cap_reaches_the_plan_and_says_why() -> None:
    """The loader capping is only half of it — the agent reads the PLAN. An unverified
    account must come back `flagged`, and the note must be the true reason.

    It may NOT borrow the packaged one ("the config asserts an origin outside the closed
    tier ladder"): a provider whose file simply stayed silent asserted no such thing, and
    telling an agent otherwise would be us writing a false sentence about somebody else's
    program — precisely the failure the ladder exists to prevent.
    """
    from gecko.find_start import DeriveStep, _apply_origin_cap

    step = DeriveStep(account="pool", provenance="extracted", note="")

    capped = _apply_origin_cap(step, {"pool": None}, "external")

    assert capped.provenance == "flagged"
    assert "not author" in capped.note and "verified" in capped.note
    assert "closed tier ladder" not in capped.note


def test_a_packaged_config_keeps_its_own_explanation() -> None:
    """The pre-existing meaning, unmoved: for OUR config a `None` entry really does mean
    a declared tier outside the ladder, and that is what the note must keep saying."""
    from gecko.find_start import DeriveStep, _apply_origin_cap

    step = DeriveStep(account="pool", provenance="extracted", note="")

    capped = _apply_origin_cap(step, {"pool": None}, "packaged")

    assert capped.provenance == "flagged"
    assert "closed tier ladder" in capped.note


def test_a_mechanical_flag_keeps_its_own_reason_under_either_trust() -> None:
    """An account already at the floor for a mechanical reason (an unresolved seed, a
    cycle) keeps that explanation. The trust cap can only ever lower a claim — it must
    never overwrite a more specific true statement with a more general one."""
    from gecko.find_start import DeriveStep, _apply_origin_cap

    step = DeriveStep(account="pool", provenance="flagged", note="seed cycle")

    assert _apply_origin_cap(step, {"pool": None}, "external").note == "seed cycle"


def test_an_unverified_account_says_flagged_in_the_purchase_plan_too() -> None:
    """`prepare_purchase` renders the same origins map for a human to read before signing.
    It used to emit the field only when the tier was truthy, so a capped account simply
    LOST its provenance line — "we have not verified this" rendered as "nothing to
    report", on the one screen where that difference decides a signature."""
    from gecko.prepare_purchase import _account_plan
    from gecko.provider_config import ProgramSpec

    program = ProgramSpec(
        program_id="BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya",
        pdas={},
        intents=(),
        pda_origins={"receipts": None, "signer": "extracted"},
    )

    plan = {
        entry["account"]: entry
        for entry in _account_plan(
            {"receipts": "A" * 32, "signer": "B" * 32, "mint": "C" * 32}, program
        )
    }

    assert plan["receipts"]["provenance"] == "flagged"
    assert plan["signer"]["provenance"] == "extracted"
    # An account the config never mentions is untouched — absence still means absence.
    assert "provenance" not in plan["mint"]


# --- validation is not weakened by the trust mode ----------------------------


def test_an_external_config_naming_an_unknown_account_is_still_refused() -> None:
    """Capping is not forgiving. A malformed or poisoned map is a loud refusal in both
    modes — otherwise 'we cap it anyway' would become the reason to stop reading it."""
    with pytest.raises(ConfigError, match="ghost"):
        api_config_from_dict(_with_origins({"ghost": "extracted"}), trust="external")


def test_a_non_object_origins_map_is_still_refused() -> None:
    with pytest.raises(ConfigError):
        api_config_from_dict(_with_origins(["pool"]), trust="external")

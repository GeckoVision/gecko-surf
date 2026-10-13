"""The registry's types, ids and merge rule. Pure; no store, no files."""

from __future__ import annotations

from typing import get_args

from gecko.anyquery import types as anyquery_types
from gecko.orthogonal.supplier import sources as supplier_sources
from gecko.servicemap import types as sm
from gecko.servicemap.ids import op_id, sighting_id, url_template
from gecko.servicemap.merge import merge_equivalence, merge_field
from gecko.servicemap.types import Equivalence, FieldObs

T0, T1, T2 = (
    "2026-10-08T00:00:00+00:00",
    "2026-10-09T00:00:00+00:00",
    "2026-10-10T00:00:00+00:00",
)


def _obs(evidence: sm.Evidence, at: str, **kw: object) -> FieldObs:
    base: dict[str, object] = {
        "op_id": "op",
        "path": "data.name",
        "json_type": None,
        "evidence": evidence,
        "claimed_by": (),
        "first_seen": at,
        "last_seen": at,
        "last_missing": None,
    }
    base.update(kw)
    return FieldObs(**base)  # type: ignore[arg-type]


# --- one definition of each shared type ------------------------------------------------


def test_catalog_has_one_definition_and_the_new_catalogs() -> None:
    assert anyquery_types.Catalog is sm.Catalog
    assert set(get_args(sm.Catalog)) >= {
        "orthogonal",
        "paysh",
        "public",
        "bazaar",
        "x402scan",
        "masterkey",
        "openapi",
    }


def test_equivalence_moved_and_the_supplier_map_still_uses_it() -> None:
    assert supplier_sources.Equivalence is sm.Equivalence
    assert supplier_sources.EvidenceLevel is sm.EvidenceLevel
    assert all(isinstance(e, sm.Equivalence) for e in supplier_sources.EQUIVALENCES)
    assert all(e.agreements == 0 for e in supplier_sources.EQUIVALENCES)


def test_every_evidence_value_has_a_rank_and_the_ladder_orders_them() -> None:
    assert set(sm.EVIDENCE_RANK) == set(get_args(sm.Evidence))
    rank = sm.EVIDENCE_RANK
    assert rank["CLAIMED"] < rank["DECLARED"] < rank["EXTRACTED"] < rank["VERIFIED"]


# --- ids ----------------------------------------------------------------------------------


def test_one_route_named_two_ways_is_one_operation() -> None:
    a = op_id("GET", url_template("coresignal", "/v2/collect/{company_id}"))
    b = op_id("get", url_template("CoreSignal", "/v2/collect/{profile_url}/"))
    c = op_id("GET", url_template("coresignal", "/v2/collect/:id?x=1"))
    assert a == b == c


def test_method_and_service_separate_operations() -> None:
    get = op_id("GET", url_template("svc", "/a"))
    assert get != op_id("POST", url_template("svc", "/a"))
    assert get != op_id("GET", url_template("other", "/a"))


def test_two_spellings_of_one_route_stay_two_sightings() -> None:
    assert sighting_id("orthogonal", "c", "GET", "/x/{a}") != sighting_id(
        "orthogonal", "c", "GET", "/x/{b}"
    )


# --- merge: claims never final, observations win ------------------------------------------


def test_an_observation_lifts_a_claim_in_either_order() -> None:
    claim = _obs("CLAIMED", T0, claimed_by=("orthogonal",))
    seen = _obs("EXTRACTED", T1)

    forward = merge_field(merge_field(None, claim), seen)
    backward = merge_field(merge_field(None, seen), claim)

    assert forward == backward
    assert forward.evidence == "EXTRACTED"
    assert forward.claimed_by == ("orthogonal",)
    assert (forward.first_seen, forward.last_seen) == (T0, T1)


def test_a_catalog_reimport_cannot_lower_an_observation_or_its_type() -> None:
    seen = _obs("EXTRACTED", T1, json_type="string")
    later_claim = _obs("CLAIMED", T2, json_type="number", claimed_by=("paysh",))

    merged = merge_field(seen, later_claim)

    assert merged.evidence == "EXTRACTED"
    assert merged.json_type == "string"
    assert merged.last_seen == T2


def test_a_missing_field_keeps_its_latest_miss() -> None:
    merged = merge_field(
        _obs("EXTRACTED", T0, last_missing=T2), _obs("EXTRACTED", T1, last_missing=T1)
    )
    assert merged.last_missing == T2


def test_an_equivalence_once_confirmed_stays_confirmed() -> None:
    confirmed = Equivalence("name", (("a", "x"), ("b", "y")), "confirmed", "live", 3)
    declared = Equivalence("name", (("a", "x"), ("b", "y")), "declared", "docs", 1)
    merged = merge_equivalence(confirmed, declared)
    assert merged.level == "confirmed" and merged.agreements == 3


def test_offer_obs_is_built_from_the_x402_offer_without_raw_or_memo() -> None:
    from gecko.servicemap.ids import offer_id
    from gecko.x402_svm import Offer

    offer = Offer(
        scheme="exact",
        network="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        asset="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        amount=10_000,
        pay_to="4Bf6zpkrv4gtirAzM3kzqXrRRLXNmbKrpHLebTpAk3Wg",
        max_timeout_seconds=60,
        fee_payer=None,
        memo="per-challenge",
        raw={"scheme": "exact"},
    )
    obs = sm.OfferObs.from_offer(offer, op_id="op", evidence="EXTRACTED", at=T1)
    assert obs.offer_id == offer_id("op", offer.network, offer.asset, offer.pay_to)
    assert (obs.amount, obs.pay_to, obs.first_seen, obs.last_seen) == (
        10_000,
        offer.pay_to,
        T1,
        T1,
    )
    assert not hasattr(obs, "memo") and not hasattr(obs, "raw")

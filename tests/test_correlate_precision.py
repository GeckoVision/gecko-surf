"""Correlation JOIN-KEY QUALITY — the precision half of §13 (graph assessment R1 + R8).

Two things live here, and they are deliberately in one file because one is worthless
without the other:

**R1 — the rules.** ``correlate``'s tier 2 is documented as "a shared discriminating
pattern/enum/format **corroborates a name match**" (``correlate.py`` module docstring).
It used to fire on the signature ALONE, with ``entity=None``, and return
``plan_eligible=True``. That inverts the discipline: a join key needs *identity*, and
the value domain is only ever a corroborator (charter invariant #5). The three cases
below are the real wrong-but-plausible links that shipped on real specs:

  * a mode ENUM (``chain_type`` -> ``chain_type``) — a bounded domain GUARANTEES
    collisions, so a shared enum is anti-evidence for a join, not evidence;
  * a ``date-time`` (``detected_at`` -> ``from``) — high-cardinality but names nothing;
    every API has twenty timestamps and none of them is a key;
  * a shared decimal PATTERN on two unrelated money amounts.

**R8 — the measurement.** Correlation precision on real specs was measured nowhere, and
nothing in CI would have caught the above. ``scripts/two_api_probe.py`` needs spec files
that are not committed, so the fixture here pins the summary over the specs the repo
DOES ship (``golden/privy_openapi.json``, ``pegana_openapi.json``). Any future widening
of a correlation rule now has to move these numbers in the same commit and say why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gecko.client import AgentApiClient
from gecko.correlate import CorrelationResult, correlate_surfaces
from gecko.surface import Surface

_FIXTURES = Path(__file__).parent / "fixtures"
_PRIVY = _FIXTURES / "golden" / "privy_openapi.json"
_PEGANA = _FIXTURES / "pegana_openapi.json"

# a base58-ish mint pattern: a genuinely discriminating value domain.
_MINT_PATTERN = "^[1-9A-HJ-NP-Za-km-z]{32,44}$"
# the decimal-string pattern a money amount carries (privy's real tier-2 offender).
_AMOUNT_PATTERN = r"^\d+(\.\d+)?$"
_CHAIN_ENUM = ["solana", "ethereum", "base"]


def _spec(
    *,
    field_name: str,
    field_schema: dict[str, Any],
    param_name: str,
    param_schema: dict[str, Any],
) -> dict[str, Any]:
    """One surface: ``produce`` emits ``field_name``; ``consume`` requires ``param_name``.

    Intra-API on purpose — the cross-API gate is already locked by
    ``tests/test_correlate.py``; what is under test here is what INTRA-API tier 2 admits.
    """
    return {
        "openapi": "3.0.0",
        "info": {"title": "One", "version": "1"},
        "servers": [{"url": "https://one.example.com"}],
        "paths": {
            "/produce": {
                "get": {
                    "operationId": "produce",
                    "summary": "Produce a record",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "title": "Record",
                                        "properties": {field_name: field_schema},
                                    }
                                }
                            }
                        }
                    },
                }
            },
            "/consume": {
                "get": {
                    "operationId": "consume",
                    "summary": "Consume a record",
                    "parameters": [
                        {
                            "name": param_name,
                            "in": "query",
                            "required": True,
                            "schema": param_schema,
                        }
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"ok": {"type": "boolean"}},
                                    }
                                }
                            }
                        }
                    },
                }
            },
        },
    }


def _correlate(spec: dict[str, Any]) -> CorrelationResult:
    surface = Surface.of(AgentApiClient(spec, surface_id="one"))
    return correlate_surfaces(surface, surface)


def _links(result: CorrelationResult, src_field: str, dst_param: str) -> list[Any]:
    return [
        lk
        for lk in result.links
        if lk.basis.src_field == src_field and lk.basis.dst_param == dst_param
    ]


# --- R1: the three wrong-but-plausible tier-2 shapes -----------------------------
def test_shared_enum_alone_is_not_a_join_key() -> None:
    """``archiveWallet.chain_type -> createWallet.chain_type`` (privy, real).

    A bounded domain guarantees collisions: every wallet op takes the SAME three chain
    values, so a shared enum tells you nothing about which value flows where. It is a
    mode flag, not a key. It must not be plan-eligible.
    """
    enum_schema = {"type": "string", "enum": _CHAIN_ENUM}
    result = _correlate(
        _spec(
            field_name="chain_type",
            field_schema=enum_schema,
            param_name="chain_type",
            param_schema=enum_schema,
        )
    )
    links = _links(result, "chain_type", "chain_type")
    assert not [lk for lk in links if lk.plan_eligible], (
        "a shared enum is anti-evidence for a join, never a plan-eligible basis: "
        f"{[lk.basis.to_dict() for lk in links]}"
    )


def test_shared_datetime_alone_is_not_a_join_key() -> None:
    """``list_alerts.detected_at -> csv.to`` (pegana, real).

    A ``date-time`` is high-cardinality and names nothing — the charter's "rarity is not
    distinctiveness" trap one level down. ``detected_at`` meeting a report's date range
    is not a key flowing into a key.
    """
    ts = {"type": "string", "format": "date-time"}
    result = _correlate(
        _spec(
            field_name="detected_at",
            field_schema=ts,
            param_name="from",
            param_schema=ts,
        )
    )
    links = _links(result, "detected_at", "from")
    assert not [lk for lk in links if lk.plan_eligible], (
        "a shared date-time is not a join basis: "
        f"{[lk.basis.to_dict() for lk in links]}"
    )


def test_shared_pattern_without_a_name_match_is_not_a_join_key() -> None:
    """``getEarnSummary.total_allocated_converted -> createCustomOrder.amount`` (privy).

    Two money quantities matching the same decimal-string pattern. The pattern is
    genuinely discriminating; what is missing is IDENTITY — neither name refers to the
    same entity, so there is nothing for the signature to corroborate.
    """
    amount = {"type": "string", "pattern": _AMOUNT_PATTERN}
    result = _correlate(
        _spec(
            field_name="total_allocated_converted",
            field_schema=amount,
            param_name="amount",
            param_schema=amount,
        )
    )
    links = _links(result, "total_allocated_converted", "amount")
    assert not [lk for lk in links if lk.plan_eligible], (
        "a signature with no name match is not a join basis: "
        f"{[lk.basis.to_dict() for lk in links]}"
    )


# --- R1: the corroborator must SURVIVE (this is a tightening, not a removal) ------
def test_signature_still_corroborates_a_real_name_match() -> None:
    """``tokenId -> tokenId`` on a shared mint pattern stays tier 2, plan-eligible.

    The signature keeps doing its one job: strengthening a name-entity match so the
    basis reads ``name-entity:token`` + ``pattern-eq``. If this regresses, R1 went too
    far and cost real recall.
    """
    mint = {"type": "string", "pattern": _MINT_PATTERN}
    result = _correlate(
        _spec(
            field_name="tokenId",
            field_schema=mint,
            param_name="tokenId",
            param_schema=mint,
        )
    )
    links = _links(result, "tokenId", "tokenId")
    assert links, "the real name+signature join must survive"
    link = links[0]
    assert link.basis.tier == 2 and link.plan_eligible
    assert link.basis.entity == "token"  # tier 2 always names the entity it joined on
    assert "name-entity:token" in link.basis.signals
    assert "pattern-eq" in link.basis.signals


def test_enum_plus_name_match_demotes_to_tier1_not_tier2() -> None:
    """A name-entity match carrying only a shared enum is tier 1, not tier 2.

    The name still carries the join (``walletId -> walletId``, intra-API, so still
    plan-eligible), but the enum must not buy it a confidence rung. Tier 2 means
    "corroborated", and an enum corroborates nothing.
    """
    enum_id = {"type": "string", "enum": ["wal_a", "wal_b", "wal_c"]}
    result = _correlate(
        _spec(
            field_name="walletId",
            field_schema=enum_id,
            param_name="walletId",
            param_schema=enum_id,
        )
    )
    links = _links(result, "walletId", "walletId")
    assert links, "the name-entity join itself must survive"
    link = links[0]
    assert link.basis.tier == 1, "an enum must not lift a name match to tier 2"
    assert link.basis.signals == ("name-entity:wallet",)
    assert "enum-eq" not in link.basis.signals


def test_tier2_always_names_the_entity_it_joined_on() -> None:
    """The structural invariant behind R1, asserted over a whole real spec.

    A tier-2 basis with ``entity=None`` is a link that cannot answer "what does it
    connect, on what basis" — the charter's "done" test. There must be none.
    """
    privy = Surface.of(AgentApiClient(str(_PRIVY), surface_id="privy"))
    orphans = [
        lk
        for lk in correlate_surfaces(privy, privy).links
        if lk.basis.tier == 2 and lk.basis.entity is None
    ]
    assert not orphans, f"{len(orphans)} tier-2 links with no entity"


# --- R8: the committed precision fixture -----------------------------------------
# Pinned over specs this repo already ships, so a rule change has to move a number.
# BEFORE R1 (2026-08-06): privy total 2918 / plan_eligible 498 / by_tier 3:0 2:479 1:19
#                         pegana total 71 / plan_eligible 68 / by_tier 3:0 2:59 1:9
# 95% of what "plan_eligible" reported was timestamps, money amounts and mode enums.
_EXPECTED = {
    "privy": {
        "total": 2439,
        "plan_eligible": 19,
        "candidates": 2420,
        "by_tier": {"3": 0, "2": 0, "1": 2439},
    },
    "pegana": {
        "total": 12,
        "plan_eligible": 9,
        "candidates": 3,
        "by_tier": {"3": 0, "2": 0, "1": 12},
    },
}


def _summary(path: Path, surface_id: str) -> dict[str, Any]:
    surface = Surface.of(AgentApiClient(str(path), surface_id=surface_id))
    return correlate_surfaces(surface, surface).summary


def test_privy_correlation_counts_are_pinned() -> None:
    """R8 — the regression fixture that would have caught R1's defect.

    If this number goes UP, a rule got wider: say what it newly admits, in the same
    commit. If it goes DOWN, say which real join was lost.
    """
    assert _summary(_PRIVY, "privy") == _EXPECTED["privy"]


def test_pegana_correlation_counts_are_pinned() -> None:
    assert _summary(_PEGANA, "pegana") == _EXPECTED["pegana"]


def test_plan_eligible_links_are_all_entity_named() -> None:
    """Every plan-eligible intra-API link on a real spec rests on a named entity.

    This is the precision claim in one assert: after R1 there is no plan-eligible link
    whose basis cannot say WHICH entity it joined on.
    """
    for path, sid in ((_PRIVY, "privy"), (_PEGANA, "pegana")):
        surface = Surface.of(AgentApiClient(str(path), surface_id=sid))
        for lk in correlate_surfaces(surface, surface).links:
            if lk.plan_eligible:
                assert lk.basis.entity, f"{sid}: unnamed plan-eligible {lk.to_dict()}"

"""The ingest gate, pinned against the eight programs we already ship.

Every number in this file was MEASURED against the packaged configs and the packaged
IDL fixtures — nothing here is invented, and no fixture is written to make a check pass.
The load-bearing row is whirlpool: its config declares ``intents: ["plan_swap"]``,
:func:`gecko.providers.cli.intent_registries` supplies no such thing, and the program
shipped anyway with no start card, no ``list_programs`` row and no drift target. If
whirlpool ever stops FAILING this gate without someone wiring ``plan_swap``, the gate has
stopped working.

The other seven are here so the gate is falsifiable in the other direction: a check that
refuses everything is not a gate either. Their outcomes span all four values, which is the
point — ``ok`` (pumpfun), ``warn`` (let_me_buy, meteora, metadao_ico, jurassic_fi, jupiter),
``refuse`` (ore, whirlpool) and ``unknown`` (the three programs with no offline IDL).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from gecko import ingest_gate
from gecko.ingest_gate import (
    CHECKS,
    DISCRIMINATION_CAVEAT,
    check_framework_fingerprint,
    gate,
)

FIXTURES = Path(__file__).parent / "fixtures"

#: The programs whose IDL is packaged in this repo. jupiter, whirlpool and jurassic_fi
#: are absent ON PURPOSE — their IDL is fetched live (gecko/orquestra_client.py:205), so
#: the IDL-backed checks must report `unknown` for them rather than `ok`.
PACKAGED_IDLS = {
    "let_me_buy": FIXTURES / "let_me_buy_idl.json",
    "ore": FIXTURES / "orquestra" / "6alwvs9936laepljczqumb" / "idl.json",
    "metadao_ico": FIXTURES / "orquestra" / "krhmrxpy2fgwn3q0whic7" / "idl.json",
    "meteora": FIXTURES / "orquestra" / "v48gsz901w84zriqe0elsl" / "idl.json",
    "pumpfun": FIXTURES / "orquestra" / "6i6q26bmm46b89xlxo1kv" / "idl.json",
}

ALL_PROGRAMS = (
    "let_me_buy",
    "jupiter",
    "meteora",
    "ore",
    "pumpfun",
    "metadao_ico",
    "jurassic_fi",
    "whirlpool",
)


def run(api_id: str):
    """The gate as a caller runs it: packaged config, packaged IDL when one exists."""
    path = PACKAGED_IDLS.get(api_id)
    return gate(api_id, idl=ingest_gate.load_idl(str(path)) if path else None)


@pytest.fixture(scope="module")
def reports():
    return {api_id: run(api_id) for api_id in ALL_PROGRAMS}


# --------------------------------------------------------------------------- #
# intent-reachability — the check that would have caught whirlpool
# --------------------------------------------------------------------------- #
def test_whirlpool_fails_intent_reachability_today(reports):
    """THE regression that shipped. whirlpool.json:12 declares plan_swap; nothing
    supplies it; both consumers drop it in silence."""
    check = reports["whirlpool"].check("intent-reachability")
    assert check.outcome == "refuse"
    assert check.blocks is True
    assert check.measured["declared"] == ["plan_swap"]
    assert check.measured["supplied"] == []
    assert check.measured["missing"] == ["plan_swap"]
    assert reports["whirlpool"].blocks is True


def test_whirlpool_refusal_names_the_file_the_missing_thing_and_the_fix(reports):
    """A refusal that says only 'failed' is a bug. This one has to name the registry
    that is missing the entry, the config that declares it, and the single edit."""
    (finding,) = reports["whirlpool"].check("intent-reachability").findings
    assert "gecko/providers/cli.py" in finding.location
    assert "intent_registries()" in finding.location
    assert "whirlpool.plan_swap" in finding.missing
    assert "gecko/providers/configs/orquestra/whirlpool.json" in finding.missing
    assert "intent_registries()" in finding.fix
    # the two places the orphan is dropped without a word
    assert "gecko/providers/cli.py:68" in finding.fix
    assert "gecko/find_start.py:966" in finding.fix


@pytest.mark.parametrize(
    "api_id, declared",
    [
        ("jupiter", ["plan_route"]),
        ("meteora", ["plan_swap"]),
        ("ore", ["plan_claim"]),
        ("pumpfun", ["plan_buy", "plan_sell"]),
        ("metadao_ico", ["plan_fund"]),
    ],
)
def test_wired_programs_pass_intent_reachability(reports, api_id, declared):
    """The five programs whose intents ARE supplied pass, with declared == supplied."""
    check = reports[api_id].check("intent-reachability")
    assert check.outcome == "ok"
    assert check.measured["declared"] == declared
    assert check.measured["supplied"] == declared
    assert check.measured["missing"] == []


@pytest.mark.parametrize("api_id", ["let_me_buy", "jurassic_fi"])
def test_derivation_only_programs_pass_vacuously(reports, api_id):
    """`"intents": []` is a deliberate declaration (comprehended for derivation only), so
    there is no orphan to find — but the vacuity is recorded, not hidden."""
    check = reports[api_id].check("intent-reachability")
    assert check.outcome == "ok"
    assert check.measured["vacuous"] is True
    assert check.measured["declared"] == []
    assert 'declares "intents": [] explicitly' in check.headline


def test_intent_reachability_reads_the_overlay_declaration_too():
    """whirlpool declares plan_swap TWICE — base config and overlay. A gate that read
    only one of them could be silenced by deleting the other."""
    from gecko.find_start import _packaged_overlay

    assert _packaged_overlay("whirlpool").get("intents") == ["plan_swap"]
    check = ingest_gate.check_intent_reachability(
        "whirlpool", (), {}, {}, overlay_declared=("plan_swap",)
    )
    assert check.outcome == "refuse"
    assert check.measured["missing"] == ["plan_swap"]


# --------------------------------------------------------------------------- #
# registry-consistency — six registries, no lockstep between them
# --------------------------------------------------------------------------- #
#: Measured: which of the six each program is in, and the score that falls out.
REGISTRY_SCORES = {
    "pumpfun": (6, []),
    "meteora": (6, []),
    "ore": (6, []),
    "jupiter": (5, ["R6"]),
    "metadao_ico": (5, ["R5"]),
    "let_me_buy": (4, ["R4", "R5"]),
    "jurassic_fi": (3, ["R4", "R5", "R6"]),
    "whirlpool": (2, ["R3", "R4", "R5", "R6"]),
}


@pytest.mark.parametrize("api_id, expected", sorted(REGISTRY_SCORES.items()))
def test_registry_consistency_spread(reports, api_id, expected):
    score, missing = expected
    check = reports[api_id].check("registry-consistency")
    assert check.measured["score"] == score
    assert sorted(k for k, v in check.measured["present"].items() if not v) == missing


def test_registry_consistency_discriminates(reports):
    """A check every program passes is a comment, not a gate: this one splits 8 programs
    across five distinct scores."""
    scores = {
        reports[api].check("registry-consistency").measured["score"]
        for api in ALL_PROGRAMS
    }
    assert scores == {2, 3, 4, 5, 6}


def test_golden_row_counts_are_read_from_the_packaged_golden_set(reports):
    """Measured against gecko/providers/configs/orquestra/find_start_golden.jsonl."""
    counts = {
        api: reports[api].check("registry-consistency").measured["golden_rows"]
        for api in ALL_PROGRAMS
    }
    assert counts == {
        "pumpfun": 12,
        "meteora": 6,
        "ore": 6,
        "let_me_buy": 6,
        "metadao_ico": 4,
        "jupiter": 0,
        "jurassic_fi": 0,
        "whirlpool": 0,
    }


def test_metadao_drift_key_near_miss_is_named(reports):
    """drift_watch keys ('metadao','fund') while the api_id is 'metadao_ico', so a target
    named metadao_ico raises WatchError. prove.py already carries BOTH spellings — the
    same bug, fixed in one dispatch and not the other. The finding has to say so."""
    check = reports["metadao_ico"].check("registry-consistency")
    assert check.measured["drift_keys"] == []
    assert check.measured["near_miss_drift_keys"] == [("metadao", "fund")]
    (finding,) = [f for f in check.findings if "drift_watch" in f.location]
    assert "gecko/prove.py" in finding.missing
    assert "metadao_ico" in finding.missing


def test_drift_dispatch_keys_are_parsed_from_source_not_called():
    """The dispatch routes to an orchestrator that opens an RPC connection, so the keys
    are read with ast. Pinned against the real table in gecko/drift_watch.py."""
    import gecko.drift_watch as drift_watch

    keys = ingest_gate._dispatch_keys(Path(drift_watch.__file__), "_default_simulator")
    assert keys == {
        ("pumpfun", "buy"),
        ("pumpfun", "sell"),
        ("meteora", "swap"),
        ("ore", "claim"),
        ("metadao", "fund"),
        ("jupiter", "route"),
    }


# --------------------------------------------------------------------------- #
# framework-fingerprint — the hardcoded 8
# --------------------------------------------------------------------------- #
def test_ore_refuses_because_its_instructions_are_one_byte(reports):
    """ore is a `steel` program: 15/15 instructions carry a ONE-byte discriminator, and
    idl_layout adds 8 unconditionally. That is the silent-wrong-offset case, so it
    refuses rather than warns."""
    check = reports["ore"].check("framework-fingerprint")
    assert check.outcome == "refuse"
    assert check.measured["framework"] == "origin=steel"
    assert check.measured["widths"]["instructions"] == [1]
    assert check.measured["widths"]["accounts"] == [8]
    (refusal,) = [f for f in check.findings if f.outcome == "refuse"]
    assert "ANCHOR_DISCRIMINATOR_LEN" in refusal.location
    assert "15 of 15 instructions" in refusal.missing
    assert "field_offset" in refusal.fix
    # and the seven inline structs field_offset cannot locate
    assert check.measured["accounts_inline"] == [
        "Automation",
        "Board",
        "Config",
        "Miner",
        "Round",
        "Stake",
        "Treasury",
    ]


def test_metadao_legacy_idl_warns_rather_than_refuses(reports):
    """A pre-0.30 Anchor IDL declares NO discriminator at all: 0/13 instructions, 0/4
    accounts, 0/11 events. account_discriminator refuses CLEANLY, so nothing decodes
    wrongly — it degrades (no memcmp enumeration) instead of breaking."""
    check = reports["metadao_ico"].check("framework-fingerprint")
    assert check.outcome == "warn"
    assert check.measured["framework"] == "legacy Anchor (no metadata.spec)"
    assert check.measured["accounts_without_discriminator"] == [
        "FundingRecord",
        "Launch",
        "OldFundingRecord",
        "OldLaunch",
    ]


@pytest.mark.parametrize("api_id", ["let_me_buy", "meteora", "pumpfun"])
def test_anchor_programs_pass_the_fingerprint(reports, api_id):
    check = reports[api_id].check("framework-fingerprint")
    assert check.outcome == "ok"
    assert check.measured["framework"] == "Anchor spec=0.1.0"
    assert check.measured["widths"] == {
        "instructions": [8],
        "accounts": [8],
        "events": [8],
    }


@pytest.mark.parametrize("api_id", ["jupiter", "whirlpool", "jurassic_fi"])
def test_no_offline_idl_is_unknown_never_ok(reports, api_id):
    """No data is not good data. These three fetch their IDL live, so the question was
    not asked — and an unasked question must not read as a passed one."""
    check = reports[api_id].check("framework-fingerprint")
    assert check.outcome == "unknown"
    assert check.measured["measured"] is False
    assert "CANNOT MEASURE OFFLINE" in check.headline
    assert check.blocks is False


def test_a_one_byte_account_discriminator_is_caught_before_it_decodes_wrongly():
    """The synthetic case that proves the arithmetic, not just the fixtures: a spec-0.1.0
    IDL declaring a 1-byte account discriminator makes field_offset return 8 where the
    truth is 1 — no exception, no warning, a neighbouring field decoded as the answer."""
    from gecko.idl_layout import account_size, field_offset

    idl = {
        "metadata": {"spec": "0.1.0", "name": "synthetic", "version": "0.1.0"},
        "instructions": [{"name": "go", "discriminator": [7]}],
        "accounts": [{"name": "Thing", "discriminator": [7]}],
        "types": [
            {
                "name": "Thing",
                "type": {
                    "kind": "struct",
                    "fields": [
                        {"name": "owner", "type": "pubkey"},
                        {"name": "amount", "type": "u64"},
                    ],
                },
            }
        ],
    }
    # idl_layout answers, confidently, wrong:
    assert field_offset(idl, "Thing", "owner")["offset"] == 8  # truth: 1
    assert account_size(idl, "Thing") == 48  # truth: 41
    # the gate refuses first:
    check = check_framework_fingerprint("synthetic", idl)
    assert check.outcome == "refuse"
    assert check.measured["widths"]["accounts"] == [1]


# --------------------------------------------------------------------------- #
# cardinality — a class from the surface, never an instance count
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "api_id, outcome",
    [
        ("let_me_buy", "ok"),
        ("ore", "ok"),
        ("pumpfun", "ok"),
        ("meteora", "warn"),
        ("metadao_ico", "warn"),
        ("whirlpool", "warn"),
        ("jurassic_fi", "warn"),
        ("jupiter", "unknown"),
    ],
)
def test_cardinality_outcomes(reports, api_id, outcome):
    assert reports[api_id].check("cardinality").outcome == outcome


def test_whirlpool_fee_tier_selectors_are_named(reports):
    """tick_spacing selects the pool among fee tiers and is derivable from the mints in
    no AMM we have read. The overlay's own `why` says ts=2 and ts=64 both yield
    well-formed WRONG pools."""
    measured = reports["whirlpool"].check("cardinality").measured
    assert measured["selector_seeded"] == {
        "adaptive_fee_tier": ["fee_tier_index"],
        "bundled_position": ["bundle_index"],
        "fee_tier": ["tick_spacing"],
        "whirlpool": ["tick_spacing"],
    }
    assert measured["resolver_seeded"] == {
        "bundled_position": ["position_bundle"],
        "reward_token_badge": ["whirlpool"],
    }


def test_meteora_fee_tier_and_uncovered_types(reports):
    """lb_pair is seeded on bin_step+base_factor (the DLMM fee tier), and 7 of the IDL's
    12 declared account types have no path at all — no config recipe, no witness from
    account_recipes.verification_recipe, not a singleton."""
    measured = reports["meteora"].check("cardinality").measured
    assert measured["selector_seeded"] == {
        "bin_array": ["index"],
        "lb_pair": ["base_factor", "bin_step"],
    }
    assert measured["types_no_path"] == [
        "ClaimFeeOperator",
        "DummyZcAccount",
        "LimitOrder",
        "Operator",
        "PositionV2",
        "PresetParameter",
        "PresetParameter2",
    ]
    assert measured["types_witnessed"] == ["TokenBadge"]


def test_metadao_old_types_have_no_path(reports):
    """The two types no instruction slot in the IDL ever names."""
    measured = reports["metadao_ico"].check("cardinality").measured
    assert measured["types_no_path"] == ["OldFundingRecord", "OldLaunch"]


def test_ore_round_is_bounded_through_a_singleton(reports):
    """round is resolver-seeded on board.round_id, and board is a SINGLETON at a constant
    address — read it, derive, exactly one Round. A resolver with a path is not a
    warning."""
    measured = reports["ore"].check("cardinality").measured
    assert measured["resolver_seeded"] == {"round": ["board"]}
    assert "board" in measured["singletons"]
    assert measured["types_no_path"] == []
    assert reports["ore"].check("cardinality").outcome == "ok"


def test_a_resolver_with_no_declared_parent_refuses():
    """The one cardinality REFUSE: a seed read off an account this config never declares
    has no path offline OR online. No packaged program is in this state, so it is proven
    against a minimal recipe rather than left untested."""
    check = ingest_gate.check_cardinality(
        "candidate",
        {
            "thing": {
                "seeds": [
                    {"kind": "constant", "value": "thing", "encoding": "utf8"},
                    {
                        "kind": "resolver",
                        "name": "owner",
                        "depends_on": ["nowhere"],
                        "reason": "account field seed",
                    },
                ]
            }
        },
    )
    assert check.outcome == "refuse"
    (finding,) = [f for f in check.findings if f.outcome == "refuse"]
    assert "program.pdas.thing" in finding.location
    assert "nowhere" in finding.missing


def test_cardinality_headline_disclaims_an_instance_count(reports):
    headline = reports["whirlpool"].check("cardinality").headline
    assert "cardinality CLASS from the surface, not an instance count" in headline
    assert "MAX_TOOL_INSTANCES = 50" in headline


def test_cardinality_cap_is_read_from_read_accounts_not_retyped():
    from gecko.read_accounts import MAX_TOOL_INSTANCES

    measured = ingest_gate.check_cardinality("x", {}).measured
    assert measured["instance_cap"] == MAX_TOOL_INSTANCES


# --------------------------------------------------------------------------- #
# discrimination — cross-catalog, with margins and its own caveat
# --------------------------------------------------------------------------- #
def test_discrimination_runs_across_the_whole_catalog(reports):
    """Not the candidate alone: every card of every wired program is probed, so a
    candidate stealing an incumbent and an incumbent stealing the candidate are the same
    measurement. Measured today: 20 cards over 8 programs, 0 stolen."""
    check = reports["whirlpool"].check("discrimination")
    assert check.measured["cards"] == 20
    assert check.measured["programs"] == 8
    assert check.measured["stolen"] == 0
    assert check.outcome == "ok"


def test_discrimination_reports_the_margin_distribution_not_only_the_count(reports):
    """The binary steal count is 0 for every program today, so on its own it says
    nothing. The margins are what carry the information.

    MEDIAN 36 -> 35 on 2026-08-29, and it is a COST we chose, not drift. Enriching
    meteora/swap's card with the words a person actually says (swap/exchange/convert/
    trade/rate) bought +1 recall@1 and +1 recall@3 on the golden set — and narrowed
    pumpfun's median discrimination margin by one, because a card that matches more
    queries also competes with more cards. Causation verified by reverting the card
    text alone and watching the median return to 36.

    That is the whole tension in one number: card enrichment BUYS RECALL AND SPENDS
    DISCRIMINATION. At 8 wired programs a one-unit median move against a p25 of 14 is
    noise. It will not stay noise — `withdraw` names an instruction in 831 catalogue
    programs and `claim` in 717, so discrimination is the axis that decays with N while
    recall is the one that improves. Anyone enriching the next card should read this
    number, not just the recall it buys: a floor on margins is the guard that has to
    exist before enrichment is applied at catalogue scale.
    """
    measured = reports["pumpfun"].check("discrimination").measured
    assert measured["margin_quantiles"] == {
        "min": 5,
        "p25": 14,
        "median": 35,
        "p75": 86,
        "max": 332,
    }
    assert len(measured["margins"]) == 20
    assert measured["margins"] == sorted(measured["margins"])
    for key in ("min", "p25", "median", "p75", "max"):
        assert (
            str(measured["margin_quantiles"][key])
            in reports["pumpfun"].check("discrimination").headline
        )


def test_discrimination_states_what_it_does_not_measure(reports):
    """The probe text is in the haystack by construction. The check has to say so in its
    OWN output, every run — a reader who sees 0/20 and reads it as comprehension has been
    misled by us."""
    headline = reports["ore"].check("discrimination").headline
    assert DISCRIMINATION_CAVEAT in headline
    assert "DISCRIMINATION, not COMPREHENSION" in headline


def test_whirlpool_is_still_ranked_by_its_surface_card(reports):
    """Honesty about the shape of the whirlpool failure: it has NO start card, but it
    does have a surface card, and that card wins its own text by 154. 'Unreachable' is a
    statement about starts, not about the router."""
    rows = reports["whirlpool"].check("discrimination").measured["candidate_cards"]
    assert rows == [{"card": "whirlpool.surface", "rank": 1, "own": 215, "margin": 154}]


def test_a_stolen_card_refuses():
    """Proven with a two-card catalog rather than left untested: today nothing is stolen,
    and a check whose refusing branch never runs is not known to work."""

    class _Op:
        def __init__(self, name, summary, description):
            self.method = "GET"
            self.path = f"/{name}"
            self.operation_id = name
            self.summary = summary
            self.description = description
            self.tags = [name]
            self.parameters = []
            self.request_body = None
            self.responses = {}

    class _Card:
        def __init__(self, api_id, operation):
            self.api_id = api_id
            self.kind = "start"
            self.instruction = operation.operation_id
            self.intent_name = None
            self.operation = operation

    rich = _Op(
        "rich",
        "swap tokens on a concentrated liquidity pool",
        "swap tokens on a concentrated liquidity pool with tick arrays and an oracle",
    )
    thin = _Op("thin", "swap tokens", "swap tokens")
    check = ingest_gate.check_discrimination(
        "thin", [_Card("rich", rich), _Card("thin", thin)]
    )
    assert check.outcome == "refuse"
    (finding,) = [f for f in check.findings if f.outcome == "refuse"]
    assert "thin.thin" in finding.missing
    assert "rich" in finding.missing


def test_a_one_card_catalog_is_unknown():
    check = ingest_gate.check_discrimination("solo", [])
    assert check.outcome == "unknown"
    assert check.measured["measured"] is False
    assert DISCRIMINATION_CAVEAT in check.headline


# --------------------------------------------------------------------------- #
# the gate itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "api_id, outcome",
    [
        ("pumpfun", "ok"),
        ("let_me_buy", "warn"),
        ("jupiter", "warn"),
        ("meteora", "warn"),
        ("metadao_ico", "warn"),
        ("jurassic_fi", "warn"),
        ("ore", "refuse"),
        ("whirlpool", "refuse"),
    ],
)
def test_gate_outcomes_span_all_four_values(reports, api_id, outcome):
    assert reports[api_id].outcome == outcome
    assert reports[api_id].blocks is (outcome == "refuse")


def test_only_refuse_blocks(reports):
    """warn and unknown are reported and never enforced. A gate that blocked on unknown
    would block three of the eight programs we already ship."""
    assert reports["jurassic_fi"].outcome == "warn"
    assert reports["jurassic_fi"].blocks is False
    assert reports["jupiter"].check("framework-fingerprint").outcome == "unknown"
    assert reports["jupiter"].blocks is False


def test_every_check_runs_for_every_program(reports):
    for api_id in ALL_PROGRAMS:
        assert tuple(c.name for c in reports[api_id].checks) == CHECKS


def test_every_finding_names_a_location_a_missing_thing_and_a_fix(reports):
    """The design constraint, enforced. A finding that cannot say where it is, what is
    absent and what to do about it is indistinguishable from a shrug."""
    seen = 0
    for api_id in ALL_PROGRAMS:
        for check in reports[api_id].checks:
            for finding in check.findings:
                seen += 1
                assert finding.location.strip()
                assert finding.missing.strip()
                assert finding.fix.strip()
                assert finding.outcome in ("refuse", "warn", "unknown")
                assert finding.check == check.name
    assert seen > 0


def test_the_gate_is_deterministic(reports):
    """Offline and pure: same bytes twice, so two runs can be diffed."""
    again = run("whirlpool")
    assert json.dumps(again.to_dict(), sort_keys=True) == json.dumps(
        reports["whirlpool"].to_dict(), sort_keys=True
    )


def test_the_gate_opens_no_socket(monkeypatch):
    """No RPC, no network, no model call — proven by making a socket impossible."""

    def _no(*args, **kwargs):
        raise AssertionError("the ingest gate opened a socket")

    monkeypatch.setattr(socket, "socket", _no)
    monkeypatch.setattr(socket, "create_connection", _no)
    # the guard is only a guard if it is reachable: prove the patch bites first
    with pytest.raises(AssertionError):
        socket.socket()
    report = run("whirlpool")
    assert report.outcome == "refuse"


def test_an_unlisted_program_raises_rather_than_scoring_zero():
    with pytest.raises(KeyError) as excinfo:
        gate("orca_not_a_program")
    assert "provider.json" in excinfo.value.args[0]


def test_render_prints_every_findings_three_parts(reports):
    text = ingest_gate.render(reports["whirlpool"])
    assert "REFUSE — do not ingest" in text
    for check in reports["whirlpool"].checks:
        for finding in check.findings:
            assert finding.location in text
            assert finding.missing in text
            assert finding.fix in text


# --------------------------------------------------------------------------- #
# the CLI entry point
# --------------------------------------------------------------------------- #
def test_cli_exits_nonzero_on_a_refusal(capsys):
    from gecko.cli import main

    assert main(["ingest-gate", "whirlpool"]) == 1
    out = capsys.readouterr().out
    assert "whirlpool: declared={plan_swap} supplied={} missing={plan_swap}" in out
    assert "DISCRIMINATION, not COMPREHENSION" in out


def test_cli_exits_zero_on_a_clean_program(capsys):
    from gecko.cli import main

    assert main(["ingest-gate", "pumpfun", "--idl", str(PACKAGED_IDLS["pumpfun"])]) == 0
    assert "pumpfun: OK" in capsys.readouterr().out


def test_cli_strict_fails_on_a_warn(capsys):
    from gecko.cli import main

    assert main(["ingest-gate", "jurassic_fi"]) == 0
    assert main(["ingest-gate", "jurassic_fi", "--strict"]) == 1


def test_cli_is_a_known_subcommand():
    """`gecko ingest-gate ...` must not fall through to the bare-spec `serve` shorthand."""
    from gecko.cli import _SUBCOMMANDS, _default_to_serve

    assert "ingest-gate" in _SUBCOMMANDS
    assert _default_to_serve(["ingest-gate", "whirlpool"]) == (
        "ingest-gate",
        ["whirlpool"],
    )


# --------------------------------------------------------------------------- #
# THE FOUR ATTACKS
#
# An adversarial pass was run against this gate and all four of these got through. They
# are kept together, named for what they defeat, because a gate is only a gate if it is
# unreachable when false — a check that can be walked around is a check that reports the
# walk-around as a pass.
# --------------------------------------------------------------------------- #
class _Op:
    """The narrow slice of an operation the catalog ranks on."""

    def __init__(self, name, summary, description):
        self.method = "GET"
        self.path = f"/{name}"
        self.operation_id = name
        self.summary = summary
        self.description = description
        self.tags = [name]
        self.parameters = []
        self.request_body = None
        self.responses = {}


class _Card:
    def __init__(self, api_id, operation):
        self.api_id = api_id
        self.kind = "start"
        self.instruction = operation.operation_id
        self.intent_name = None
        self.operation = operation


def test_attack_1_an_empty_idl_cannot_score_ok():
    """AN EMPTY DOCUMENT AGREES WITH EVERYTHING.

    `{}` declares no instruction, account or event, so no declared discriminator width
    disagrees with idl_layout's constant, so the check found no findings and returned
    `ok` — certifying, on the strength of an empty file, a program whose REAL IDL it
    refuses. Nothing to compare is not agreement.
    """
    empty = check_framework_fingerprint("attacker", {}, idl_source="a blank document")
    assert empty.outcome == "unknown"
    assert empty.measured == {"measured": False, "empty_idl": True}

    # and the positive control: the real thing still refuses, so `unknown` is not simply
    # what this check now says about everything.
    real = ingest_gate.load_idl(str(PACKAGED_IDLS["ore"]))
    assert check_framework_fingerprint("ore", real).outcome == "refuse"


def test_attack_2_a_number_written_as_text_is_not_an_identity(reports):
    """THE SEED THAT HID IN ITS OWN ENCODING.

    Orca writes `start_tick_index` — an i32 — as its decimal string, so the seed's
    encoding is `utf8`. The old classifier read `utf8` as "a name the caller chose" and
    excluded it, which made a numeric selector INVISIBLE to the one check whose whole
    job is finding numeric selectors. `tick_array` scored clean while every value of
    that number derives a different, equally resolvable, wrong array.

    The encoding says how the bytes are made. Only the declared type says what the value
    IS — and with no IDL the honest answer is `unknown`, never "a name".
    """
    measured = reports["whirlpool"].check("cardinality").measured
    assert measured["unclassified_seeds"] == {"tick_array": ["start_tick_index"]}
    assert "tick_array" not in measured["selector_seeded"]
    unknowns = [
        f
        for f in reports["whirlpool"].check("cardinality").findings
        if f.outcome == "unknown"
    ]
    assert any("start_tick_index" in f.missing for f in unknowns)


def test_attack_2b_the_idl_type_settles_what_the_encoding_cannot():
    """The same utf8 seed, twice, with the declared type as the only difference — the
    positive control that proves the fix reads the type and did not just start calling
    every text seed unknown."""
    node = {
        "seeds": [
            {"kind": "constant", "value": "thing", "encoding": "utf8"},
            {
                "kind": "variable",
                "name": "market",
                "source": "account",
                "encoding": "pubkey",
            },
            {
                "kind": "variable",
                "name": "which",
                "source": "argument",
                "encoding": "utf8",
            },
        ]
    }
    numeric = {"instructions": [{"args": [{"name": "which", "type": "i32"}]}]}
    named = {"instructions": [{"args": [{"name": "which", "type": "string"}]}]}

    selectors, unknown = ingest_gate._classify_argument_seeds(
        node, ingest_gate._argument_types(numeric)
    )
    assert (selectors, unknown) == (["which"], [])

    selectors, unknown = ingest_gate._classify_argument_seeds(
        node, ingest_gate._argument_types(named)
    )
    assert (selectors, unknown) == ([], [])

    selectors, unknown = ingest_gate._classify_argument_seeds(node, None)
    assert (selectors, unknown) == ([], ["which"])


def test_attack_3_a_pre_existing_collision_does_not_refuse_a_new_config():
    """ONE BAD CONFIG BLOCKED EVERY FUTURE INGEST.

    The discrimination check walks the WHOLE catalog, and it appended its refusal for
    every misranked card regardless of whose it was. So a collision between two programs
    that were BOTH already wired refused the next candidate — forever, and for something
    the candidate did not do and cannot fix. A gate that fails closed on somebody else's
    fault is a gate that gets switched off.

    Proven on a synthetic catalog, because today's real one has no inherited collision:
    asserting this against the live catalog passes whether the gate is fixed or not, and
    a test that cannot fail is not evidence.
    """
    rich = _Op(
        "rich",
        "swap tokens on a concentrated liquidity pool",
        "swap tokens on a concentrated liquidity pool with tick arrays and an oracle",
    )
    thin = _Op("thin", "swap tokens", "swap tokens")
    newcomer = _Op(
        "lend",
        "borrow against posted collateral",
        "borrow against posted collateral at a variable rate with a liquidation margin",
    )
    cards = [_Card("rich", rich), _Card("thin", thin), _Card("newcomer", newcomer)]

    # the collision is real and it is between the two INCUMBENTS
    incumbent = ingest_gate.check_discrimination("thin", cards)
    assert incumbent.outcome == "refuse"

    # ...and the newcomer, whose own card is clean, is charged nothing for it
    candidate = ingest_gate.check_discrimination("newcomer", cards)
    assert candidate.findings == ()
    assert candidate.outcome == "ok"
    assert candidate.measured["inherited_collisions"] == 1, (
        "the inherited collision must still be COUNTED and printed — a clean verdict "
        "for the candidate must never read as a clean catalog"
    )


#: An IDL whose account discriminator is ONE byte — the ore shape. idl_layout.py:51
#: assumes eight, so every offset it computes for this program is seven bytes late and
#: decodes the neighbouring field into a well-formed, resolvable, WRONG address.
_ONE_BYTE_IDL = {
    "address": "oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv",
    "metadata": {"name": "attacker", "version": "0.1.0"},
    "instructions": [],
    "accounts": [{"name": "Thing", "discriminator": [1]}],
    "types": [
        {
            "name": "Thing",
            "type": {
                "kind": "struct",
                "fields": [
                    {"name": "owner", "type": "pubkey"},
                    {"name": "amount", "type": "u64"},
                ],
            },
        }
    ],
}


class _RefusingClient:
    """The narrowest possible stand-in for OrquestraClient: one project, one surface,
    no network. `comprehend_main` takes the client injected, so the ingest path can be
    driven end to end offline."""

    def fetch_surface(self, slug):
        from gecko.orquestra_client import ProjectSurface

        return ProjectSurface(
            slug=slug,
            program_id="oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv",
            idl=dict(_ONE_BYTE_IDL),
            pda_accounts=(),
        )


def test_attack_4_the_ingest_path_refuses_and_says_so_in_its_exit_code():
    """A CHECK NOTHING CALLS IS A CHECK THAT DOES NOT RUN.

    The gate passed all of its own tests and blocked exactly nothing, because no code
    path outside the test suite invoked it. `comprehend` is the ingest path — it is
    where a candidate config comes into existence — so that is where the pre-check
    belongs, and the refusal has to reach a script as an EXIT CODE. Prose on stderr is
    what the whirlpool regression already had.

    Driven for real rather than grepped for: a source-level assertion that the string
    `precheck_config` appears somewhere passes just as happily when the call has been
    disconnected from its result.
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from gecko.providers.cli import comprehend_main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = comprehend_main(["--project", "attacker"], client=_RefusingClient())

    assert rc == 3, "a refused config must exit non-zero, not merely complain"
    assert out.getvalue() == "", "a refused config must not be emitted at all"
    assert "REFUSE" in err.getvalue()
    assert "is not 8 bytes" in err.getvalue() or "not 8 bytes" in err.getvalue()

    # --force is an override a human types, and it never hides the verdict
    out2, err2 = io.StringIO(), io.StringIO()
    with redirect_stdout(out2), redirect_stderr(err2):
        rc2 = comprehend_main(
            ["--project", "attacker", "--force"], client=_RefusingClient()
        )
    assert rc2 == 0
    assert json.loads(out2.getvalue())["program"]["program_id"]
    assert "REFUSE" in err2.getvalue(), (
        "--force overrides the exit code, not the report"
    )


def test_a_candidate_config_can_be_gated_before_it_is_packaged():
    """precheck_config is the half of the gate that runs BEFORE ingest — and it must say
    out loud which checks it could not run, because a clean pre-check is a necessary
    condition for ingest and never a sufficient one."""
    from gecko.ingest_gate import precheck_config

    report = precheck_config(
        "candidate",
        {
            "program": {
                "program_id": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
                "intents": ["plan_swap"],
                "pdas": {
                    "thing": {
                        "seeds": [
                            {"kind": "constant", "value": "t", "encoding": "utf8"},
                            {
                                "kind": "variable",
                                "name": "owner",
                                "source": "account",
                                "encoding": "pubkey",
                            },
                            {
                                "kind": "variable",
                                "name": "index",
                                "source": "argument",
                                "encoding": "le",
                            },
                        ]
                    }
                },
            }
        },
    )
    assert report.check("cardinality").measured["selector_seeded"] == {
        "thing": ["index"]
    }
    # the two checks it CANNOT run are named, not silently skipped
    assert "intent-reachability" in report.summary
    assert "discrimination" in report.summary
    assert "plan_swap" in report.summary
    assert [c.name for c in report.checks] == ["cardinality", "framework-fingerprint"]


def test_no_wired_program_refuses_without_a_written_disposition(reports):
    """Both directions, and the second one is the one that rots.

    A new refusal must not hide among the old ones — and an entry must not outlive the
    refusal it describes. A waiver whose reason is no longer true is how a reverted
    invariant walks back in wearing its own justification.
    """
    from gecko.ingest_gate import RECORDED_REFUSALS

    refusing = {api_id for api_id, report in reports.items() if report.blocks}
    assert refusing == set(RECORDED_REFUSALS), (
        f"refusing={sorted(refusing)} recorded={sorted(RECORDED_REFUSALS)} — a refusal "
        "with no entry is unaccounted for; an entry with no refusal is stale"
    )
    for api_id, (disposition, why) in RECORDED_REFUSALS.items():
        assert disposition in ("waived", "open"), disposition
        assert len(why) > 120, f"{api_id}: a disposition without a reason is a shrug"


def test_an_open_refusal_is_never_described_as_accepted():
    """`waived` and `open` are not synonyms. `waived` claims containment and has to name
    it; `open` is a tracked bug and must not claim any."""
    from gecko.ingest_gate import RECORDED_REFUSALS

    for api_id, (disposition, why) in RECORDED_REFUSALS.items():
        if disposition == "waived":
            assert "CONTAINMENT:" in why, f"{api_id} is waived without naming what "
            "makes accepting it safe"
        else:
            assert "NO CONTAINMENT" in why, f"{api_id} is open, so it must say plainly "
            "that nothing is holding it"

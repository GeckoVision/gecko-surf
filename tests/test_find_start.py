"""find_start — THE SHOWCASE, offline (the demo in test form).

An agent that knows nothing about 40+ instructions across the wired programs
says a plain intent and gets the exact starting point: (program, instruction),
the dependency-ordered derive plan with a provenance tag on every account —
LEADING with the recovered gaps (the IDL-hidden / source-recovered accounts,
never the catalog listing) — the DECLARED preludes, the honest FLAGGED gaps,
and the Orquestra execute pointer.

Also under test: the no-fabrication floor (nonsense → honest no-start +
GUESS-labeled candidates), the comprehend-on-pick pointer for unwired catalog
programs, catalog pagination against saved fixtures, and the opt-in CATEGORICAL
miss instrumentation (never the intent text).
"""

from __future__ import annotations

import json
from pathlib import Path


from gecko.find_start import (
    FindStartResult,
    MissRecord,
    find_start,
    format_result,
    jsonl_miss_logger,
)
from gecko.orquestra_client import (
    OrquestraClient,
    ProjectCatalogPage,
    ProjectInfo,
)

FIXTURES = Path(__file__).parent / "fixtures" / "orquestra"
BASE = "https://api.orquestra.dev/api"

PUMP_INTENT = "buy this token on pump and hold it"


def _step(result: FindStartResult, account: str):
    steps = [s for s in result.starts[0].derive_plan if s.account == account]
    assert steps, f"{account} missing from the derive plan"
    return steps[0]


def _fixture_client(extra: dict[str, bytes] | None = None) -> OrquestraClient:
    responses = {
        f"{BASE}/projects?page=1": (FIXTURES / "projects.json").read_bytes(),
    }
    responses.update(extra or {})

    def http_get(url: str) -> bytes:
        if url not in responses:
            raise AssertionError(f"unexpected GET {url}")
        return responses[url]

    return OrquestraClient(base_url=BASE, http_get=http_get)


# --- THE SHOWCASE: buy on pump --------------------------------------------------


def test_pump_intent_routes_to_pumpfun_buy() -> None:
    result = find_start(PUMP_INTENT)
    top = result.starts[0]
    assert not result.no_start
    assert top.kind == "start"
    assert (top.program, top.instruction) == ("pumpfun", "buy")
    assert top.next_tool == "plan_buy"
    assert {"buy", "pump", "token"} <= set(top.why)
    assert top.execute is not None
    assert top.execute["url"].endswith("/instructions/buy/build")
    assert top.execute["builder"] == "orquestra"


def test_pump_derive_plan_leads_with_the_recovered_gaps() -> None:
    """The demo law: the recovered/IDL-hidden accounts are the story."""
    result = find_start(PUMP_INTENT)
    creator_vault = _step(result, "creator_vault")
    assert creator_vault.provenance == "recovered"
    assert creator_vault.resolver  # the dotted-path read recipe is declared
    assert "bonding_curve" in (creator_vault.resolver or "")

    bonding_curve_v2 = _step(result, "bonding_curve_v2")
    assert bonding_curve_v2.provenance == "recovered"
    assert "IDL never names it" in bonding_curve_v2.note  # the hidden account

    fee_recipient = _step(result, "fee_recipient")
    assert fee_recipient.provenance == "flagged"  # honest, never dropped
    assert any(g.name == "fee_recipient" for g in result.starts[0].gaps)


def test_pump_derive_plan_is_dependency_ordered() -> None:
    plan = [s.account for s in find_start(PUMP_INTENT).starts[0].derive_plan]
    assert plan.index("bonding_curve") < plan.index("creator_vault")


def test_pump_preludes_declare_ata_with_token_2022_note() -> None:
    preludes = find_start(PUMP_INTENT).starts[0].preludes
    kinds = {p.kind for p in preludes}
    assert "create_idempotent_ata" in kinds
    assert "compute_budget" in kinds
    ata = next(p for p in preludes if p.kind == "create_idempotent_ata")
    assert "Token-2022" in ata.note


# --- the other showcase intents -------------------------------------------------


def test_swap_intent_routes_to_meteora_swap() -> None:
    result = find_start("swap sol for usdc")
    top = result.starts[0]
    assert (top.program, top.instruction) == ("meteora", "swap")
    assert "base_factor" in top.inputs  # the fee-tier seed the agent must supply
    bin_array = _step(result, "bin_array")
    assert bin_array.provenance == "recovered"
    assert "remaining_accounts the IDL never names" in bin_array.note
    lb_pair = _step(result, "lb_pair")
    assert lb_pair.provenance == "recovered"
    assert "base_factor" in lb_pair.note  # the silently-wrong-pool seed


def test_ore_stake_intent_surfaces_the_cross_program_note() -> None:
    result = find_start("stake my tokens on ore")
    top = result.starts[0]
    assert top.program == "ore"
    assert top.kind == "surface"  # honest: no plan intent wired yet
    assert top.next_tool is None
    stake = _step(result, "stake")
    assert stake.provenance == "recovered"
    assert "cross-program" in stake.note  # CPI'd into the SEPARATE ore-stake program
    # ORE's runtime-data seed stays an honest flag — never dropped, never fabricated
    round_step = _step(result, "round")
    assert round_step.provenance == "flagged"


def test_nonsense_intent_is_an_honest_no_start_with_guesses() -> None:
    result = find_start("flumbuzzle the quantum wombat")
    assert result.no_start
    assert "no start found" in result.note
    assert result.starts  # closest candidates still offered…
    assert all(p.kind == "guess" for p in result.starts)  # …but labeled GUESSES
    assert all(p.score == 0 for p in result.starts)


def test_stopword_only_intent_routes_nothing() -> None:
    result = find_start("please do it for me")
    assert result.no_start
    assert result.starts == ()


def test_surface_card_of_a_program_with_intents_points_at_them() -> None:
    """Honesty: pumpfun's surface card must not claim 'no intent wired'."""
    result = find_start(PUMP_INTENT)
    surface = next(
        p for p in result.starts if p.kind == "surface" and p.program == "pumpfun"
    )
    assert "plan_buy" in surface.note
    assert "no executable plan intent" not in surface.note


def test_recovered_tags_are_program_level_truths() -> None:
    """lb_pair is source-recovered no matter which card shows it — the surface
    card must not downgrade it to 'extracted' while the intent card says
    'recovered'."""
    result = find_start("swap sol for usdc")
    surface = next(
        p for p in result.starts if p.kind == "surface" and p.program == "meteora"
    )
    lb_pair = next(s for s in surface.derive_plan if s.account == "lb_pair")
    assert lb_pair.provenance == "recovered"


# --- program hint ---------------------------------------------------------------


def test_wired_program_hint_narrows_the_search() -> None:
    result = find_start("buy tokens", program="meteora")
    assert all(p.program == "meteora" for p in result.starts)


def test_unwired_program_hint_returns_a_comprehend_pointer() -> None:
    page = _fixture_client().list_projects(page=1)
    result = find_start("swap tokens", program="Jupiter", catalog_pages=[page])
    assert result.no_start
    assert "not wired" in result.note
    assert result.catalog  # the D-A path: comprehend first
    top = result.catalog[0]
    assert "jupiter" in top.name.lower()
    assert top.comprehend_first["tool"] == "comprehend_program"
    assert top.slug in top.comprehend_first["cli"]


def test_catalog_candidates_ride_along_as_pointers_not_starts() -> None:
    projects = (
        ProjectInfo(
            id="jup6slug",
            name="Jupiter Aggregator",
            program_id="JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
            description="swap aggregator routing across pools",
        ),
    )
    page = ProjectCatalogPage(projects=projects, page=1, total_pages=1, total=1)
    result = find_start("swap sol for usdc", catalog_pages=[page])
    assert not result.no_start  # the wired start still wins
    assert result.starts[0].program == "meteora"
    assert [c.slug for c in result.catalog] == ["jup6slug"]
    assert "NOT yet comprehended" in result.catalog[0].comprehend_first["step"]


def test_wired_programs_never_appear_as_catalog_candidates() -> None:
    page = _fixture_client().list_projects(page=1)  # contains the 4 wired programs
    result = find_start(PUMP_INTENT, catalog_pages=[page])
    wired_ids = {p.program_id for p in result.starts}
    assert all(c.program_id not in wired_ids for c in result.catalog)


def test_catalog_pagination_fixture_spans_multiple_pages() -> None:
    # the saved catalog fixture is one page of a REAL paginated listing
    page = _fixture_client().list_projects(page=1)
    assert page.page == 1
    assert page.total_pages == 225
    assert page.total == 4500
    assert len(page.projects) == 20


# --- miss instrumentation (the lexical-vs-semantic evidence gate) ---------------


def test_miss_logging_is_off_by_default_and_categorical_when_on(
    tmp_path: Path,
) -> None:
    find_start("flumbuzzle the quantum wombat")  # no logger → no side effects

    log = tmp_path / "misses.jsonl"
    find_start("flumbuzzle the quantum wombat", on_miss=jsonl_miss_logger(str(log)))
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert set(record) == {
        "intent_term_count",
        "matched_score",
        "wired_program_count",
    }
    assert all(isinstance(v, int) for v in record.values())
    # NEVER the intent text (it could carry user data — control-plane rules)
    assert "wombat" not in log.read_text(encoding="utf-8")


def test_hits_never_log_a_miss() -> None:
    records: list[MissRecord] = []
    find_start(PUMP_INTENT, on_miss=records.append)
    assert records == []


def test_miss_record_counts_are_sane() -> None:
    records: list[MissRecord] = []
    find_start("flumbuzzle the quantum wombat", on_miss=records.append)
    (record,) = records
    assert record.intent_term_count == 3  # 'the' is a stopword
    assert record.matched_score == 0
    assert record.wired_program_count == 4


# --- rendering + serialization --------------------------------------------------


def test_format_result_shows_provenance_tags_and_flagged_gaps() -> None:
    text = format_result(find_start(PUMP_INTENT))
    assert "[recovered]" in text
    assert "[FLAGGED]" in text
    assert "fee_recipient" in text
    assert "Token-2022" in text
    assert "never signs" in text


def test_result_is_json_serializable() -> None:
    payload = json.dumps(find_start(PUMP_INTENT).to_json())
    loaded = json.loads(payload)
    assert loaded["starts"][0]["program"] == "pumpfun"
    assert loaded["no_start"] is False


# --- the derivation-order seam --------------------------------------------------


def test_derivation_order_for_orders_dependents_after_roots() -> None:
    from gecko.program_graph import derivation_order_for
    from gecko.provider_config import load_packaged_provider

    _, apis = load_packaged_provider("orquestra")
    program = apis["pumpfun"].program
    assert program is not None
    ordered = derivation_order_for(
        dict(program.pdas), ("creator_vault", "global", "bonding_curve")
    )
    assert ordered.index("bonding_curve") < ordered.index("creator_vault")
    assert set(ordered) == {"creator_vault", "global", "bonding_curve"}

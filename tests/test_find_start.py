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


from gecko.providers.cli import PROGRAMS
from gecko.provider_config import load_packaged_provider
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


# --- the lifecycle chain (the first multi-instruction start) ---------------------


def test_a_bar_intent_routes_to_a_let_me_buy_instruction_not_a_memecoin_buy() -> None:
    """Before this landed, "buy a beer" routed to pumpfun/buy — a runnable plan to buy
    a memecoin off a bonding curve. The nearest ask an agent could act on was the wrong
    program entirely, because no card in the corpus named a storefront."""
    top = find_start("buy a beer").starts[0]
    assert (top.program, top.instruction) == ("let_me_buy", "make_purchase")
    assert top.kind == "start"
    assert top.next_tool is None  # honest: no Gecko plan tool, the derive plan is it
    assert "beer" in top.why


def test_every_start_point_carries_a_chain_verdict_it_had_to_decide() -> None:
    """``StartPoint.chain`` has no default. A start that is one call of a multi-call
    lifecycle and does not say so is exactly what this field prevents, and a start with
    no chain must say THAT rather than leave the field empty."""
    for intent in (PUMP_INTENT, "buy a beer", "swap sol for usdc"):
        for point in find_start(intent).starts:
            assert point.chain.status in {"ordered", "unresolved", "not_evaluated"}
            assert point.chain.note


def test_a_chain_start_is_ordered_only_on_an_explicit_agreement() -> None:
    unchecked = find_start("buy a beer").starts[0].chain
    assert unchecked.status == "not_evaluated"  # fail closed: nothing supplied it
    checked = (
        find_start("buy a beer", chain_verdicts={"sell_and_deliver": "AGREE"})
        .starts[0]
        .chain
    )
    assert checked.status == "ordered"
    assert [s.instruction for s in checked.steps] == [
        "make_purchase",
        "mark_as_delivered",
    ]


def test_the_surface_card_of_a_chain_program_points_at_its_steps() -> None:
    """Honesty: let_me_buy's surface card must not claim 'no start is wired' now that
    its chain steps are start points."""
    surface = next(
        p
        for p in find_start("open a store and sell peanuts", limit=12).starts
        if p.kind == "surface" and p.program == "let_me_buy"
    )
    assert "initialize" in surface.note
    assert "no executable plan intent" not in surface.note


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
    # limit raised past the default: a fifth wired program pushes meteora's SURFACE
    # card (its intent card ranks higher) out of the top 5.
    result = find_start("swap sol for usdc", limit=12)
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
    # Jupiter used to stand in for "unwired" here; it is wired now, so the fixture's
    # PancakeSwap plays that role. The behaviour under test is unchanged.
    page = _fixture_client().list_projects(page=1)
    result = find_start("swap tokens", program="PancakeSwap", catalog_pages=[page])
    assert result.no_start
    assert "not wired" in result.note
    assert result.catalog  # the D-A path: comprehend first
    top = result.catalog[0]
    assert "pancakeswap" in top.name.lower()
    assert top.comprehend_first["tool"] == "comprehend_program"
    assert top.slug in top.comprehend_first["cli"]


def test_catalog_candidates_ride_along_as_pointers_not_starts() -> None:
    # An UNWIRED catalog entry (Jupiter itself is wired now, so this is a different
    # aggregator) must ride along as a pointer and never outrank a wired start.
    projects = (
        ProjectInfo(
            id="raydslug",
            name="Raydium AMM",
            program_id="675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
            description="swap aggregator routing across pools",
        ),
    )
    page = ProjectCatalogPage(projects=projects, page=1, total_pages=1, total=1)
    result = find_start("swap sol for usdc on meteora", catalog_pages=[page])
    assert not result.no_start  # the wired start still wins
    assert result.starts[0].program == "meteora"
    assert [c.slug for c in result.catalog] == ["raydslug"]
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
        # v1 fields — kept, back-compat
        "intent_term_count",
        "matched_score",
        "wired_program_count",
        # v2 — gold-free ranking fields (names/scores only)
        "top_candidates",
        "margin",
        "floor",
        # v2 — eval-only fields, None in production
        "gold_rank",
        "miss_cause",
    }
    assert record["floor"] == "guess"
    assert record["gold_rank"] is None
    assert record["miss_cause"] is None
    # the served GUESSES ride along as program/instruction NAMES + scores
    assert record["top_candidates"]
    assert all(
        c["kind"] == "guess" and c["score"] == 0 for c in record["top_candidates"]
    )
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
    # The field counts the api_ids that produced CARDS — i.e. every packaged program
    # config — not the intent registry. Those two were the same number until
    # `let_me_buy` landed: it is comprehended for DERIVATION and wires no plan intent,
    # so PROGRAMS is now a STRICT subset. Asserting against the card source is what the
    # field actually means; the subset check keeps the old relationship visible.
    packaged = {
        api_id
        for api_id, api in load_packaged_provider("orquestra")[1].items()
        if api.program is not None
    }
    assert record.wired_program_count == len(packaged)
    assert set(PROGRAMS) < packaged
    assert record.margin == 0  # all fallback guesses score 0


def test_empty_intent_miss_record_serves_no_candidates() -> None:
    records: list[MissRecord] = []
    find_start("please do it for me", on_miss=records.append)
    (record,) = records
    assert record.top_candidates == ()
    assert record.floor == "guess"  # nothing above the floor was served


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


def test_derivation_order_with_cycle_reports_the_unorderable_accounts() -> None:
    """The seam find_start uses must hand back the cycle, not just an order."""
    from gecko.pda import PdaNode, VariablePdaSeedNode
    from gecko.program_graph import derivation_order_with_cycle

    pdas = {
        "a": PdaNode("a", (VariablePdaSeedNode("b", "account", "pubkey"),), None),
        "b": PdaNode("b", (VariablePdaSeedNode("a", "account", "pubkey"),), None),
    }
    ordered, cycle = derivation_order_with_cycle(pdas, ("a", "b"))
    assert set(ordered) == {"a", "b"}  # never dropped
    assert cycle == frozenset({"a", "b"})


def test_a_cyclic_account_is_flagged_in_the_derive_plan() -> None:
    """A cycle outranks every recipe: an account that cannot be ordered is a
    FLAGGED gap, never a confident ``extracted``/``recovered`` step."""
    from gecko.find_start import _account_step

    step = _account_step(
        "a",
        None,
        recovered={"a": "recovered from source"},
        overlay_pdas=frozenset(),
        overlay_why={},
        cyclic=frozenset({"a", "b"}),
    )
    assert step.provenance == "flagged"
    assert "cycle" in step.note and "a, b" in step.note


# --- R2: the lexical lens (field-weighted BM25F) ----------------------------------


def test_an_instruction_outranks_its_own_program_surface() -> None:
    """The parent/child field model, measured.

    A program-SURFACE card carries the whole program note plus every PDA name, so on
    a plain term COUNT it absorbs vocabulary no single instruction card can carry and
    beats its own instruction. It did, on this exact intent: the old ranker served the
    `metadao_ico` surface first and `metadao_ico/fund` — the thing the caller asked
    for — second. The surface card is not an independent document competing with its
    instructions; it is their parent, holding the union of their vocabulary, so it is
    scored on a `notes` field at half a real description's weight.
    """
    result = find_start("participate in the metadao ico", limit=5)
    assert not result.no_start
    top = result.starts[0]
    assert (top.program, top.instruction) == ("metadao_ico", "fund")
    assert top.kind == "start"
    # …and the surface is still offered, just not first: it is the honest fallback,
    # not the answer.
    assert any(
        p.instruction is None and p.program == "metadao_ico" for p in result.starts
    )


def test_a_repeated_term_in_prose_cannot_outrank_a_name_match() -> None:
    """The property per-field saturation buys, on cards built here so it cannot pass
    by accident of the real corpus.

    Textbook BM25F sums the per-field TFs and saturates ONCE, so an unbounded prose
    field can raise the combined TF without limit — measured on the real cards, the
    pumpfun surface (`pump` x7, `buy` x6 in its note) beat its own `buy` instruction
    on every pumpfun row, dropping recall@1 to 0.6364. Saturating each field on its
    own caps what any one field can contribute at that field's weight.
    """
    from gecko.find_start import _Card, _CardLens, _operation

    def card(kind: str, api_id: str, instruction: str | None, prose: str) -> _Card:
        return _Card(
            kind=kind,  # type: ignore[arg-type]
            api_id=api_id,
            program_id="Prog111",
            instruction=instruction,
            intent_name=None,
            inputs=(),
            accounts=(),
            pdas={},
            spec=None,
            notes=prose,
            execute_url=None,
            operation=_operation(
                operation_id=instruction or api_id,
                path=f"/{api_id}" + (f"/{instruction}" if instruction else ""),
                summary="",
                description=prose,
                tags=[api_id],
            ),
        )

    # `rank` takes ALREADY-FOLDED terms (what `_query_tokens` produces), so the term
    # here is deliberately fold-stable — an unfolded one would score 0 on both cards
    # and silently fall through to the never-empty prior instead of testing anything.
    named = card("start", "widget", "sprocket", "does the thing")
    shouty = card("start", "other", "unrelated", " ".join(["sprocket"] * 40))
    ranked = _CardLens([named, shouty]).rank({"sprocket": 1.0}, limit=2)
    assert not ranked[0].is_fallback
    assert (ranked[0].card.api_id, ranked[0].card.instruction) == (
        "widget",
        "sprocket",
    )
    assert ranked[0].score > ranked[1].score


def test_a_genuine_lexical_hit_never_scores_zero() -> None:
    """Score 0 keeps meaning exactly one thing — no lexical evidence at all — so the
    below-floor GUESS path stays distinguishable from a weak real match."""
    from gecko.find_start import _CardLens, _query_tokens, _wired_cards

    cards = _wired_cards()
    ranked = _CardLens(cards).rank(
        {t: 1.0 for t in _query_tokens("buy this token on pump")}, limit=5
    )
    assert ranked and not any(r.is_fallback for r in ranked)
    assert all(r.score >= 1 for r in ranked)


def test_the_never_empty_prior_still_answers_a_matchless_query() -> None:
    """No genuine match must still return flagged candidates at score 0 — the 0/97
    contract the overlap scorer carried, preserved by the lens."""
    from gecko.find_start import _CardLens, _wired_cards

    ranked = _CardLens(_wired_cards()).rank({"zzzqqxnothing": 1.0}, limit=3)
    assert len(ranked) == 3
    assert all(r.is_fallback and r.score == 0 for r in ranked)


def test_floor_admission_is_a_closed_four_way_table() -> None:
    """The floor's branch, as a pure function — the SINGLE source the router gates on
    and the eval classifies false accepts with, so the two cannot drift.

    The constants are pinned here on purpose: one naming term admits, and it takes TWO
    distinguishing terms to admit without one. Loosening either is a security-gate
    decision, and this table is where such a change becomes visible.
    """
    from gecko.find_start import floor_admission, _wired_cards

    card = next(
        c for c in _wired_cards() if (c.api_id, c.instruction) == ("meteora", "swap")
    )
    assert floor_admission({"swap"}, card, set()) == "named"
    assert floor_admission({"a", "b"}, card, {"a", "b"}) == "corroborated"
    assert floor_admission({"swap", "a", "b"}, card, {"a", "b"}) == "both"
    assert floor_admission({"a"}, card, {"a"}) == "refused"  # one term is not two
    assert floor_admission(set(), card, {"a", "b"}) == "refused"

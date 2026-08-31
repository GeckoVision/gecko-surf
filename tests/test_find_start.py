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
    # It WAS `None`, with a note saying "no Gecko plan tool is wired for this
    # instruction". That was false — `prepare_purchase` plans and CHECKS this exact call,
    # and a null here sent agents to a raw builder instead.
    assert top.next_tool == "prepare_purchase"
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
    # The query names the surface. It used to be "swap sol for usdc", which reached this
    # card only through the word "swap" in its DESCRIPTION prose (about bin_array PDAs) —
    # its summary is "Meteora DLMM", its tags are ["meteora"], and neither says "swap".
    # The confidence floor now gates on intent vocabulary rather than prose, so that route
    # is gone and the intent card `plan_swap` answers a swap query alone, which is right.
    # The assertion below is unchanged: this test is about provenance agreeing across the
    # two cards, and the query was only ever the vehicle for surfacing them.
    result = find_start("meteora dlmm", limit=12)
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
    # config — not the intent registry. The two sets diverged while let_me_buy and
    # jurassic_fi were comprehended for DERIVATION only; since 2026-08-31 every
    # packaged program wires an intent again, so the sets coincide — asserting
    # equality keeps the next divergence visible either way.
    packaged = {
        api_id
        for api_id, api in load_packaged_provider("orquestra")[1].items()
        if api.program is not None
    }
    assert record.wired_program_count == len(packaged)
    assert set(PROGRAMS) == packaged
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


def test_a_purchase_routes_to_the_tool_that_checks_it_not_to_a_raw_builder() -> None:
    """`find_start` resolved let_me_buy/make_purchase perfectly and then said
    `next_tool: null` with "no Gecko plan tool is wired for this instruction".

    That is FALSE — `prepare_purchase` is the plan tool for `make_purchase`, and it is the
    one that derives offline, refuses a self-paying plan, patches a fresh blockhash,
    simulates the exact bytes and binds a receipt. Sending an agent to Orquestra's raw
    `/build` instead is the strictly worse path, recommended by our own router.
    """
    from gecko.find_start import find_start

    result = find_start("buy a coffee at a store")
    top = result.starts[0]

    assert (top.program, top.instruction) == ("let_me_buy", "make_purchase")
    assert top.next_tool == "prepare_purchase"


def test_no_start_point_instructs_the_agent_to_pass_something_it_cannot() -> None:
    """A dangling instruction is worse than none: it burns a reasoning step and yields
    nothing. The note told agents to `Supply chain_verdicts={name:'AGREE'}` while the tool
    schema is {intent, program} — there is no such parameter, and there must not be.

    A verdict that an AGENT can assert is a verdict the agent can fabricate. It stays a
    library-caller argument, supplied from evidence, and the prose must not invite anyone
    to pass it through a tool.
    """
    import json

    from gecko.find_start import find_start
    from gecko.providers.catalog_surface import _FIND_START_TOOL

    assert "chain_verdicts" not in _FIND_START_TOOL["inputSchema"]["properties"]

    blob = json.dumps(find_start("buy this token on pump and hold it").to_json())
    assert "Supply chain_verdicts" not in blob, (
        "the result tells an agent to pass a parameter the tool does not accept"
    )


def test_a_function_word_does_not_outrank_the_verb_that_names_the_capability() -> None:
    """Measured live, 2026-08-19: "swap one token for another" routed to
    ``let_me_buy.make_purchase``, because ``one`` and ``token`` both scored and beat
    ``meteora.swap``'s single ``swap``. Dropping the word ``one`` from the same sentence
    returned the right program — so a filler word was outvoting the verb.

    Document frequency (``_distinguishing_terms``) cannot catch this: ``one`` is rare
    enough across cards to look distinguishing. It is not a rare term, it is a function
    word, and function words are excluded by class rather than by count.
    """
    result = find_start("swap one token for another", limit=3)

    top = result.starts[0]
    assert top.program == "meteora", (
        f"a filler word still outranks the verb: {top.program}.{top.instruction} "
        f"won on {top.why}"
    )
    assert "one" not in top.why
    assert "another" not in top.why


def test_dropping_a_filler_word_does_not_change_the_answer() -> None:
    """The same intent with and without its filler words must route identically —
    otherwise the ranking is reading English grammar as evidence about Solana."""
    with_filler = find_start("swap one token for another", limit=1).starts[0]
    without = find_start("swap token", limit=1).starts[0]

    assert (with_filler.program, with_filler.instruction) == (
        without.program,
        without.instruction,
    )


def test_naming_the_verb_is_not_describing_the_task() -> None:
    """Measured 2026-08-19: 8 of the golden set's 12 out-of-scope intents cleared the floor
    and were served as RUNNABLE starts. Six of them matched on a single word, and that word
    was the instruction's own name:

        "buy a house"                   -> pumpfun.buy               why=('buy',)
        "sell my car"                   -> pumpfun.sell              why=('sell',)
        "deliver my mail"               -> let_me_buy.mark_as_delivered
        "swap my shift with a coworker" -> meteora.swap              why=('swap',)

    The corroboration gate has two branches, and the ``named`` branch was unconditional: a
    match on a program or instruction NAME was treated as sufficient evidence on its own.
    But naming an action is not describing a task. "buy" says what to do; it says nothing
    about WHAT is being bought, and every one of these intents supplied that object —
    house, car, mail, shift — in words no wired card carries.

    Note what could not fix this: "swap my shift with a coworker" and "swap sol for usdc"
    match the identical term set ``{swap}``. Nothing about the matched words separates them.
    The evidence is in what the caller said BEYOND the verb, so that is what the gate now
    requires.
    """
    for intent in (
        "buy a house",
        "sell my car",
        "deliver my mail",
        "swap my shift with a coworker",
        "buy groceries for the week",
    ):
        result = find_start(intent, limit=5)
        assert result.no_start, (
            f"{intent!r} was served as a runnable start: "
            f"{result.starts[0].program}.{result.starts[0].instruction} "
            f"on why={result.starts[0].why}"
        )


def test_the_verb_plus_an_object_still_routes() -> None:
    """The other half of the rule, so the fix cannot be "refuse everything". These name an
    action AND say what it acts on, which is exactly the evidence the gate now asks for."""
    for intent, program in (
        ("buy a beer", "let_me_buy"),
        ("stake my tokens on ore", "ore"),
        ("claim my ore mining rewards", "ore"),
    ):
        result = find_start(intent, limit=5)
        assert not result.no_start, f"{intent!r} lost its start"
        assert result.starts[0].program == program


def test_a_card_that_describes_its_implementation_cannot_be_reached_by_task_words() -> (
    None
):
    """The gate's measured COST, pinned rather than hidden.

    `swap sol for usdc` no longer routes, and the reason is not the gate. `meteora.swap`'s
    card reads "Plan a Meteora DLMM swap. Give input_mint, output_mint, bin_step,
    base_factor..." — it describes the IMPLEMENTATION and never the words a person says.
    So the only term that matches is `swap`, which is the instruction's own name, and one
    verb is exactly what the gate stopped accepting.

    The permissive gate was not buying recall, it was borrowing it: the same branch that
    let this through also served "buy a house" as pumpfun.buy. Four of the seven rows the
    gate costs are meteora rows, all thin in the same way.

    The fix is to enrich the card from the PROGRAM ARTIFACT — its declared value domains,
    IDL errors and events — never by pasting this test's own vocabulary into the config,
    which would make the eval score itself. Until that lands, this is the honest state.
    """
    result = find_start("swap sol for usdc", limit=5)

    assert result.no_start
    meteora_swap = [
        point
        for point in result.starts
        if point.program == "meteora" and point.instruction == "swap"
    ]
    assert meteora_swap, "meteora.swap should still be offered as a candidate"
    assert meteora_swap[0].kind == "guess"
    assert meteora_swap[0].why == ("swap",)


def test_program_query_tokens_drop_bare_numbers() -> None:
    """The scoped half of the digit rule: `_query_tokens` (program routing) drops pure
    digits — a quantity names no program — while mixed identifiers stay identifiers."""
    from gecko.find_start import _query_tokens

    tokens = _query_tokens("buy 2 espressos")
    assert "2" not in tokens
    assert any("espresso" in t for t in tokens)


def test_a_quantity_does_not_route_a_purchase_to_a_token_launchpad() -> None:
    """The founder's Q5, pinned. "buy 2 Espressos … convert … USDC" must start at the
    storefront purchase, not at pumpfun — whose only claim was the token `2` (from
    `bonding_curve_v2`) plus generic `amount|buy`. The espresso vocabulary now reaches
    the make_purchase card the same way `coffee` already did for Q1."""
    from gecko.find_start import find_start

    result = find_start(
        "I'd like to buy 2 Espressos. I only have USDG and I need you to "
        "convert the right amount to USDC"
    )
    assert not result.no_start
    top = result.starts[0]
    assert (top.program, top.instruction) == ("let_me_buy", "make_purchase"), (
        f"routed to {top.program}/{top.instruction} [{top.kind}] via {top.why}"
    )

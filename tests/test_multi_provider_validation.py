"""Multi-provider validation — the "can Gecko connect to MANY real providers?" gate.

Offline ($0, Pattern B). This encodes the whole provider-universe matrix as assertions so
a regression ANYWHERE — a spec that stops comprehending, a surface that starts leaking an
auth header, an unexpected Skill-Guard quarantine, or a broken cross-provider mint join —
trips this test. The numbers here are the CURRENT honest state of the committed specs
(see ``gecko.provider_matrix``). After the round-2 narrow (an EVM address is PUBLIC data,
not a secret), privy's wallet-address FPs dropped 72 -> 6; the 6 survivors and the two
remaining single-op quarantines (payapi ``createTransfer`` / surfpool ``requestAirdrop``)
are CORRECT-BY-DESIGN fail-closed cases (indistinguishable-from-secret shapes, fund-routing
vocabulary with no address target, or over-depth schemas), recorded AS FINDINGS with the
honest answer = confirm-to-unquarantine, NOT loosen-a-rule. Any change to detection/
quarantine that moves these routes through defi-security — this test is the tripwire, never
the thing you loosen.
"""

from __future__ import annotations

from gecko.provider_matrix import (
    MINT_ENTITY,
    PROVIDERS,
    all_reports,
    exit_liquidity_chain,
    mint_correlation,
)

#: #operations comprehended per committed spec (extract_operations). A drift here means the
#: ingest changed how it enumerates a real surface — a strong, cheap regression anchor.
EXPECTED_OPERATIONS: dict[str, int] = {
    "birdeye": 89,
    "payapi": 2,
    "jito": 5,
    "jito_blockengine": 6,
    "pegana_api2": 41,
    "refugios": 1,
    "reportavnzla": 4,
    "sosvenezuela": 5,
    "surfpool": 14,
    "txline": 18,
    "colosseum": 11,
    "jupiter": 4,
    "privy": 159,
    "pegana_p0": 43,
}

#: The EXACT Skill-Guard quarantine per provider. Empty == clean (the honest expectation
#: for most). The non-empty ones are KNOWN false positives on benign prose/response shapes,
#: pinned so a detection change (either way) trips the gate for a security review.
EXPECTED_QUARANTINE: dict[str, tuple[str, ...]] = {
    "birdeye": (),
    "payapi": ("createTransfer",),  # fund_routing FP on "Transfer funds to a recipient"
    "jito": (),
    "jito_blockengine": (),
    "pegana_api2": (),
    "refugios": (),
    "reportavnzla": (),
    "sosvenezuela": (),
    "surfpool": (
        "requestAirdrop",
    ),  # base58-address FP in the airdrop response example
    "txline": (),
    "colosseum": (),
    "jupiter": (),
    "pegana_p0": (),
    # privy is asserted separately (6 correct-by-design fail-closed ops after the address
    # narrow) in test_privy_wallet_address_false_positives_are_pinned.
}

#: Providers that participate in the DECLARED Solana-token-mint value domain (with the
#: customer-confirmed hints applied). A wallet ``address`` alone does NOT qualify — only a
#: customer declaration does — so privy/others are intentionally absent.
MINT_PROVIDERS = {"pegana_p0", "birdeye", "jupiter"}


def _by_name() -> dict[str, object]:
    return {r.name: r for r in all_reports()}


def test_every_committed_spec_comprehends_without_raising() -> None:
    reports = all_reports()
    assert {r.name for r in reports} == set(PROVIDERS)
    for r in reports:
        assert r.comprehended, f"{r.name} DID NOT COMPREHEND: {r.error}"
        assert r.operations == EXPECTED_OPERATIONS[r.name], (
            f"{r.name}: operations {r.operations} != {EXPECTED_OPERATIONS[r.name]}"
        )
        # every comprehended op becomes a question-shaped tool def (none silently dropped).
        assert r.tools_total == r.operations


def test_no_surface_ever_exposes_an_auth_header_to_the_agent() -> None:
    """Invariant #4 across the WHOLE universe: not one surfaced tool leaks a credential.
    (txline's auth-gated ops carry ``Authorization``/``X-Api-Token`` params, but the
    no-auth session hides them — the agent-facing surface stays clean.)"""
    for r in all_reports():
        assert r.auth_exposed == (), (
            f"{r.name} exposes an auth header on tools: {r.auth_exposed}"
        )


def test_skill_guard_quarantine_matches_expected_per_provider() -> None:
    reports = _by_name()
    for name, expected in EXPECTED_QUARANTINE.items():
        got = reports[name].quarantined  # type: ignore[attr-defined]
        assert got == expected, f"{name}: quarantine {list(got)} != {list(expected)}"


def test_privy_wallet_address_false_positives_are_pinned() -> None:
    """Privy: after the EVM-address-is-not-a-secret narrow, 66 wallet ops that quarantined
    only on a PUBLIC 40-hex ``0x…`` address in a response example are now clean. The 6 that
    REMAIN are CORRECT-BY-DESIGN fail-closed, not the address FP — pinned so a detection
    change either way trips this gate:

      * ``getTransaction`` / ``getTransactionByReferenceId`` / ``signWithWallet`` — a
        64-hex ``0x…`` value (a 32-byte tx hash / signature) is byte-for-byte
        indistinguishable from an EVM private key at the text layer, so it fails closed.
      * ``withdrawFunds`` — the description "Withdraw funds" is fund-routing VOCABULARY with
        no address target; identical in shape to the red-team attack "withdraw all funds
        now", so the rule cannot un-flag it without opening a no-address routing bypass.
      * ``createPolicy`` (request schema depth 16) / ``getKrakenUser`` (response depth 21) —
        nesting past the depth cap is UNSCANNABLE, so it fails closed (a separate control,
        not the address class).

    The honest answer for all 6 is confirm-to-unquarantine by the provider, NOT loosening a
    rule. ``transfer`` / ``createUser`` / ``getWallet`` dropped out (pure address FP, fixed).
    """
    privy = _by_name()["privy"]
    assert set(privy.quarantined) == {  # type: ignore[attr-defined]
        "getTransaction",
        "getTransactionByReferenceId",
        "signWithWallet",
        "withdrawFunds",
        "createPolicy",
        "getKrakenUser",
    }


def test_only_declared_mint_providers_join_the_mint_domain() -> None:
    mint = {r.name for r in all_reports() if r.mint_domain}
    assert mint == MINT_PROVIDERS
    for r in all_reports():
        if r.name in MINT_PROVIDERS:
            assert r.value_domains == (MINT_ENTITY,)
        else:
            assert r.value_domains == ()


def test_cross_provider_mint_correlation_spans_all_three_providers() -> None:
    """Pegana ↔ Birdeye ↔ Jupiter join on the mint value-domain (not on name/signature —
    each spec names the mint differently). Every cross-provider pair is found, plan-eligible
    (customer-confirmed), and provenance-tagged."""
    corr = mint_correlation()
    assert corr.entities == (MINT_ENTITY,)
    # all three providers produce AND consume the mint entity.
    prod_surfaces = {s for s, _, _ in corr.producers}
    cons_surfaces = {s for s, _, _ in corr.consumers}
    assert MINT_PROVIDERS <= prod_surfaces
    assert MINT_PROVIDERS <= cons_surfaces

    pairs = {(p, c) for p, c, _entity, _eligible in corr.cross_joins}
    # both directions across every provider pair — a real cross-provider mesh.
    for a, b in (
        ("pegana_p0", "birdeye"),
        ("birdeye", "pegana_p0"),
        ("pegana_p0", "jupiter"),
        ("jupiter", "pegana_p0"),
        ("birdeye", "jupiter"),
        ("jupiter", "birdeye"),
    ):
        assert (a, b) in pairs, f"missing cross join {a} -> {b}"
    # every cross join carries the mint entity and is plan-eligible (all sides confirmed).
    assert all(entity == MINT_ENTITY for _p, _c, entity, _e in corr.cross_joins)
    assert all(eligible for _p, _c, _entity, eligible in corr.cross_joins)


def test_pegana_birdeye_declared_edge_is_tier3_declared() -> None:
    """The pairwise Pegana ``mint`` (produced) -> Birdeye ``address`` (consumed) edge is a
    DECLARED tier-3 cross-API link — the names differ, only the value-domain bridges them."""
    corr = mint_correlation()
    edges = [
        lk
        for lk in corr.pegana_birdeye.links
        if lk.basis.src_surface == "pegana_p0"
        and lk.basis.dst_surface == "birdeye"
        and lk.basis.src_field == "mint"
        and lk.basis.dst_param == "address"
    ]
    assert edges, "expected a DECLARED Pegana mint -> Birdeye address cross link"
    edge = edges[0]
    assert edge.basis.tier == 3
    assert edge.basis.provenance == "DECLARED"
    assert edge.basis.entity == MINT_ENTITY
    assert edge.plan_eligible
    assert f"declared:{MINT_ENTITY}" in edge.basis.signals


def test_exit_liquidity_safe_chain_composes_clean_and_crosses_to_pegana() -> None:
    """The flagship chain: Pegana (peg oracle) → Birdeye (exit liquidity), joined by the
    confirmed mint. It composes complete, clean (no quarantined hop), keyless, and actually
    CROSSES the boundary — a Pegana hop sources the mint Birdeye's exit op consumes."""
    chain = exit_liquidity_chain()
    assert chain is not None, "confirmed mint join should compose a safe chain"
    assert chain.complete and not chain.refused
    assert chain.safe
    assert not chain.quarantined_nodes
    assert chain.target_tool == "get-defi-multi_price"
    assert chain.nodes[-1].tool == "get-defi-multi_price"
    assert any(n.surface_id == "pegana_p0" for n in chain.nodes), (
        "the chain must cross to Pegana, not self-supply intra-Birdeye"
    )
    assert chain.join_basis == (f"declared:{MINT_ENTITY}",)

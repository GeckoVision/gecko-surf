"""The gecko-orquestra provider CLI — entry = provider, program = a parameter."""

from __future__ import annotations

import pytest

from gecko.providers.cli import PROGRAMS, main

# Each servable program: (name, expected program_id, orquestra slug, a known derive_pda
# case → real mainnet address). These reuse the ground truth pinned in each program's own
# test module; here we prove the *registry-built* surface derives them (servable, end to
# end through PROGRAMS[...]()).
_SERVABLE = {
    "meteora": {
        "program_id": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
        "slug": "v48gsz901w84zriqe0elsl",
        "account": "lb_pair",
        # A real CURRENT (post-May-2024, require_base_factor_seed == 1) mainnet pool —
        # derived via the 4-seed `derive_lb_pair_pda2` scheme (bin_step + base_factor).
        "bindings": {
            "token_x_mint": "Df6yfrKC8kZE3KNkrHERKzAetSxbrWeniQfyJY4Jpump",
            "token_y_mint": "So11111111111111111111111111111111111111112",
            "bin_step": 250,
            "base_factor": 4000,
        },
        "address": "EtAdVRLFH22rjWh3mcUasKFF27WtHhsaCvK27tPFFWig",
    },
    "pumpfun": {
        "program_id": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        "slug": "6i6q26bmm46b89xlxo1kv",
        "account": "bonding_curve",
        "bindings": {"mint": "8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump"},
        "address": "EExN5XXyaaE3G3w93WdKbJgMUAH3sFgLJBsg5crNp3tH",
    },
    "ore": {
        "program_id": "oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv",
        "slug": "6alwvs9936laepljczqumb",
        "account": "config",
        "bindings": {},
        "address": "9c9X7aDRAF41faiDs94ELjT19UrGnn72wBW9hPsS4Awy",
    },
    "jupiter": {
        "program_id": "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
        "slug": "1y8gp8l6eixrl0ume2bzwo",
        "account": "event_authority",
        "bindings": {},
        "address": "D8cy77BBepLMngZx6ZukaTff5hCt1HrWyKk3Hnd9oitf",
    },
    "metadao_ico": {
        "program_id": "moontUzsdepotRGe5xsfip7vLPTJnVuafqdUWexVnPM",
        "slug": "krhmrxpy2fgwn3q0whic7",
        "account": "launch",
        "bindings": {"base_mint": "Hszh6zhqhfR6vv27dQi5rARjvdXyaobGcVhj3t73meta"},
        "address": "1AEZZsShFCnC8UUetqk9hGby66Q5mgyszDHdXjAmdYe",
    },
    "whirlpool": {
        "program_id": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
        "slug": "4tj9k6117wn4xla89szsj",
        "account": "whirlpool",
        # The USDG/USDC ts=1 pool four Gecko-built swap_v2 transactions landed on —
        # config + both mints + tick_spacing derive the account holding $25M of TVL.
        "bindings": {
            "whirlpools_config": "2LecshUwdy9xi7meFgHtFJQNSKk4KdTrcpvaB56dP2NQ",
            "token_mint_a": "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH",
            "token_mint_b": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "tick_spacing": 1,
        },
        "address": "9RqDTfwCx2SgxsvKpspQHc38HUo3B6hRd3oR9JR966Ps",
    },
}


def test_registry_matches_the_servable_set() -> None:
    # Every wired program must be servable — the registry and the fixture set
    # are kept in lockstep so adding one without a smoke fixture fails here.
    assert set(PROGRAMS) == set(_SERVABLE)


@pytest.mark.parametrize("name", sorted(_SERVABLE))
def test_registered_surface_is_servable(name: str) -> None:
    spec = _SERVABLE[name]
    surface = PROGRAMS[name]()  # the registered builder yields a working surface
    assert surface.program_id == spec["program_id"]
    assert surface.project_base_url == f"https://api.orquestra.dev/api/{spec['slug']}"
    # every servable program exposes the derive + graph tools
    tool_names = {t["name"] for t in surface.list_tools()}
    assert {"get_program_graph", "derive_pda"} <= tool_names
    # …and derives the known real mainnet address, first call correct
    out = surface.call_tool(
        "derive_pda", {"account": spec["account"], "bindings": spec["bindings"]}
    )
    assert out["address"] == spec["address"]


def test_meteora_keeps_its_plan_intent() -> None:
    # Wiring the derivation-only programs must not disturb Meteora's plan_swap intent.
    tool_names = {t["name"] for t in PROGRAMS["meteora"]().list_tools()}
    assert "plan_swap" in tool_names


def test_program_is_required() -> None:
    with pytest.raises(SystemExit):
        main([])  # no --program


def test_unknown_program_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["--program", "nope"])  # not in the registry


@pytest.mark.parametrize("name", ["pumpfun", "ore", "metadao_ico"])
def test_new_programs_are_valid_program_choices(name: str) -> None:
    # `gecko-orquestra --program <p> --help` exits 0 → argparse accepts it as a choice.
    with pytest.raises(SystemExit) as exc:
        main(["--program", name, "--help"])
    assert exc.value.code == 0


def test_gecko_orquestra_subcommand_dispatches() -> None:
    """`gecko orquestra …` (and thus `npx @geckovision/gecko orquestra …`) reaches the
    provider CLI — 'orquestra' is a known subcommand, not defaulted to serve."""
    from gecko.cli import _SUBCOMMANDS
    from gecko.cli import main as gecko_main

    assert "orquestra" in _SUBCOMMANDS
    with pytest.raises(SystemExit) as exc:
        gecko_main(
            ["orquestra", "--program", "meteora", "--help"]
        )  # argparse --help exits 0
    assert exc.value.code == 0

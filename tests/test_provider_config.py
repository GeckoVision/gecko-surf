"""The config-driven provider backbone — PDA recipes and program identity as DATA.

See docs/specs/2026-08-01-provider-control-panel.md (§B0) and
docs/plans/2026-08-01-provider-control-panel-pr1-config-backbone.md.
"""

from __future__ import annotations

from gecko.pda import derive_pda
from gecko.provider_config import (
    ConfigError,
    api_config_from_dict,
    load_packaged_provider,
    node_from_spec,
    seed_from_spec,
)

METEORA = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

LB_PAIR_SPEC = {
    "program_id": METEORA,
    "seeds": [
        {
            "kind": "ordered_pair",
            "left": "token_x_mint",
            "right": "token_y_mint",
            "select": "min",
        },
        {
            "kind": "ordered_pair",
            "left": "token_x_mint",
            "right": "token_y_mint",
            "select": "max",
        },
        {
            "kind": "variable",
            "name": "bin_step",
            "source": "argument",
            "encoding": "le",
            "width": 2,
        },
    ],
}


def test_lb_pair_spec_derives_real_pool() -> None:
    node = node_from_spec("lb_pair", LB_PAIR_SPEC)
    got = derive_pda(node, {"token_x_mint": SOL, "token_y_mint": USDC, "bin_step": 4})
    assert got.address == "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"


def test_constant_utf8_seed_bytes() -> None:
    seed = seed_from_spec({"kind": "constant", "value": "oracle", "encoding": "utf8"})
    assert seed.value == b"oracle"


def test_constant_pubkey_seed_decodes_to_32_bytes() -> None:
    # a hardcoded program/account address baked into a seed (e.g. the SPL Token
    # program in an ATA recipe): the base58 string decodes to the raw 32 pubkey bytes.
    seed = seed_from_spec(
        {
            "kind": "constant",
            "value": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "encoding": "pubkey",
        }
    )
    assert seed.encoding == "pubkey"
    assert isinstance(seed.value, bytes)
    assert len(seed.value) == 32


def test_constant_pubkey_seed_rejects_bad_base58() -> None:
    try:
        seed_from_spec(
            {"kind": "constant", "value": "not-a-pubkey!", "encoding": "pubkey"}
        )
        assert False, "expected ConfigError"
    except ConfigError:
        pass


def test_unknown_seed_kind_rejected() -> None:
    try:
        seed_from_spec({"kind": "nope"})
        assert False, "expected ConfigError"
    except ConfigError:
        pass


METEORA_API = {
    "api_id": "meteora",
    "kind": "program",
    "spec_source": {"type": "program", "value": METEORA},
    "program": {
        "program_id": METEORA,
        "orquestra_project": "v48gsz901w84zriqe0elsl",
        "intents": ["plan_swap"],
        "pdas": {"lb_pair": LB_PAIR_SPEC},
    },
    "auth": {"scheme": "none", "account_ref": "keyring:meteora", "injected": True},
}


def test_api_config_builds_program_pdas() -> None:
    cfg = api_config_from_dict(METEORA_API)
    assert cfg.kind == "program"
    assert cfg.program is not None
    assert cfg.program.orquestra_project == "v48gsz901w84zriqe0elsl"
    assert cfg.program.intents == ("plan_swap",)
    assert "lb_pair" in cfg.program.pdas  # already a PdaNode, ready to derive


def test_inline_secret_in_auth_is_rejected() -> None:
    bad = {
        **METEORA_API,
        "auth": {"scheme": "bearer", "account_ref": "sk_live_ABC123DEF456"},
    }
    try:
        api_config_from_dict(bad)
        assert False, "expected ConfigError for inline secret"
    except ConfigError:
        pass


def test_packaged_orquestra_loads_and_derives() -> None:
    provider, apis = load_packaged_provider("orquestra")
    assert provider.provider_id == "orquestra"
    assert "meteora" in apis
    program = apis["meteora"].program
    assert program is not None
    node = program.pdas["lb_pair"]
    # the packaged recipe is the 4-seed `derive_lb_pair_pda2` scheme → a real CURRENT pool
    got = derive_pda(
        node,
        {
            "token_x_mint": "Df6yfrKC8kZE3KNkrHERKzAetSxbrWeniQfyJY4Jpump",
            "token_y_mint": SOL,
            "bin_step": 250,
            "base_factor": 4000,
        },
    )
    assert got.address == "EtAdVRLFH22rjWh3mcUasKFF27WtHhsaCvK27tPFFWig"


def test_resolver_seed_kind_is_honest_gap() -> None:
    # a seed we cannot statically resolve (a field inside another account's data)
    # becomes a ResolverPdaSeedNode — never fabricated, dependencies declared.
    from gecko.pda import ResolverPdaSeedNode

    seed = seed_from_spec(
        {
            "kind": "resolver",
            "name": "creator",
            "depends_on": ["bonding_curve"],
            "reason": "bonding_curve.data.creator — fetch + Anchor-decode",
        }
    )
    assert isinstance(seed, ResolverPdaSeedNode)
    assert seed.depends_on == ("bonding_curve",)
    assert "creator" in seed.reason


def test_program_orquestra_project_is_optional() -> None:
    # a program can be comprehended for derivation before its execute/build URL is wired
    data = {
        "api_id": "someprog",
        "kind": "program",
        "spec_source": {"type": "program", "value": METEORA},
        "program": {
            "program_id": METEORA,
            "intents": [],
            "pdas": {"lb_pair": LB_PAIR_SPEC},
        },
    }
    cfg = api_config_from_dict(data)
    assert cfg.program is not None
    assert cfg.program.orquestra_project is None

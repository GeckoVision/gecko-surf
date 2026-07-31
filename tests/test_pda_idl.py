"""Phase 2a: Anchor-IDL extraction + the source/IDL join ("both, joined").

from_anchor_idl reads the array-form seed metadata Anchor keeps; merge_pda_nodes
fills the recipes Anchor DROPPED (the #4057 case) from source recovery. Tied to the
same ORE mainnet ground truth: an IDL const-seed derives to the real deployed
address, and an IDL-recovered dynamic PDA derives identically to the source-recovered
one.
"""

from __future__ import annotations

from gecko.pda import ResolverPdaSeedNode, VariablePdaSeedNode, derive_pda
from gecko.pda_extract import from_anchor_idl, from_source, merge_pda_nodes
from tests.test_pda_extract import ORE_PROGRAM, ORE_SOURCE

ORE_CONFIG = "9c9X7aDRAF41faiDs94ELjT19UrGnn72wBW9hPsS4Awy"

# byte values of the ORE seed strings (what an Anchor IDL stores in `const.value`)
_B = lambda s: [b for b in s.encode()]  # noqa: E731

# A faithful Anchor 0.30 IDL over the ORE program id: array-form seeds for config
# (const), round (const + u64 arg), miner (const + account); a vault whose seed
# reads another account's field (opaque runtime data → honest resolver).
ANCHOR_IDL = {
    "address": ORE_PROGRAM,
    "metadata": {"name": "demo", "version": "0.1.0", "spec": "0.1.0"},
    "instructions": [
        {
            "name": "init_config",
            "accounts": [
                {
                    "name": "config",
                    "pda": {"seeds": [{"kind": "const", "value": _B("config")}]},
                },
                {"name": "signer", "signer": True},
            ],
            "args": [],
        },
        {
            "name": "open_round",
            "accounts": [
                {
                    "name": "round",
                    "pda": {
                        "seeds": [
                            {"kind": "const", "value": _B("round")},
                            {"kind": "arg", "path": "id"},
                        ]
                    },
                },
                {
                    "name": "miner",
                    "pda": {
                        "seeds": [
                            {"kind": "const", "value": _B("miner")},
                            {"kind": "account", "path": "authority"},
                        ]
                    },
                },
                {"name": "authority", "signer": True},
            ],
            "args": [{"name": "id", "type": "u64"}],
        },
        {
            "name": "deposit",
            "accounts": [
                {
                    "name": "vault",
                    "pda": {
                        "seeds": [
                            {"kind": "const", "value": _B("vault")},
                            {"kind": "account", "path": "pool.mint"},
                        ]
                    },
                }
            ],
            "args": [],
        },
    ],
}


def test_from_anchor_idl_recovers_pda_accounts() -> None:
    nodes = from_anchor_idl(ANCHOR_IDL)
    assert set(nodes) == {"config", "round", "miner", "vault"}
    assert all(n.program_id == ORE_PROGRAM for n in nodes.values())


def test_idl_const_seed_derives_to_mainnet_ground_truth() -> None:
    """An IDL const seed [b"config"] under the ORE program derives to the exact
    deployed mainnet address — the array-form seed metadata is enough."""
    nodes = from_anchor_idl(ANCHOR_IDL)
    assert derive_pda(nodes["config"]).address == ORE_CONFIG


def test_idl_arg_and_account_seed_shapes() -> None:
    nodes = from_anchor_idl(ANCHOR_IDL)
    assert nodes["round"].variable_seeds == (
        VariablePdaSeedNode("id", source="argument", encoding="le", width=8),
    )
    assert nodes["miner"].variable_seeds == (
        VariablePdaSeedNode("authority", source="account", encoding="pubkey"),
    )


def test_idl_and_source_agree_on_dynamic_pda() -> None:
    """The same dynamic recipe recovered two independent ways — from the Anchor IDL
    and from Rust source — derives to the identical address. Cross-validation."""
    idl_miner = from_anchor_idl(ANCHOR_IDL)["miner"]
    src_miner = from_source(ORE_SOURCE, program_id=ORE_PROGRAM)["miner"]
    a = derive_pda(idl_miner, {"authority": ORE_CONFIG})
    b = derive_pda(src_miner, {"authority": ORE_CONFIG})
    assert a.address == b.address


def test_idl_account_field_seed_is_honest_resolver() -> None:
    """A seed that reads another account's field (`pool.mint`) is runtime data, not
    statically reproducible — flagged, not fabricated."""
    vault = from_anchor_idl(ANCHOR_IDL)["vault"]
    assert vault.resolvable is False
    resolver = vault.unresolved_seeds[0]
    assert isinstance(resolver, ResolverPdaSeedNode)
    assert "pool" in resolver.depends_on


def test_merge_fills_the_dropped_recipe_from_source() -> None:
    """The #4057 case: Anchor drops config's pda block entirely (opaque seed), so the
    IDL has no config recipe — merge_pda_nodes recovers it from source, and it
    derives to the real mainnet address."""
    # simulate Anchor dropping config's pda block
    idl_missing = {
        **ANCHOR_IDL,
        "instructions": [
            {
                "name": "init_config",
                "accounts": [{"name": "config"}],  # no `pda` — dropped by Anchor
                "args": [],
            }
        ],
    }
    idl_nodes = from_anchor_idl(idl_missing)
    assert "config" not in idl_nodes  # the gap

    source_nodes = from_source(ORE_SOURCE, program_id=ORE_PROGRAM)
    merged = merge_pda_nodes(idl_nodes, source_nodes)

    assert "config" in merged  # source rescued it
    assert derive_pda(merged["config"]).address == ORE_CONFIG


def test_merge_prefers_source_when_idl_seed_is_opaque() -> None:
    """When the IDL has an account but its seed is opaque (resolver) and source has a
    resolvable recipe for the same name, the join takes the source node."""
    idl_nodes = from_anchor_idl(ANCHOR_IDL)  # vault is a resolver here
    assert idl_nodes["vault"].resolvable is False

    # a source recovery that DOES resolve `vault`
    vault_source = from_source(
        'pub const VAULT: &[u8] = b"vault";\n'
        "pub fn vault_pda(owner: Pubkey) -> (Pubkey, u8) {\n"
        "    Pubkey::find_program_address(&[VAULT, &owner.to_bytes()], &crate::ID)\n"
        "}\n",
        program_id=ORE_PROGRAM,
    )
    assert vault_source["vault"].resolvable is True

    merged = merge_pda_nodes(idl_nodes, vault_source)
    assert merged["vault"].resolvable is True  # source won
    # IDL-authoritative accounts untouched
    assert merged["config"].program_id == ORE_PROGRAM

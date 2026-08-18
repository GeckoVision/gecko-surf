"""Phase 2a: Anchor-IDL extraction + the source/IDL join ("both, joined").

from_anchor_idl reads the array-form seed metadata Anchor keeps; merge_pda_nodes
fills the recipes Anchor DROPPED (the #4057 case) from source recovery. Tied to the
same ORE mainnet ground truth: an IDL const-seed derives to the real deployed
address, and an IDL-recovered dynamic PDA derives identically to the source-recovered
one.
"""

from __future__ import annotations

from gecko.pda import (
    ConstantPdaSeedNode,
    ResolverPdaSeedNode,
    VariablePdaSeedNode,
    derive_pda,
)
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


# ---------------------------------------------------------------------------
# legacy (pre-0.30) IDLs — the const seed that crashed a whole program's graph
# ---------------------------------------------------------------------------


def _legacy_idl(seed: dict[str, object]) -> dict[str, object]:
    """A one-instruction pre-0.30 IDL whose only PDA carries `seed`."""
    return {
        "address": "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya",
        "instructions": [
            {
                "name": "swap",
                "args": [],
                "accounts": [{"name": "state", "pda": {"seeds": [seed]}}],
            }
        ],
    }


def test_legacy_string_const_seed_does_not_crash_the_program() -> None:
    """Pre-0.30 Anchor writes a const seed as a TYPED LITERAL, not a byte array:

        {"kind": "const", "type": "string", "value": "bonkswapstatev1"}

    `bytes("bonkswapstatev1")` raises `TypeError: string argument without an
    encoding`, and because from_anchor_idl builds every instruction in one pass a
    single such seed took down the WHOLE program's graph. Measured on a live
    corpus: 52 of one program's 109 seeds, and the one hard failure in a 60-program
    sample — a share that only grows down the long tail, where the older IDLs live.
    """
    nodes = from_anchor_idl(
        _legacy_idl({"kind": "const", "type": "string", "value": "bonkswapstatev1"})
    )

    assert "state" in nodes, "a legacy const seed must not lose the account"
    (seed,) = nodes["state"].seeds
    assert isinstance(seed, ConstantPdaSeedNode)
    assert seed.value == b"bonkswapstatev1"
    assert seed.encoding == "utf8"


def test_the_modern_array_form_still_wins() -> None:
    """The 0.30 shape is 1,047 of 2,382 seeds in the same corpus — it must not regress."""
    nodes = from_anchor_idl(
        _legacy_idl({"kind": "const", "value": [114, 101, 99, 101, 105, 112, 116, 115]})
    )
    (seed,) = nodes["state"].seeds
    assert isinstance(seed, ConstantPdaSeedNode)
    assert seed.value == b"receipts"


def test_an_unmeasured_const_shape_is_flagged_rather_than_guessed() -> None:
    """A typed literal we have never observed becomes an honest resolver, not a guess.

    `{"type": "publicKey", "value": "<base58>"}` is plausible and appears ZERO times
    in the measured corpus. Decoding it would mean adding a base58 dependency to a
    comprehension module on the strength of a guess, and guessing a seed's bytes is
    how you derive a valid address that belongs to somebody else. Refuse, name it,
    and add it when a real IDL shows one.
    """
    nodes = from_anchor_idl(
        _legacy_idl(
            {
                "kind": "const",
                "type": "publicKey",
                "value": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            }
        )
    )
    (seed,) = nodes["state"].seeds
    assert isinstance(seed, ResolverPdaSeedNode)
    assert "publicKey" in seed.reason


# ---------------------------------------------------------------------------
# the self-referential root PDA — measured live on jurassic_fi_token_sale
# ---------------------------------------------------------------------------


def _self_referential_idl() -> dict[str, object]:
    """The shape jurassic_fi ships, reduced to its two load-bearing instructions.

    `claim` declares the root PDA from fields stored INSIDE it, which is a correct runtime
    check for the program and a dead end for a caller. `initialize_launch` declares the SAME
    PDA derivably, because at creation there is no account to read from. The IDL lists
    `claim` first.
    """
    return {
        "address": "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm",
        "instructions": [
            {
                "name": "claim",
                "args": [],
                "accounts": [
                    {
                        "name": "launch",
                        "pda": {
                            "seeds": [
                                {
                                    "kind": "const",
                                    "value": [108, 97, 117, 110, 99, 104],
                                },
                                {
                                    "kind": "account",
                                    "path": "launch.admin",
                                    "account": "Launch",
                                },
                                {
                                    "kind": "account",
                                    "path": "launch.launch_id",
                                    "account": "Launch",
                                },
                            ]
                        },
                    }
                ],
            },
            {
                "name": "initialize_launch",
                "args": [{"name": "params", "type": {"defined": "LaunchParams"}}],
                "accounts": [
                    {"name": "admin"},
                    {
                        "name": "launch",
                        "pda": {
                            "seeds": [
                                {
                                    "kind": "const",
                                    "value": [108, 97, 117, 110, 99, 104],
                                },
                                {"kind": "account", "path": "admin"},
                                {"kind": "arg", "path": "params.launch_id"},
                            ]
                        },
                    },
                ],
            },
        ],
        # The width is the whole difficulty, and the IDL answers it. Verified against the
        # live account: launch_id at u8, u16 and u32 each derive a DIFFERENT valid address,
        # and only u64 matches — so reading this rather than defaulting is the fix.
        "types": [
            {
                "name": "LaunchParams",
                "type": {
                    "kind": "struct",
                    "fields": [
                        {"name": "launch_id", "type": "u64"},
                        {"name": "raise_cap", "type": "u64"},
                    ],
                },
            }
        ],
    }


def test_a_resolvable_sibling_recipe_beats_a_self_referential_one() -> None:
    """First-declaration-wins threw away the only usable recipe in the program.

    Measured on jurassic_fi_token_sale, a live token sale holding 323,816 USDC: seven of its
    eight instructions declare `launch` from `launch.admin` and `launch.launch_id` — fields
    of the account being derived. Only `initialize_launch` states it derivably. Because the
    IDL lists `claim` first and this function skipped any name it had already seen, the
    resolvable recipe was silently discarded and SIX instructions became uncallable: three
    more accounts (`user_position`, `payment_vault`, `token_vault`) seed on `launch`.

    Order is not evidence. A resolvable declaration is.
    """
    nodes = from_anchor_idl(_self_referential_idl())

    assert "launch" in nodes
    launch = nodes["launch"]
    assert launch.resolvable, (
        "the derivable sibling recipe must win over the self-referential one, "
        f"got seeds {launch.seeds}"
    )
    # and it is the RIGHT recipe: const 'launch', then the admin account, then the arg
    const, admin, launch_id = launch.seeds
    assert isinstance(const, ConstantPdaSeedNode) and const.value == b"launch"
    assert isinstance(admin, VariablePdaSeedNode) and admin.name == "admin"
    assert isinstance(launch_id, (VariablePdaSeedNode, ResolverPdaSeedNode))


def _foreign_account_read_idl() -> dict[str, object]:
    """pump.fun's shape: the same PDA, blocked on a read of a DIFFERENT account.

    Nine instructions seed `creator_vault` on `bonding_curve.creator` — data inside a
    bonding curve, not inside the vault being derived. `collect_creator_fee` seeds it on a
    plain `creator` account, which that instruction happens to take and `buy` does not.
    """
    return {
        "address": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        "instructions": [
            {
                "name": "buy",
                "args": [],
                "accounts": [
                    {"name": "bonding_curve"},
                    {
                        "name": "creator_vault",
                        "pda": {
                            "seeds": [
                                {"kind": "const", "value": list(b"creator-vault")},
                                {
                                    "kind": "account",
                                    "path": "bonding_curve.creator",
                                    "account": "BondingCurve",
                                },
                            ]
                        },
                    },
                ],
            },
            {
                "name": "collect_creator_fee",
                "args": [],
                "accounts": [
                    {"name": "creator"},
                    {
                        "name": "creator_vault",
                        "pda": {
                            "seeds": [
                                {"kind": "const", "value": list(b"creator-vault")},
                                {"kind": "account", "path": "creator"},
                            ]
                        },
                    },
                ],
            },
        ],
    }


def test_a_sibling_recipe_does_not_win_over_a_read_of_another_account() -> None:
    """The limit on the rule above, and pump.fun is the case that sets it.

    Promoting `collect_creator_fee`'s recipe program-wide would report `creator_vault` as
    cleanly derivable to a `buy` caller — who has no `creator` account in that instruction
    at all, and no way to compute one. Unlike the self-referential case, nothing proves the
    two inputs carry the same value: `creator` is a caller-supplied account the program
    checks, not a field the seeds constraint pins.

    So `buy` keeps an honest flagged resolver naming `bonding_curve`, which is a real gap a
    caller can close with a chain read, rather than a derivable-looking wrong answer.
    """
    for idl in (_foreign_account_read_idl(), None):
        if idl is None:  # and the same answer with the instructions the other way round
            idl = _foreign_account_read_idl()
            idl["instructions"] = list(reversed(idl["instructions"]))  # type: ignore[index]
        vault = from_anchor_idl(idl)["creator_vault"]
        assert not vault.resolvable, (
            "a recipe blocked on ANOTHER account's data must stay flagged, "
            f"got seeds {vault.seeds}"
        )
        blocker = next(s for s in vault.seeds if isinstance(s, ResolverPdaSeedNode))
        assert blocker.depends_on == ("bonding_curve",)


def test_declaration_order_does_not_decide_which_recipe_wins() -> None:
    """The same IDL with the instructions the other way round must give the same answer."""
    idl = _self_referential_idl()
    idl["instructions"] = list(reversed(idl["instructions"]))  # type: ignore[index]
    assert from_anchor_idl(idl)["launch"].resolvable


def test_a_fixed_size_byte_array_argument_is_a_bindable_seed() -> None:
    """`{"array": ["u8", 32]}` is 32 bytes, and 32 bytes are a seed.

    Measured across a live catalogue: after the sibling-recipe and arg-field fixes, only
    two patterns of unresolvable recipe were left. One is genuinely runtime data read off
    another account (`pool.base_mint`) and is correctly refused. The other was this — nine
    recipes refused with "unsupported seed type {'array': ['u8', 32]}", mostly on one
    program. A fixed-width byte array is passed through verbatim; there is nothing to
    infer and nothing to guess.
    """
    idl = {
        "address": "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm",
        "instructions": [
            {
                "name": "create_order",
                "args": [{"name": "nonce", "type": {"array": ["u8", 32]}}],
                "accounts": [
                    {
                        "name": "order",
                        "pda": {
                            "seeds": [
                                {"kind": "const", "value": [111, 114, 100]},
                                {"kind": "arg", "path": "nonce"},
                            ]
                        },
                    }
                ],
            }
        ],
    }
    node = from_anchor_idl(idl)["order"]
    assert node.resolvable, f"a [u8; 32] argument is bindable, got {node.seeds}"
    _, nonce = node.seeds
    assert isinstance(nonce, VariablePdaSeedNode)
    assert nonce.name == "nonce" and nonce.encoding == "bytes"


def test_an_array_of_something_other_than_bytes_is_still_refused() -> None:
    """`[u64; 4]` has an element width and an order we would have to assume. Refuse it."""
    idl = {
        "address": "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm",
        "instructions": [
            {
                "name": "x",
                "args": [{"name": "ids", "type": {"array": ["u64", 4]}}],
                "accounts": [
                    {
                        "name": "thing",
                        "pda": {"seeds": [{"kind": "arg", "path": "ids"}]},
                    }
                ],
            }
        ],
    }
    assert not from_anchor_idl(idl)["thing"].resolvable

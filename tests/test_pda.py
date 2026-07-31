"""The PDA seed-graph model + derivation, tested against REAL mainnet ground truth.

The oracle is the ORE program (Steel framework, no IDL) deployed on Solana mainnet:
its seed recipes live in `api/src/state/mod.rs` and the resulting addresses are
hardcoded in `api/src/consts.rs`. We recover the recipes as PdaNodes, derive, and
assert byte-for-byte equality with the on-chain constants — the exact join an IDL /
llms.txt loses. (Cross-checked on a surfpool mainnet fork: these addresses hold the
live ORE accounts.)
"""

from __future__ import annotations

import pytest

from gecko.pda import (
    ConstantPdaSeedNode,
    MissingBindingError,
    PdaError,
    PdaNode,
    ResolverPdaSeedNode,
    UnresolvedSeedError,
    VariablePdaSeedNode,
    derive_pda,
)

# ORE program id (mainnet) — api/src/lib.rs `declare_id!`.
ORE_PROGRAM = "oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv"

# Ground-truth PDAs from api/src/consts.rs (deployed on mainnet).
ORE_CONFIG = "9c9X7aDRAF41faiDs94ELjT19UrGnn72wBW9hPsS4Awy"
ORE_TREASURY = "45db2FSR4mcXdSVVZbKbwojU6uYDpMyhpEi7cC8nHaWG"
ORE_BOARD = "BrcSxdp1nXFzou1YyDnQJcPNBNHgoypZmTsyKBSLLXzi"


def _const(name: str, seed: bytes) -> PdaNode:
    return PdaNode(
        name=name,
        seeds=(ConstantPdaSeedNode(seed, encoding="utf8"),),
        program_id=ORE_PROGRAM,
    )


@pytest.mark.parametrize(
    "name,seed,expected",
    [
        ("config", b"config", ORE_CONFIG),
        ("treasury", b"treasury", ORE_TREASURY),
        ("board", b"board", ORE_BOARD),
    ],
)
def test_constant_pda_matches_mainnet_ground_truth(
    name: str, seed: bytes, expected: str
) -> None:
    """Single-constant-seed PDAs derive to their exact deployed mainnet address."""
    result = derive_pda(_const(name, seed))
    assert result.address == expected
    assert result.node.name == name
    assert 0 <= result.bump <= 255


def test_variable_pubkey_seed_derivation_is_stable_and_binding_dependent() -> None:
    """`miner_pda(authority) = [b"miner", authority.to_bytes()]` — a dynamic PDA the
    IDL can't express. Deriving the same authority twice is stable; a different
    authority yields a different address."""
    miner = PdaNode(
        name="miner",
        seeds=(
            ConstantPdaSeedNode(b"miner", encoding="utf8"),
            VariablePdaSeedNode("authority", source="account", encoding="pubkey"),
        ),
        program_id=ORE_PROGRAM,
    )
    authority_a = "HBUh9g46wk2X89CvaNN15UmsznP59rh6od1h8JwYAopk"  # ORE ADMIN_ADDRESS
    authority_b = ORE_CONFIG  # any other valid pubkey

    a1 = derive_pda(miner, {"authority": authority_a})
    a2 = derive_pda(miner, {"authority": authority_a})
    b = derive_pda(miner, {"authority": authority_b})

    assert a1.address == a2.address  # deterministic
    assert a1.address != b.address  # binding-dependent
    assert miner.variable_seeds == (
        VariablePdaSeedNode("authority", source="account", encoding="pubkey"),
    )


def test_variable_le_integer_seed_derivation() -> None:
    """`round_pda(id) = [b"round", id.to_le_bytes()]` for a u64 arg — an argument-
    sourced integer seed. Derives, and differs per id."""
    round_node = PdaNode(
        name="round",
        seeds=(
            ConstantPdaSeedNode(b"round", encoding="utf8"),
            VariablePdaSeedNode("id", source="argument", encoding="le", width=8),
        ),
        program_id=ORE_PROGRAM,
    )
    r0 = derive_pda(round_node, {"id": 0})
    r1 = derive_pda(round_node, {"id": 1})
    assert r0.address != r1.address
    assert len(r0.address) >= 32  # base58 pubkey


def test_program_id_argument_overrides_node() -> None:
    """A node recovered before the program id is known can be derived by passing
    program_id= at call time."""
    node = PdaNode(
        name="config", seeds=(ConstantPdaSeedNode(b"config", encoding="utf8"),)
    )
    assert node.program_id is None
    result = derive_pda(node, program_id=ORE_PROGRAM)
    assert result.address == ORE_CONFIG


# --- honesty + error paths -------------------------------------------------


def test_resolver_seed_makes_node_unresolvable_and_refuses_derivation() -> None:
    """The differentiator: an unresolved seed is flagged, not fabricated. The node
    reports it as a gap and derive_pda refuses rather than guessing an address."""
    lb_pair = PdaNode(
        name="lb_pair",
        seeds=(
            ConstantPdaSeedNode(b"lb_pair", encoding="utf8"),
            # Meteora orders the pair with max_key(token_x, token_y) — Anchor silently
            # drops this; we keep it as an honest, dependency-declaring placeholder.
            ResolverPdaSeedNode(
                name="ordered_pair",
                depends_on=("token_x", "token_y"),
                reason="max_key(token_x, token_y) — helper-fn seed, not statically resolvable",
            ),
        ),
        program_id=ORE_PROGRAM,
    )
    assert lb_pair.resolvable is False
    assert lb_pair.unresolved_seeds[0].depends_on == ("token_x", "token_y")

    with pytest.raises(UnresolvedSeedError) as exc:
        derive_pda(lb_pair, {"token_x": ORE_CONFIG, "token_y": ORE_TREASURY})
    assert "ordered_pair" in str(exc.value)


def test_missing_binding_raises() -> None:
    node = PdaNode(
        name="miner",
        seeds=(
            ConstantPdaSeedNode(b"miner", encoding="utf8"),
            VariablePdaSeedNode("authority", source="account", encoding="pubkey"),
        ),
        program_id=ORE_PROGRAM,
    )
    with pytest.raises(MissingBindingError):
        derive_pda(node, {})  # no authority


def test_missing_program_id_raises() -> None:
    node = PdaNode(name="config", seeds=(ConstantPdaSeedNode(b"config"),))
    with pytest.raises(PdaError):
        derive_pda(node)


def test_integer_seed_requires_width() -> None:
    with pytest.raises(PdaError):
        VariablePdaSeedNode("id", source="argument", encoding="le")  # no width


def test_constant_seed_rejects_non_bytes() -> None:
    with pytest.raises(PdaError):
        ConstantPdaSeedNode("config")  # type: ignore[arg-type]  # must be bytes, not str


def test_resolvable_node_reports_variable_seeds_and_no_gaps() -> None:
    node = PdaNode(
        name="miner",
        seeds=(
            ConstantPdaSeedNode(b"miner", encoding="utf8"),
            VariablePdaSeedNode("authority", source="account", encoding="pubkey"),
        ),
        program_id=ORE_PROGRAM,
    )
    assert node.resolvable is True
    assert node.unresolved_seeds == ()
    assert [s.name for s in node.variable_seeds] == ["authority"]

"""The jurassic_fi lifecycle chain — contribute, then claim.

Jurassic Finance sells a fractionalised museum-grade Triceratops skull ($TRCH1) on
mainnet. A contributor pays USDC into an open launch and claims tokens once it settles.
The IDL states each instruction's accounts and args; it never states that `claim` pays
out a position only a landed `contribute` wrote. That join is what this chain declares,
and these tests hold the declaration to what the live surface actually says.

The chain was authored from the live IDL's own account lists (via Orquestra's
`list_instructions`), so the tests assert the two facts that authoring could have got
wrong: the linking account really is on both instructions, and the width that decides
the launch address is the recovered one, not an assumed one.
"""

from __future__ import annotations

import json
from pathlib import Path

from gecko.find_start import declared_chains, find_start

CONFIG = (
    Path(__file__).resolve().parents[1]
    / "gecko"
    / "providers"
    / "configs"
    / "orquestra"
    / "jurassic_fi.json"
)


def _chain():
    chains = declared_chains("jurassic_fi")
    assert len(chains) == 1
    return chains[0]


def test_the_linking_account_is_named_by_both_instructions() -> None:
    """An edge that claims two calls address one account is only true if both
    instructions actually carry it. This is the assertion that would have caught a
    chain authored from memory rather than from the surface."""
    chain = _chain()
    edge = chain.edges[0]
    carrying = {
        step.instruction: step.spec.accounts
        for step in chain.steps
        if edge.account in step.spec.accounts
    }

    assert edge.account == "user_position"
    assert set(carrying) == {"contribute", "claim"}


def test_the_edge_says_what_would_refute_it() -> None:
    """A declared edge that cannot be refuted is an opinion. Ours names the observation
    that would kill it."""
    edge = _chain().edges[0]

    assert edge.basis
    assert "user_position" in edge.basis
    assert "contribute" in edge.refuted_by


def test_the_launch_id_seed_keeps_its_recovered_width() -> None:
    """The whole demo rests on this. ``params.launch_id`` is a u64; read at u8 it derives
    a different, perfectly VALID address — correctly formatted, resolvable, and not the
    sale. Nothing downstream can detect that, so the width has to survive here."""
    config = json.loads(CONFIG.read_text())
    seeds = config["program"]["pdas"]["launch"]["seeds"]
    launch_id = [s for s in seeds if s.get("name") == "params.launch_id"]

    assert len(launch_id) == 1, seeds
    assert launch_id[0]["width"] == 8
    assert launch_id[0]["encoding"] == "le"


def test_the_launch_account_is_seeded_on_its_own_fields() -> None:
    """The reason a catalogue dead-ends here, asserted rather than described: two of the
    three seeds are fields of the account being derived."""
    config = json.loads(CONFIG.read_text())
    seeds = config["program"]["pdas"]["launch"]["seeds"]
    names = [s.get("value") or s.get("name") for s in seeds]

    assert names == ["launch", "admin", "params.launch_id"]


def test_contributing_to_a_launch_routes_to_the_sale_not_to_a_storefront() -> None:
    """The routing this chain exists to make possible."""
    result = find_start("contribute to a launch", limit=3)

    top = result.starts[0]
    assert (top.program, top.instruction) == ("jurassic_fi", "contribute")
    assert top.kind == "start"


def test_the_claim_step_keeps_its_honest_gap() -> None:
    """``dust_token_account`` reads as clean in the IDL and is not buildable from it.
    A gap that quietly disappears is worse than one that is stated."""
    claim = next(s for s in _chain().steps if s.instruction == "claim")

    gaps = {gap.name for gap in claim.spec.gaps}
    assert "dust_token_account" in gaps

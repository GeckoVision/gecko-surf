"""The instruction ordering, recovered from seed shapes alone.

WHY IT IS RECOVERABLE AT ALL. An Anchor program that stores its own seed values gives
itself away: the instruction that CREATES an account must state that account's seeds
derivably, because at creation there is nothing to read them from. Every later instruction
states the same account from its own stored fields. The split is the lifecycle.

Every case here is the shape of a live program (jurassic_fi_token_sale), reduced to what
the assertions turn on.
"""

from __future__ import annotations

from typing import Any

from gecko.lifecycle import build_lifecycle
from gecko.program_graph import build_program_graph

PROGRAM = "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm"
LAUNCH_SEEDS_DERIVABLE = [
    {"kind": "const", "value": list(b"launch")},
    {"kind": "account", "path": "admin"},
    {"kind": "arg", "path": "params.launch_id"},
]
LAUNCH_SEEDS_SELF = [
    {"kind": "const", "value": list(b"launch")},
    {"kind": "account", "path": "launch.admin", "account": "Launch"},
]
POSITION_SEEDS = [
    {"kind": "const", "value": list(b"user_position")},
    {"kind": "account", "path": "launch"},
    {"kind": "account", "path": "contributor"},
]

IDL: dict[str, Any] = {
    "address": PROGRAM,
    "metadata": {"spec": "0.1.0"},
    "instructions": [
        {
            "name": "initialize_launch",
            "args": [{"name": "params", "type": {"defined": "P"}}],
            "accounts": [
                {"name": "admin", "signer": True},
                {
                    "name": "launch",
                    "writable": True,
                    "pda": {"seeds": LAUNCH_SEEDS_DERIVABLE},
                },
            ],
        },
        {
            "name": "contribute",
            "args": [],
            "accounts": [
                {"name": "contributor", "signer": True},
                {
                    "name": "launch",
                    "writable": True,
                    "pda": {"seeds": LAUNCH_SEEDS_SELF},
                },
                {
                    "name": "user_position",
                    "writable": True,
                    "pda": {"seeds": POSITION_SEEDS},
                },
            ],
        },
        {
            "name": "claim",
            "args": [],
            "accounts": [
                {"name": "contributor", "signer": True},
                {
                    "name": "launch",
                    "writable": True,
                    "pda": {"seeds": LAUNCH_SEEDS_SELF},
                },
                # NO pda block — the caller supplies it. This is the real shape, and it
                # is why recipe-only reading misses the claim -> contribute edge.
                {"name": "user_position", "writable": True},
            ],
        },
        {
            "name": "remove_payment_mint_allowlist",
            "args": [],
            "accounts": [{"name": "admin", "signer": True}],
        },
    ],
    "accounts": [{"name": "Launch", "discriminator": [1, 2, 3, 4, 5, 6, 7, 8]}],
    "types": [
        {
            "name": "P",
            "type": {
                "kind": "struct",
                "fields": [{"name": "launch_id", "type": "u64"}],
            },
        },
        {
            "name": "Launch",
            "type": {"kind": "struct", "fields": [{"name": "admin", "type": "pubkey"}]},
        },
    ],
    "errors": [
        {"code": 6005, "name": "LaunchNotOpen", "msg": "not open"},
        {"code": 6007, "name": "LaunchNotSuccessful", "msg": "not successful"},
        {"code": 6009, "name": "ZeroValueNotAllowed", "msg": "zero"},
    ],
}


def lifecycle() -> Any:
    return build_lifecycle(build_program_graph(idl=IDL, program_id=PROGRAM), IDL)


def test_the_creating_instruction_is_the_one_with_a_DERIVABLE_recipe() -> None:
    """`initialize_launch` states `launch` from `admin` + `params.launch_id`; `contribute`
    and `claim` state it from `launch.admin`. That difference is the only evidence needed,
    and it needs no docs and no chain read."""
    launch = next(a for a in lifecycle().accounts if a.account == "launch")

    assert launch.produced_by == ("initialize_launch",)
    assert set(launch.consumed_by) == {"contribute", "claim"}


def test_an_account_passed_WITHOUT_a_recipe_still_counts_as_consumed() -> None:
    """The edge a recipe-only reading loses.

    `claim` takes `user_position` with no `pda` block at all, so comparing recipes never
    connects it to `contribute`, which creates it. Missing this gave every instruction the
    same single dependency on `initialize_launch` and hid the actual lifecycle.
    """
    position = next(a for a in lifecycle().accounts if a.account == "user_position")

    assert position.produced_by == ("contribute",)
    assert "claim" in position.consumed_by


def test_the_ordering_names_the_real_predecessor() -> None:
    must = lifecycle().must_follow

    assert "contribute" in must["claim"], (
        "claim needs a position, contribute creates it"
    )
    assert must["contribute"] == ("initialize_launch",)


def test_an_instruction_touching_nothing_shared_is_standalone() -> None:
    """Two of jurassic_fi's eight instructions depend on nothing and nothing depends on
    them — and they are the two nobody builds on. Worth reporting rather than hiding in
    an ordering that implies a sequence they are not part of."""
    assert lifecycle().standalone == ("remove_payment_mint_allowlist",)


def test_blast_radius_says_what_moves_when_an_account_moves() -> None:
    """`user_position` seeds on `launch`, so a different launch is a different position.
    That is the question "what else changes" — which a list of instructions cannot answer."""
    launch = next(a for a in lifecycle().accounts if a.account == "launch")

    assert "user_position" in launch.depended_on_by


def test_declared_states_are_the_program_s_OWN_words_and_nothing_else() -> None:
    """Reported as vocabulary, never as a current value. A state inferred from a name
    would be a guess wearing a fact's clothes."""
    states = lifecycle().declared_states

    assert "LaunchNotOpen" in states
    assert "LaunchNotSuccessful" in states
    assert "ZeroValueNotAllowed" not in states, (
        "not a state guard — it is a value check"
    )


def test_the_json_says_what_it_does_NOT_claim() -> None:
    """A dependency order read as a state machine is exactly the over-claim this project
    exists to avoid, so the payload carries the disclaimer rather than a doc page."""
    payload = lifecycle().to_json()

    assert "not a state machine" in payload["what_it_does_not_say"]
    assert "no chain state" in payload["how_this_was_derived"].lower()

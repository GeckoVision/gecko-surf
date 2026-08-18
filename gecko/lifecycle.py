"""The order a program's instructions must happen in — derived, not described.

WHAT THIS ANSWERS THAT AN INSTRUCTION LIST CANNOT. A catalogue lists instructions
independently: eight entries, alphabetical, each with its accounts. That tells you what
exists. It does not tell you that `contribute` cannot run before `initialize_launch`, that
`claim` cannot run before somebody settles, or that changing `launch` moves four other
accounts with it. Those are edges, and an entry in a list has no edges.

HOW THE ORDER IS RECOVERED, and it needs no prose and no state. An Anchor program that
stores its own seed values gives itself away: the instruction that CREATES an account must
state that account's seeds derivably, because at creation there is nothing to read them
from. Every later instruction states the same account from its own stored fields —
`launch.admin`, `launch.launch_id` — which is a correct runtime check and a dead end for a
caller.

So the split IS the lifecycle:

    a derivable recipe   -> this instruction PRODUCES the account
    a self-referential   -> this instruction CONSUMES an account that already exists

Measured on jurassic_fi: `initialize_launch` states `launch` from `admin` + `params
.launch_id`; the other seven state it from `launch.admin` + `launch.launch_id`. One
producer, seven consumers, recovered from seed shapes alone.

WHAT IT DOES NOT CLAIM. This is a *dependency* order, not a state machine. It says
`contribute` needs a `launch` that exists; it does not say the launch is open, and nothing
here reads a byte of chain state. Where a program declares state-guard errors those are
reported verbatim as the states the program itself names — evidence about the vocabulary,
never a claim about where any particular account currently sits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .pda_extract import blocked_only_on_itself, instruction_pdas
from .program_graph import ProgramGraph

__all__ = [
    "LIFECYCLE_TOOL",
    "AccountLifecycle",
    "ProgramLifecycle",
    "build_lifecycle",
]


@dataclass(frozen=True)
class AccountLifecycle:
    """One account, and which instructions bring it into being versus use it."""

    account: str
    #: instructions whose recipe for this account is derivable — they can create it
    produced_by: tuple[str, ...]
    #: instructions that state it from its own stored fields — it must already exist
    consumed_by: tuple[str, ...]
    #: instructions that take it writable
    written_by: tuple[str, ...]
    #: PDA accounts whose own seeds include this one — they move when it moves
    depended_on_by: tuple[str, ...] = ()

    @property
    def orphaned(self) -> bool:
        """Consumed but never produced — nothing in this program can create it.

        Not a defect on its own: an account may be created by a DIFFERENT program (an
        associated token account is the common case). It is worth reporting because a
        caller who assumes this program can bootstrap it will wait forever.
        """
        return bool(self.consumed_by) and not self.produced_by


@dataclass(frozen=True)
class ProgramLifecycle:
    """The whole program's ordering, as edges rather than a list."""

    program_id: str | None
    accounts: tuple[AccountLifecycle, ...]
    #: instruction -> instructions that must have happened first
    must_follow: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: the states the program's own error codes name, verbatim
    declared_states: tuple[str, ...] = ()
    #: instructions nothing else depends on and which depend on nothing — usually admin
    standalone: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "accounts": [
                {
                    "account": a.account,
                    "produced_by": list(a.produced_by),
                    "consumed_by": list(a.consumed_by),
                    "written_by": list(a.written_by),
                    "depended_on_by": list(a.depended_on_by),
                    "orphaned": a.orphaned,
                }
                for a in self.accounts
            ],
            "must_follow": {k: list(v) for k, v in self.must_follow.items()},
            "standalone": list(self.standalone),
            "declared_states": list(self.declared_states),
            "how_this_was_derived": (
                "from seed shapes only. An instruction that CREATES an account must state "
                "its seeds derivably, because at creation there is nothing to read them "
                "from; every later instruction states the same account from its own "
                "stored fields. No chain state was read and no documentation was parsed."
            ),
            "what_it_does_not_say": (
                "this is a dependency order, not a state machine. It says `contribute` "
                "needs a launch that exists; it does not say the launch is open. "
                "`declared_states` are the states the program's own errors NAME — the "
                "vocabulary, not the current value."
            ),
        }


def _state_words(idl: Mapping[str, Any]) -> tuple[str, ...]:
    """State names the program's own error codes use, verbatim.

    Reported because which call is legal often depends entirely on where an account sits:
    five of jurassic_fi's 32 errors are state guards — `LaunchNotOpen`,
    `LaunchNotSuccessful`, `LaunchNotRefundable`.

    THE RULE IS ANCHORED TO THE PROGRAM'S OWN TYPE NAMES, not to the word "Not". A first
    version matched any name containing "Not" and swept up `ZeroValueNotAllowed`, which is
    a value check and says nothing about state. An error qualifies only when the part
    before "Not" names an account or type this program actually declares — so `Launch`
    counts because `Launch` is a real account, and `ZeroValue` does not because nothing by
    that name exists. Anything else would be us inventing a taxonomy and presenting it as
    the program's.
    """
    subjects = {
        str(entry.get("name"))
        for group in ("accounts", "types")
        for entry in (idl.get(group) or [])
        if isinstance(entry, dict) and entry.get("name")
    }
    seen: list[str] = []
    for error in idl.get("errors", []) or []:
        name = str((error or {}).get("name", ""))
        head, marker, _ = name.partition("Not")
        if marker and head in subjects and name not in seen:
            seen.append(name)
    return tuple(seen)


def build_lifecycle(graph: ProgramGraph, idl: Mapping[str, Any]) -> ProgramLifecycle:
    """Recover the ordering from the graph and the IDL's own seed declarations."""
    type_defs = {
        str(t.get("name")): t for t in idl.get("types", []) if isinstance(t, dict)
    }
    produced: dict[str, list[str]] = {}
    consumed: dict[str, list[str]] = {}
    written: dict[str, list[str]] = {}

    for raw in idl.get("instructions", []) or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", ""))
        declared = instruction_pdas(
            raw, program_id=graph.program_id, type_defs=type_defs
        )
        for account, node in declared.items():
            if blocked_only_on_itself(node):
                consumed.setdefault(account, []).append(name)
            elif node.resolvable:
                produced.setdefault(account, []).append(name)
            else:
                # a recipe blocked on ANOTHER account's data: it needs that account to
                # exist, so it is a consumer even though it is not self-referential
                consumed.setdefault(account, []).append(name)
        for account in raw.get("accounts", []) or []:
            if not isinstance(account, dict):
                continue
            account_name = str(account.get("name"))
            if account.get("writable"):
                written.setdefault(account_name, []).append(name)
            # AN ACCOUNT PASSED WITHOUT A RECIPE IS STILL A CONSUMER, and missing this
            # loses the most useful edges in the program. `claim` takes `user_position`
            # with no `pda` block at all — the caller supplies the address — so recipe
            # shapes alone never connect it to `contribute`, which creates it. Reading
            # only declared recipes gave every instruction the same single dependency on
            # `initialize_launch` and hid the actual lifecycle.
            if (
                account_name in graph.pdas
                and account_name not in declared
                and name not in consumed.get(account_name, [])
            ):
                consumed.setdefault(account_name, []).append(name)

    # which PDA accounts seed on which others — the "move together" edges
    depends: dict[str, list[str]] = {}
    for account, node in graph.pdas.items():
        for seed in node.seeds:
            seed_name = getattr(seed, "name", None)
            if seed_name and seed_name in graph.pdas and seed_name != account:
                depends.setdefault(seed_name, []).append(account)

    accounts = tuple(
        AccountLifecycle(
            account=name,
            produced_by=tuple(dict.fromkeys(produced.get(name, ()))),
            consumed_by=tuple(dict.fromkeys(consumed.get(name, ()))),
            written_by=tuple(dict.fromkeys(written.get(name, ()))),
            depended_on_by=tuple(dict.fromkeys(depends.get(name, ()))),
        )
        for name in sorted(set(produced) | set(consumed))
    )

    # an instruction that consumes an account must follow whatever produces it
    must_follow: dict[str, list[str]] = {}
    for entry in accounts:
        for consumer in entry.consumed_by:
            for producer in entry.produced_by:
                if producer != consumer:
                    must_follow.setdefault(consumer, [])
                    if producer not in must_follow[consumer]:
                        must_follow[consumer].append(producer)

    all_names = [
        str(i.get("name", ""))
        for i in (idl.get("instructions") or [])
        if isinstance(i, dict)
    ]
    depended_upon = {p for deps in must_follow.values() for p in deps}
    standalone = tuple(
        n for n in all_names if n not in must_follow and n not in depended_upon
    )

    return ProgramLifecycle(
        program_id=graph.program_id,
        accounts=accounts,
        must_follow={k: tuple(v) for k, v in must_follow.items()},
        declared_states=_state_words(idl),
        standalone=standalone,
    )


LIFECYCLE_TOOL = {
    "name": "program_lifecycle",
    "description": (
        "The ORDER a program's instructions must happen in, and which accounts move "
        "together — derived from the graph, not from documentation.\n"
        "\n"
        "Ask this BEFORE prepare_instruction when you do not already know the flow. An "
        "instruction list tells you what exists; this tells you what has to come first, "
        "which is the part that decides whether your call can work at all. On a live "
        "token sale it recovers `claim must follow contribute`, `contribute must follow "
        "initialize_launch`, and that changing `launch` moves `user_position`, "
        "`payment_vault` and `token_vault` with it.\n"
        "\n"
        "HOW: an instruction that CREATES an account must state its seeds derivably, "
        "because at creation there is nothing to read them from; later instructions state "
        "the same account from its own stored fields. The split is the lifecycle. No "
        "chain state is read.\n"
        "\n"
        "IT IS A DEPENDENCY ORDER, NOT A STATE MACHINE. It says `claim` needs a position "
        "that exists; it does not say the sale has settled. `declared_states` lists the "
        "states the program's own errors NAME — the vocabulary, never the current value."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "program_id": {
                "type": "string",
                "description": "the Solana program address (base58)",
            }
        },
        "required": ["program_id"],
        "additionalProperties": False,
    },
}

"""What a partner's live surface says about a PDA, and what ours says — side by side.

THE MEASUREMENT THIS ANSWERS. An agent deriving a program address needs two things from a
surface: the seed ORDER and each variable seed's TYPE. The order is in every IDL. The type
is not: Anchor records a seed's *path* and never its width, and it encodes a numeric seed
little-endian at its DECLARED width — so the same value at the wrong width derives a
different address that is still perfectly valid, still passes every client-side check, and
is caught by nothing downstream.

So the metric is not "did the call succeed". It is **how many variable seeds carry a
declared type**, and from that, **how many accounts a caller could derive without guessing
a width**. An account with one untyped numeric seed is not 90% derivable; it is a coin
flip across the plausible widths.

BOTH ARMS READ THE SAME IDL. The partner arm asks the partner's own live MCP; the Gecko
arm builds our graph from the IDL that partner serves. Neither arm is allowed a private
input, because a score a party produces about itself is not a score.

Read-only: the partner tools called here list and describe. Nothing signs or broadcasts,
and no response is persisted (control-plane invariant #1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .mcp_client import McpClient, McpError
from .program_graph import build_program_graph

__all__ = [
    "SeedCoverage",
    "ProgramDelta",
    "parse_partner_seeds",
    "partner_coverage",
    "gecko_coverage",
    "compare_program",
]

#: A seed line in the partner's `list_pda_accounts` output, e.g.
#:   ``  - `params.launch_id` (arg, type: u64)``
#:   ``  - `launch.admin` (account_field)``
#:   ``  - const — launch``
_SEED_LINE = re.compile(
    r"^\s*-\s+`(?P<name>[^`]+)`\s+\((?P<kind>[a-z_]+)(?:,\s*type:\s*(?P<type>[^)]+))?\)"
)
_ACCOUNT_LINE = re.compile(
    r"^##\s+`(?P<account>[^`]+)`\s+\(instruction:\s+`(?P<ix>[^`]+)`\)"
)


@dataclass
class SeedCoverage:
    """How much of a surface's seed metadata a caller can actually act on."""

    variable_seeds: int = 0
    typed_seeds: int = 0
    accounts: int = 0
    #: accounts whose EVERY variable seed carries a type — the ones a caller can derive
    #: without guessing
    fully_typed_accounts: int = 0

    @property
    def typed_rate(self) -> float:
        return self.typed_seeds / self.variable_seeds if self.variable_seeds else 0.0

    @property
    def derivable_rate(self) -> float:
        return self.fully_typed_accounts / self.accounts if self.accounts else 0.0


@dataclass
class ProgramDelta:
    """One program, measured on both arms."""

    name: str
    project_id: str
    program_id: str | None
    partner: SeedCoverage
    gecko: SeedCoverage
    error: str | None = None
    #: PDA slots WE resolve that the partner's listing does not mention at all — counted,
    #: never folded into a rate, because the other arm was not asked about them
    gecko_only_accounts: int = 0


def parse_partner_seeds(text: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Their markdown listing → ``{(instruction, account): [seed, ...]}``.

    Parsed rather than reconstructed: the point is to read what the surface actually
    tells an agent, which is this text. Reading their source instead would measure what
    we think their code does.
    """
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    current: tuple[str, str] | None = None
    for line in text.splitlines():
        header = _ACCOUNT_LINE.match(line)
        if header:
            current = (header.group("ix"), header.group("account"))
            out.setdefault(current, [])
            continue
        if current is None:
            continue
        seed = _SEED_LINE.match(line)
        if seed:
            out[current].append(
                {
                    "name": seed.group("name"),
                    "kind": seed.group("kind"),
                    "type": (seed.group("type") or "").strip() or None,
                }
            )
    return out


def partner_coverage(text: str) -> SeedCoverage:
    """Coverage as the partner's own output reports it."""
    coverage = SeedCoverage()
    for seeds in parse_partner_seeds(text).values():
        coverage.accounts += 1
        variables = [s for s in seeds if s["kind"] != "const"]
        typed = [s for s in variables if s["type"]]
        coverage.variable_seeds += len(variables)
        coverage.typed_seeds += len(typed)
        if variables and len(typed) == len(variables):
            coverage.fully_typed_accounts += 1
    return coverage


def gecko_coverage(
    idl: dict[str, Any],
    program_id: str | None = None,
    only_pairs: set[tuple[str, str]] | None = None,
) -> tuple[SeedCoverage, int]:
    """Coverage from our graph over the SAME IDL, plus the pairs the partner did not list.

    ``only_pairs`` restricts counting to the ``(instruction, account)`` pairs the partner
    reported, so the two arms share a denominator. THIS MATTERS MORE THAN IT LOOKS: we
    resolve PDA slots the partner does not list at all, because we carry a recipe from the
    instruction that declares it to the instructions that do not. Counting those in the
    same percentage would be scoring ourselves on a question the other arm was never
    asked. They are returned separately instead, as a count.

    A seed is TYPED when its binding carries an encoding. A
    :class:`~gecko.pda.ResolverPdaSeedNode` binds with an empty encoding by design — the
    honest opposite of a type, and the reason this metric cannot be gamed by resolving
    more seeds badly.
    """
    graph = build_program_graph(idl=idl, program_id=program_id)
    coverage = SeedCoverage()
    extra = 0
    for instruction in graph.instructions:
        for account in instruction.accounts:
            if not account.is_pda:
                continue
            pair = (instruction.name, account.name)
            if only_pairs is not None and pair not in only_pairs:
                extra += 1
                continue
            typed = [b for b in account.derive_from if b.encoding]
            coverage.accounts += 1
            coverage.variable_seeds += len(account.derive_from)
            coverage.typed_seeds += len(typed)
            if account.derive_from and len(typed) == len(account.derive_from):
                coverage.fully_typed_accounts += 1
    return coverage, extra


def compare_program(
    client: McpClient,
    *,
    name: str,
    project_id: str,
    idl: dict[str, Any],
    program_id: str | None = None,
) -> ProgramDelta:
    """Ask the partner, build ours, and report both. Never raises for one program."""
    partner = SeedCoverage()
    pairs: set[tuple[str, str]] | None = None
    error: str | None = None
    try:
        text = client.call_tool("list_pda_accounts", {"projectId": project_id})
        parsed = parse_partner_seeds(text)
        pairs = set(parsed)
        partner = partner_coverage(text)
    except (McpError, OSError) as exc:  # one program's outage is not the measurement's
        error = f"{type(exc).__name__}: {exc}"

    extra = 0
    try:
        gecko, extra = gecko_coverage(idl, program_id, only_pairs=pairs)
    except Exception as exc:  # noqa: BLE001 - a graph we cannot build is a data point
        gecko = SeedCoverage()
        error = error or f"graph: {type(exc).__name__}: {exc}"

    return ProgramDelta(
        name=name,
        project_id=project_id,
        program_id=program_id,
        partner=partner,
        gecko=gecko,
        error=error,
        gecko_only_accounts=extra,
    )

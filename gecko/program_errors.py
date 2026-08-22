"""Name a reverted call's error against the program's OWN table — or refuse to name it.

WHAT THIS FIXES. A refused simulation used to say the same sentence for every failure:
"the program rejected this call". The code was carried (`{"InstructionError": [0,
{"Custom": 6008}]}`) and the IDL already declares the table (6008 = VectorLimitReached),
so the information was in hand and unused. This module joins them.

WHY IT IS NOT A DICTIONARY LOOKUP, and this is the whole design. The index in an
`InstructionError` is the instruction's position in the transaction. It says NOTHING about
which program produced the code. If a program CPIs into SPL Token and the token program
errors, the `Custom` code belongs to SPL Token's table — and looking it up in the outer
program's table yields a confident, well-formed, WRONG name, spoken in the program's
voice. We have met this exact confusion before: a transaction's top-level instruction list
is not the list of programs invoked.

So naming is GATED ON RECONCILIATION. The runtime logs the origin directly:

    Program <id> failed: custom program error: 0x1778

A name is emitted only when that program is the one whose IDL we hold AND the hex
reconciles to the code in the `InstructionError`. Anything else — a different program, no
logs, a code absent from the table — is reported UNNAMED, saying which program actually
failed. An unnamed error is honest; a wrongly-named one is worse than the generic string
it replaced.

WHOSE VOICE. A name from `idl["errors"]` is the program's own declaration, so it may be
attributed to the program (`source="program-idl"`). Anything we add on top — what to do
about it — is Gecko's knowledge and belongs in an overlay that says so. This module
emits only the first kind. `gecko/lifecycle.py` sets the same precedent: it refuses to
sweep `ZeroValueNotAllowed` into a state taxonomy because that would be "us inventing a
taxonomy and presenting it as the program's".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

__all__ = [
    "ProgramError",
    "custom_error_code",
    "failing_program",
    "name_program_error",
]

#: The runtime's own statement of which program failed and with what. The FIRST such line
#: is the origin: when an inner program fails, an Anchor caller propagates the same code
#: and logs its own failure afterwards, so a later line names the messenger.
_FAILED_LINE = re.compile(
    r"^Program (?P<program>[1-9A-HJ-NP-Za-km-z]{32,44}) failed: "
    r"custom program error: 0x(?P<hex>[0-9a-fA-F]+)"
)

Source = Literal["program-idl", "unnamed"]


@dataclass(frozen=True)
class ProgramError:
    """A reverted call's error, named only when the evidence supports naming it."""

    code: int
    #: The program the LOGS say produced it — not the program we happened to be calling.
    program: str | None
    name: str | None = None
    message: str | None = None
    source: Source = "unnamed"
    #: Why a name was withheld. Present exactly when `name` is None.
    unnamed_because: str | None = None

    @property
    def named(self) -> bool:
        return self.name is not None

    def describe(self) -> str:
        """One line, and it never claims more than was established."""
        if self.name is not None:
            return f"{self.name} ({self.code})"
        return f"custom error {self.code} — {self.unnamed_because}"


def custom_error_code(error: Any) -> int | None:
    """The `Custom` code out of an `InstructionError`, or None if it is another shape.

    Deliberately narrow: a `BorshIoError`, an `InsufficientFunds`, or a bare string is not
    a custom code and must not be coerced into one.
    """
    if not isinstance(error, Mapping):
        return None
    detail = error.get("InstructionError")
    if not isinstance(detail, Sequence) or isinstance(detail, str) or len(detail) != 2:
        return None
    kind = detail[1]
    if not isinstance(kind, Mapping):
        return None
    code = kind.get("Custom")
    return int(code) if isinstance(code, int) else None


def failing_program(logs: Sequence[str] | None) -> tuple[str, int] | None:
    """`(program_id, code)` from the FIRST runtime failure line, or None.

    The first line is the origin. A propagating caller logs its own failure with the same
    code afterwards, and trusting the last line would credit the messenger.
    """
    for line in logs or ():
        match = _FAILED_LINE.match(str(line).strip())
        if match:
            return match.group("program"), int(match.group("hex"), 16)
    return None


def _table(idl: Mapping[str, Any] | None) -> dict[int, tuple[str, str]]:
    entries: dict[int, tuple[str, str]] = {}
    for entry in (idl or {}).get("errors", []) or []:
        if not isinstance(entry, Mapping):
            continue
        code, name = entry.get("code"), entry.get("name")
        if isinstance(code, int) and isinstance(name, str):
            entries[code] = (name, str(entry.get("msg") or name))
    return entries


def name_program_error(
    error: Any,
    *,
    logs: Sequence[str] | None,
    idl: Mapping[str, Any] | None,
    program_id: str | None,
) -> ProgramError | None:
    """Name the error against `idl`'s table, but ONLY if the logs place it there.

    None when the error carries no custom code at all — there is nothing to name, and
    inventing a shape for it would be worse than staying quiet.
    """
    code = custom_error_code(error)
    if code is None:
        return None

    origin = failing_program(logs)
    if origin is None:
        return ProgramError(
            code=code,
            program=None,
            unnamed_because=(
                "no log names the program that produced it, so it cannot be attributed"
            ),
        )
    origin_program, origin_code = origin
    if origin_code != code:
        return ProgramError(
            code=code,
            program=origin_program,
            unnamed_because=(
                f"the logs report 0x{origin_code:x} ({origin_code}) from {origin_program}, "
                f"which does not reconcile with {code}"
            ),
        )
    if program_id is not None and origin_program != program_id:
        return ProgramError(
            code=code,
            program=origin_program,
            unnamed_because=(
                f"it came from {origin_program}, which is not the program this IDL "
                f"describes ({program_id}) — naming it here would be a guess"
            ),
        )

    entry = _table(idl).get(code)
    if entry is None:
        return ProgramError(
            code=code,
            program=origin_program,
            unnamed_because=f"{origin_program} declares no error with this code",
        )
    name, message = entry
    return ProgramError(
        code=code,
        program=origin_program,
        name=name,
        message=message,
        source="program-idl",
    )

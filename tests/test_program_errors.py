"""Naming a reverted call's error — and, more importantly, refusing to.

The CPI test is the one that matters. It fails silently if anyone ever "simplifies" the
mapping back to a bare `Custom(N)` lookup, because the wrong answer it produces is
well-formed, confident, and spoken in the program's voice.
"""

from __future__ import annotations

from gecko.program_errors import (
    custom_error_code,
    failing_program,
    name_program_error,
)

LMB = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

#: let_me_buy's real table, as its IDL declares it.
IDL = {
    "errors": [
        {"code": 6001, "name": "ProductAlreadyExists", "msg": "ProductAlreadyExists"},
        {"code": 6004, "name": "InvalidAuthority", "msg": "InvalidAuthority"},
        {"code": 6008, "name": "VectorLimitReached", "msg": "VectorLimitReached"},
    ]
}
#: Captured verbatim from a 21st add_product against the fork.
REVERT_6008 = {"InstructionError": [0, {"Custom": 6008}]}
LOGS_6008 = (
    "Program log: AnchorError thrown in programs/let-me-buy/src/lib.rs:149. "
    "Error Code: VectorLimitReached. Error Number: 6008.",
    f"Program {LMB} consumed 27541 of 200000 compute units",
    f"Program {LMB} failed: custom program error: 0x1778",
)


def test_the_cap_error_is_named_from_the_programs_own_table() -> None:
    named = name_program_error(REVERT_6008, logs=LOGS_6008, idl=IDL, program_id=LMB)
    assert named is not None
    assert named.named and named.name == "VectorLimitReached"
    assert named.code == 6008 and named.program == LMB
    assert named.source == "program-idl"
    assert named.describe() == "VectorLimitReached (6008)"


def test_a_cpi_error_is_never_named_against_the_outer_programs_table() -> None:
    """THE test. 6008 is real in let_me_buy's table — and this one is NOT let_me_buy's.

    An inner program fails and the Anchor caller propagates the identical code, so the
    InstructionError is indistinguishable from a native failure. Only the logs say where
    it came from, and a bare table lookup would confidently answer 'VectorLimitReached'
    about an error the token program raised.
    """
    logs = (
        f"Program {LMB} invoke [1]",
        f"Program {TOKEN} invoke [2]",
        f"Program {TOKEN} failed: custom program error: 0x1778",
        f"Program {LMB} failed: custom program error: 0x1778",
    )
    result = name_program_error(REVERT_6008, logs=logs, idl=IDL, program_id=LMB)
    assert result is not None
    assert not result.named
    assert result.program == TOKEN
    assert "not the program this IDL describes" in (result.unnamed_because or "")


def test_the_first_failure_line_is_the_origin_not_the_messenger() -> None:
    """A propagating caller logs its own failure afterwards; trusting it credits the wrong
    program with the error."""
    logs = (
        f"Program {TOKEN} failed: custom program error: 0x1",
        f"Program {LMB} failed: custom program error: 0x1",
    )
    assert failing_program(logs) == (TOKEN, 1)


def test_no_logs_means_no_name() -> None:
    result = name_program_error(REVERT_6008, logs=(), idl=IDL, program_id=LMB)
    assert result is not None and not result.named
    assert result.program is None
    assert "no log names the program" in (result.unnamed_because or "")


def test_a_code_the_table_does_not_declare_stays_unnamed() -> None:
    error = {"InstructionError": [0, {"Custom": 6099}]}
    logs = (f"Program {LMB} failed: custom program error: 0x17d3",)
    result = name_program_error(error, logs=logs, idl=IDL, program_id=LMB)
    assert result is not None and not result.named
    assert "declares no error with this code" in (result.unnamed_because or "")


def test_a_hex_that_does_not_reconcile_stays_unnamed() -> None:
    """The log and the InstructionError disagreeing means one of them is not about this
    failure — so neither may be trusted to name it."""
    logs = (f"Program {LMB} failed: custom program error: 0x1771",)  # 6001, not 6008
    result = name_program_error(REVERT_6008, logs=logs, idl=IDL, program_id=LMB)
    assert result is not None and not result.named
    assert "does not reconcile" in (result.unnamed_because or "")


def test_a_non_custom_error_is_not_coerced_into_one() -> None:
    """Only `Custom` carries a program-defined code; everything else must stay untouched."""
    assert custom_error_code({"InstructionError": [0, "InvalidAccountData"]}) is None
    assert custom_error_code({"InsufficientFundsForRent": {"account_index": 1}}) is None
    assert custom_error_code("BlockhashNotFound") is None
    assert custom_error_code(None) is None
    assert (
        name_program_error(
            {"InstructionError": [0, "InvalidAccountData"]},
            logs=LOGS_6008,
            idl=IDL,
            program_id=LMB,
        )
        is None
    )


def test_the_duplicate_name_error_is_named_too() -> None:
    """6001 against a real add_product — the other error this path actually meets."""
    result = name_program_error(
        {"InstructionError": [0, {"Custom": 6001}]},
        logs=(f"Program {LMB} failed: custom program error: 0x1771",),
        idl=IDL,
        program_id=LMB,
    )
    assert result is not None and result.name == "ProductAlreadyExists"

"""Where a missing seed value can be READ from — the hop between two of our own tools.

`prepare_instruction` refuses `launch` with ``caller_must_supply=('admin',
'params.launch_id')`` and stops. :mod:`gecko.read_accounts` returns exactly ``{admin,
launch_id}`` for that program's ``Launch`` type, each instance proven by re-derivation.
Two tools, one holding what the other needs, and nothing joining them — the most repeated
finding of the week, reported by two independent agents and then found again a layer
deeper.

Measured across 196 cached catalogue IDLs: **181 PDA accounts need seed values, and
read_accounts could supply every value for 121 of them (66.9%).**

**This names a source. It never fetches the value and never chooses an instance.** A
resolved address decides who gets paid, and "there was only one, so it must be the one" is
the drain. The caller makes one more call, with the tool and its arguments spelled out, and
the answer arrives through a path that re-derives what it read.

Two guards keep the hint honest, and both were false positives before they were guards:

- **Only witnessed types are offered.** ``read_accounts`` refuses 75.9% of account types;
  pointing a caller at a tool that will refuse them is worse than saying nothing.
- **Never an instruction that CREATES the account.** You cannot read a ``Launch`` to build
  the call that creates it. Measured as a real false positive of name matching:
  ``initialize.authority <- Distributor``.

Name matching is an INFERENCE and is safe only because whatever the caller reads is
re-derived before it can be used. It widens what a caller can find; it never widens what
Gecko will assert.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .account_recipes import verification_recipe

__all__ = ["CREATING_PREFIXES", "value_sources"]

#: An instruction whose name starts with one of these is taken to CREATE the account it
#: seeds, so the account cannot be read first. Crude on purpose: it is a refusal filter, and
#: over-refusing loses a hint while under-refusing sends a caller to read something that does
#: not exist yet.
CREATING_PREFIXES = ("init", "create", "new", "open", "setup", "register", "deploy")

_NOTE = (
    "read it from this account type and pass the field through. Several instances may "
    "come back — choose which one you mean; nothing here chooses for you, and every "
    "instance is re-derived from its own values before it is offered."
)


def _witnessed_fields(idl: Mapping[str, Any], program_id: str) -> dict[str, set[str]]:
    """Account types this program can WITNESS, mapped to the fields they store."""
    out: dict[str, set[str]] = {}
    types = idl.get("types") or ()
    for account in idl.get("accounts") or ():
        name = account.get("name")
        if not isinstance(name, str):
            continue
        try:
            if not verification_recipe(idl, name, program_id):
                continue
        except Exception:  # noqa: BLE001 - every refusal means "not offerable"
            continue
        fields = {
            str(f.get("name"))
            for entry in types
            if entry.get("name") == name
            for f in ((entry.get("type") or {}).get("fields") or ())
        }
        if fields:
            out[name] = fields
    return out


def value_sources(
    idl: Mapping[str, Any],
    program_id: str,
    *,
    instruction: str,
    needed: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """For each value in ``needed``, the account it can be read from — or nothing.

    Keyed by the value's name AS THE CALLER WAS TOLD IT (``params.launch_id``, not
    ``launch_id``), so a caller can match the hint to the refusal that produced it without
    re-deriving the spelling.
    """
    if instruction.lower().startswith(CREATING_PREFIXES):
        return {}

    # Accounts of THIS instruction are DERIVED, never read. `user_position` is seeded on
    # `launch`, so `launch` shows up as a needed value — but the plan derives it as soon as
    # its own seeds are known, and offering "read UserPosition.launch to get launch" sends
    # the caller backwards through the very chain being resolved. Caught by walking the
    # hops as an agent would; the name match alone cannot tell a seed value from an account.
    # Only accounts this instruction DERIVES — i.e. that declare a `pda` block. A slot
    # without one is the caller's to supply (`payment_mint`), and it is exactly the case a
    # hint should cover. Guarding on every account of the instruction over-refused and
    # silently dropped the payment_mint hint; walking the hops caught it, the tests did not.
    derived_here = {
        str(a.get("name"))
        for ix in idl.get("instructions") or ()
        if ix.get("name") == instruction
        for a in ix.get("accounts") or ()
        if a.get("pda")
    }

    witnessed = _witnessed_fields(idl, program_id)
    if not witnessed:
        return {}

    hints: dict[str, dict[str, Any]] = {}
    for value in needed:
        if str(value) in derived_here:
            continue
        field = str(value).split(".")[-1]
        # sorted so the same IDL always yields the same hint — a source that moves between
        # runs is a source nobody can pin a bug to.
        for account_type in sorted(witnessed):
            if field not in witnessed[account_type]:
                continue
            # `fields` is part of the ARGUMENTS, not just the prose. read_accounts returns
            # only the WITNESS fields by default, so a caller following this hint literally
            # would get a payload without the field the hint names — which is the whole
            # point of the hint. Caught by walking the hop as an agent would, not by a
            # test: naming a tool is not the same as handing over a call that works.
            hints[str(value)] = {
                "tool": "read_accounts",
                "arguments": {
                    "program_id": program_id,
                    "account_type": account_type,
                    "fields": [field],
                },
                "account_type": account_type,
                "field": field,
                "note": _NOTE,
            }
            break
    return hints

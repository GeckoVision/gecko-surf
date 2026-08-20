"""`needs` must never tell a caller they already hold a value they do not have.

Found 2026-08-19 by an anchor-engineer review, confirmed against 80 instructions in a
200-program sample of the live catalogue. `main::add_collateral_admin` declares
``args: ['new_admin']`` — there is no ``new_collateral`` argument anywhere in it — and we
told its caller:

    a field of the `new_collateral` argument struct — you build it, so pass
    `new_collateral.id` directly

The recipe came from a SIBLING instruction (``create_collateral``) via the program-wide
merge, and the branch fired on the sibling's seed definition without ever checking whether
THIS instruction declares the argument. A caller who follows that sentence invents an
``id``, and an invented id derives a perfectly valid wrong address — correctly formatted,
resolvable, and not the account they meant.

That is the exact failure this repo exists to prevent, produced by our own advice, and it
shipped inside the plugin trees `gecko export-plugin` hands to providers.
"""

from __future__ import annotations

from gecko.artifact import callability, instruction_needs

#: The recipe as the program-wide merge produces it: `id` is declared as a field of an
#: ARGUMENT struct, because the sibling that declares this account really does take one.
_RECIPE = {
    "collateral": {
        "resolvable": True,
        "seeds": [
            {"name": "new_collateral.id", "source": "argument", "encoding": "le"},
        ],
    }
}


def _instruction(args: list[dict[str, str]]) -> dict[str, object]:
    return {
        "name": "add_collateral_admin",
        "args": args,
        "accounts": [
            {
                "name": "collateral",
                "is_pda": True,
                "derive_from": [
                    {
                        "kind": "unresolved",
                        "seed": "new_collateral.id",
                        "encoding": "le",
                    }
                ],
            }
        ],
    }


def test_a_caller_is_not_told_to_pass_an_argument_the_instruction_does_not_declare() -> (
    None
):
    """The live defect. This instruction takes `new_admin` and nothing else."""
    needs = instruction_needs(
        _instruction([{"name": "new_admin", "type": "pubkey"}]), _RECIPE
    )

    (need,) = needs
    assert need["value"] == "new_collateral.id"
    assert "you build it" not in need["why"], need["why"]
    assert "pass `new_collateral.id` directly" not in need["why"], need["why"]


def test_the_advice_survives_when_the_instruction_really_does_take_the_struct() -> None:
    """The other half, so the fix cannot be "delete the helpful branch". When the caller
    genuinely constructs the struct, telling them to read it on-chain would send them
    after a value that has no account."""
    needs = instruction_needs(
        _instruction([{"name": "new_collateral", "type": "NewCollateral"}]), _RECIPE
    )

    (need,) = needs
    assert "you build it" in need["why"]
    assert "`new_collateral.id`" in need["why"]


def test_an_instruction_with_no_args_at_all_is_never_told_it_builds_one() -> None:
    """`main::close_old_collateral_signatures` has `args: []`."""
    needs = instruction_needs(_instruction([]), _RECIPE)

    (need,) = needs
    assert "you build it" not in need["why"], need["why"]


# --- the callability split -------------------------------------------------------
#
# `needs` non-empty used to mean four different things at once, and a 200-program sample
# of the live catalogue reported "17.7% blocked" as a result. Decomposed, that was ~8%
# with no derivable recipe at all and ~10% where the recipe is known and a named value has
# to be fetched — plus a slice that was not blocked in any sense, because the caller
# constructs the value themselves.
#
# Those are three different products: the first needs source recovery or enumeration, the
# second needs one chain read, the third needs nothing. A single number that averages them
# is not a measurement, and this one was about to be quoted to a partner.


def _instr(name: str, *, needs: bool, unresolvable: bool) -> dict[str, object]:
    return {
        "name": name,
        "needs": [{"value": "x", "why": "y"}] if needs else [],
        "unresolvable": ["some_account"] if unresolvable else [],
    }


def test_the_three_states_are_counted_apart() -> None:
    split = callability(
        [
            _instr("a", needs=False, unresolvable=False),
            _instr("b", needs=True, unresolvable=False),
            _instr("c", needs=False, unresolvable=True),
        ]
    )

    assert split["instructions"] == 3
    assert split["buildable_now"] == 1
    assert split["needs_a_lookup"] == 1
    assert split["uncallable"] == 1


def test_the_states_are_mutually_exclusive_and_total() -> None:
    """An instruction that is both unresolvable and needs a lookup must be counted ONCE,
    in the worse bucket. Double-counting is how a rate climbs above what it measures."""
    split = callability(
        [
            _instr("a", needs=True, unresolvable=True),
            _instr("b", needs=True, unresolvable=False),
        ]
    )

    assert split["uncallable"] == 1
    assert split["needs_a_lookup"] == 1
    assert split["buildable_now"] == 0
    assert (
        split["buildable_now"] + split["needs_a_lookup"] + split["uncallable"]
        == split["instructions"]
    )


def test_uncallable_is_the_worse_bucket() -> None:
    """No recipe exists — a chain read cannot help, because there is nothing to read INTO."""
    split = callability([_instr("a", needs=True, unresolvable=True)])

    assert split["uncallable"] == 1
    assert split["needs_a_lookup"] == 0


def test_an_empty_program_reports_zeroes_not_a_division() -> None:
    assert callability([])["instructions"] == 0

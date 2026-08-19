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

from gecko.artifact import instruction_needs

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

"""Derivation as a primitive, and the bug that made it necessary.

An agent followed a correct refusal — `contributor_payment_account` is not a PDA in the
IDL, so the caller supplies it — and hand-rolled the derivation itself. It skipped the
ed25519 on-curve check and produced a well-formed WRONG address at bump 255. The real one
is 254. It caught the error only because it re-derived a sibling account and compared.

That is this project's own documented failure mode, reproduced by an agent doing exactly
what it was told. Refusing to guess is right; refusing in a way that pushes the guess
outside where it can be checked is not.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.prepare_instruction import derive_ata_result, derive_pda_result

OWNER = "GpaLFMwQWh2xuBkMQGKmcYT5A1WgYJekofu6DJjp8W9c"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
#: Live mainnet Token-2022 mint (Global Dollar) — the case the old default got wrong.
USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
CLASSIC_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
LAUNCH_PROGRAM = "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm"
ADMIN = "6Dw1xBGXChPeS69hovvYMF2nmRxgdoA711TKuuAbN5rV"


def test_the_ata_bump_is_254_not_255() -> None:
    """The exact address and bump the hand-rolled loop got wrong.

    Both are asserted: an address alone would pass for a loop that happened to land on
    the right value by luck, and the bump is what the skipped check actually decides.
    """
    result = derive_ata_result(
        {"owner": OWNER, "mint": USDC, "token_program": CLASSIC_TOKEN}
    )

    assert result["address"] == "DwwMEu6CYdevytmKM68xEfmBVtzrGQetUhNZyvbUKKvQ"
    assert result["bump"] == 254, (
        "255 is what a loop without the on-curve check returns"
    )


def test_token_2022_derives_a_different_account() -> None:
    """Not a detail: the two programs derive different addresses for the same owner and
    mint, and only one of them is the account a program will accept."""
    legacy = derive_ata_result(
        {"owner": OWNER, "mint": USDC, "token_program": CLASSIC_TOKEN}
    )
    t22 = derive_ata_result({"owner": OWNER, "mint": USDC, "token_program": TOKEN_2022})

    assert legacy["address"] != t22["address"]


def test_an_omitted_token_program_refuses_instead_of_assuming_classic() -> None:
    """The tool used to default to the legacy SPL Token program, which reproduced its
    OWN documented failure mode one field over: a well-formed WRONG address, returned
    with ``refused: false``, for every Token-2022 mint whose caller left the field out.

    USDG (2u1tsz..., Token-2022, live on mainnet) is the case that matters — the store
    programs that pin classic SPL cannot settle it at all, and the wrong ATA is not
    rejected by the runtime: it is a valid, off-curve address for an account that was
    never initialized. The token program is a property OF THE MINT, read from the mint
    account's `owner`. It cannot be inferred from the mint address, its decimals, or its
    label, so the honest answer when it is absent is a refusal."""
    result = derive_ata_result({"owner": OWNER, "mint": USDG})

    assert result["refused"] is True
    assert result["code"] == "argument-missing"
    assert "address" not in result, "a refusal must not carry an address"
    assert "owner" in result["reason"], (
        "the refusal has to say HOW to get the value — read the mint account's owner"
    )


def test_supplying_the_token_program_still_derives() -> None:
    """The refusal is about the ABSENT field, not a new restriction: both programs
    derive exactly as before when the caller names which one the mint belongs to."""
    for program in (CLASSIC_TOKEN, TOKEN_2022):
        result = derive_ata_result(
            {"owner": OWNER, "mint": USDG, "token_program": program}
        )
        assert result["refused"] is False
        assert result["token_program"] == program


def test_a_pda_derives_the_live_account_from_its_seeds() -> None:
    """Ground truth: this is a real account on mainnet, and these are its real seeds."""
    result = derive_pda_result(
        {
            "program_id": LAUNCH_PROGRAM,
            "seeds": [{"utf8": "launch"}, {"pubkey": ADMIN}, {"u64": 100}],
        }
    )

    assert result["address"] == "9nFKKFBEVW4njBtyJkcvngEkmT3qVXm4fgGGRBLbqH65"


@pytest.mark.parametrize(
    "width,expected_differs",
    [("u8", True), ("u16", True), ("u32", True), ("u64", False)],
)
def test_the_integer_WIDTH_changes_the_address(
    width: str, expected_differs: bool
) -> None:
    """The whole reason a recipe must carry its width. The same value at u8, u16 and u32
    each derives a DIFFERENT, perfectly valid address, and only u64 is the live account —
    so a caller who guesses the width gets a success that is wrong."""
    live = "9nFKKFBEVW4njBtyJkcvngEkmT3qVXm4fgGGRBLbqH65"
    result = derive_pda_result(
        {
            "program_id": LAUNCH_PROGRAM,
            "seeds": [{"utf8": "launch"}, {"pubkey": ADMIN}, {width: 100}],
        }
    )

    assert (result["address"] != live) is expected_differs


@pytest.mark.parametrize(
    "arguments,code",
    [
        ({}, "argument-missing"),
        ({"owner": OWNER}, "argument-missing"),
        ({"owner": OWNER, "mint": USDC}, "argument-missing"),  # no token_program
        (
            {
                "owner": "not-base58!!",
                "mint": USDC,
                "token_program": CLASSIC_TOKEN,
            },
            "argument-invalid",
        ),
    ],
)
def test_derive_ata_refuses_rather_than_returning_something(
    arguments: dict[str, Any], code: str
) -> None:
    result = derive_ata_result(arguments)
    assert result["refused"] is True
    assert result["code"] == code
    assert "address" not in result, "a refusal must not carry an address"


@pytest.mark.parametrize(
    "seeds",
    [
        [{"nonsense": 1}],
        [{"utf8": "a", "pubkey": OWNER}],  # two kinds in one seed — ambiguous
        [],
    ],
    ids=["unknown-kind", "ambiguous-seed", "empty"],
)
def test_derive_pda_refuses_a_seed_it_cannot_read(seeds: list[Any]) -> None:
    """A seed list that cannot be read unambiguously must not produce an address —
    guessing which key was meant is how a caller gets a confident wrong answer."""
    result = derive_pda_result({"program_id": LAUNCH_PROGRAM, "seeds": seeds})
    assert result["refused"] is True
    assert "address" not in result

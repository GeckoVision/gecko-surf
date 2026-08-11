"""The local PDA derivation is pinned to an address the CHAIN confirmed.

`examples/derive_locally.py` talks to two networks, so it cannot be a test. The part worth
pinning is pure: seed recipe in, address out. The expected value below is not a golden file
regenerated from the code it checks — it was read off mainnet, where the account exists and
is owned by the program. That is what makes this a test rather than a tautology.
"""

from __future__ import annotations

import pytest

solders = pytest.importorskip("solders", reason="needs the `solana` extra")

PROGRAM_ID = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"

# store name -> (receipts PDA, bump). `jonasbar` was verified on mainnet: the account
# EXISTS, is 4,908 bytes, and its owner is PROGRAM_ID. Twelve landed purchases wrote to it.
CHAIN_CONFIRMED = {"jonasbar": ("H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V", 253)}


def _derive(store_name: str) -> tuple[str, int]:
    from examples.derive_locally import derive_locally

    return derive_locally(store_name)


@pytest.mark.parametrize("store_name,expected", CHAIN_CONFIRMED.items())
def test_the_derivation_matches_an_account_the_chain_confirmed(
    store_name: str, expected: tuple[str, int]
) -> None:
    assert _derive(store_name) == expected


def test_the_recipe_is_the_seed_pair_and_not_a_hardcoded_table() -> None:
    """A different store name must produce a different address. Without this, a lookup
    table that happened to contain `jonasbar` would pass the test above."""
    assert _derive("jonasbar")[0] != _derive("jonasbar-2")[0]


def test_deriving_needs_no_network() -> None:
    """The whole point: the address is known without asking anyone. If this ever starts
    reaching the network, the property this example demonstrates is gone."""
    import urllib.request

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("derive_locally() reached the network — it must not")

    original = urllib.request.urlopen
    urllib.request.urlopen = refuse  # type: ignore[assignment]
    try:
        assert _derive("jonasbar") == CHAIN_CONFIRMED["jonasbar"]
    finally:
        urllib.request.urlopen = original  # type: ignore[assignment]

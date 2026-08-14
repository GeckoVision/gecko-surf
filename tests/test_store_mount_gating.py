"""The wallet-aware mount is built ONLY when it will actually be gated.

`orquestra` is the PUBLIC storefront and must stay keyless — it is the funnel. Mode B needs
a different door, because a mount that looks up a buyer from an authenticated account is
only meaningful where callers authenticate.

Serving that surface ungated would not move anyone's money — with no gate there is no
account, so every call falls back to mode A. It would do something quieter and worse: the
tool description would tell every agent *"if your account has a wallet bound, omit the
buyer"* on a mount where an account can never exist. A false sentence in a tool
description is the failure mode this whole engine exists to prevent, and it would be OURS.

So the mount is not guarded after the fact — it is **not built** unless all three
conditions hold. There is no configuration in which it exists and is open.
"""

from __future__ import annotations

from typing import Any

from gecko.serve_mcp import STORE_SURFACE, wallet_aware_store_surface
from gecko.wallet_binding import InMemoryWalletDirectory


def _directory() -> InMemoryWalletDirectory:
    return InMemoryWalletDirectory()


def _mount(
    gated: Any, *, wallets: Any = None, require_key: bool | None = True
) -> tuple[str, Any] | None:
    return wallet_aware_store_surface(gated, wallets=wallets, require_key=require_key)


def test_it_is_served_when_gated_configured_and_backed_by_a_directory() -> None:
    mount = _mount([STORE_SURFACE], wallets=_directory())

    assert mount is not None
    name, surface = mount
    assert name == STORE_SURFACE
    assert surface.wallets is not None
    # It must not ride along on the partner's catalog either — same reason the public
    # mount does not: an authenticated call is still not ours to spend upstream.
    assert surface.find_start_pages == 0


def test_no_directory_means_no_mount() -> None:
    """Not a degraded mount — no mount. A wallet-aware surface with nothing to look up in
    would advertise a binding that cannot exist."""
    assert _mount([STORE_SURFACE], wallets=None) is None


def test_the_gate_being_off_means_no_mount() -> None:
    """The stance and the scope are two independent env vars, and this needs both. With
    `GECKO_REQUIRE_KEY` off, naming it gated gates nothing."""
    assert _mount([STORE_SURFACE], wallets=_directory(), require_key=False) is None


def test_not_being_named_in_the_gated_set_means_no_mount() -> None:
    """The exact misconfiguration that would otherwise serve it openly: the gate is ON, but
    it closes only the surfaces named — and this one is not among them."""
    assert _mount(["birdeye"], wallets=_directory()) is None


def test_gate_everything_counts_as_gating_this_one() -> None:
    """`None` is the library default meaning "every mount is gated" — the one case where
    silence is safe, because it can only ever close more doors."""
    assert _mount(None, wallets=_directory()) is not None


def test_the_name_is_matched_case_insensitively_like_the_gate_itself() -> None:
    """A casing slip in `GECKO_GATED_SURFACES` has already left a PAID mount open once.
    Folding can only ever gate MORE surfaces, so it is safe in the fail-closed direction —
    and here, matching the gate's own rule is what keeps the two from disagreeing about
    whether this mount is closed."""
    assert _mount(["STORE"], wallets=_directory()) is not None

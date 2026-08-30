"""Find the pool that converts one mint into another — and make each candidate prove it.

THE ONE IDEA. A `getProgramAccounts` memcmp PROPOSES candidates; the packaged seed recipe
DISPOSES of them. Every candidate must reproduce its own address from its own on-chain
configuration (config, both mints, tick spacing), and one that cannot is DROPPED rather
than ranked lower.

That asymmetry is the whole safety argument, and it is not a style preference. The
second-best answer here is a real, funded, working pool — it would accept the money and
report success. Ranking a wrong pool lower still leaves it reachable; dropping it means a
wrong field offset REFUTES ITSELF instead of returning something plausible.

Offsets come from the IDL (`idl_layout`), never from a hardcoded size table. The script
this replaces carried a private `_SIZES` map, which silently produces wrong offsets the
day a field is added upstream — exactly the failure the re-derivation check exists to
catch, arriving through the door the check cannot see.

Nothing here signs, builds bytes, or persists a response. The RPC call is injected.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .idl_layout import LayoutError, account_discriminator, field_layout
from .pda import b58_encode, derive_pda
from .rpc import RpcCall

__all__ = [
    "TICKS_PER_ARRAY",
    "WHIRLPOOL_PROGRAM",
    "Direction",
    "Venue",
    "VerifyStatus",
    "WhirlpoolAccount",
    "WhirlpoolIdlIncomplete",
    "WhirlpoolLayout",
    "decode_whirlpool",
    "find_venues",
    "tick_array_start",
    "tick_arrays",
    "whirlpool_layout",
]

WHIRLPOOL_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"

#: Ticks per tick-array account. A count of TICKS, not of price steps — the range one
#: array spans is this times the pool's own tick_spacing, so a fixed span is wrong for
#: every pool but spacing=1.
TICKS_PER_ARRAY = 88

#: Which side of the pool the caller is selling. `swap_v2` needs it explicitly.
Direction = Literal["a_to_b", "b_to_a"]
#: How a venue earned its place. Only one value exists today, and that is the point: a
#: candidate that did not re-derive never becomes a Venue at all.
VerifyStatus = Literal["rederived"]

#: The fields this module reads. Every one is located through the IDL.
_FIELDS = (
    "whirlpools_config",
    "tick_spacing",
    "token_mint_a",
    "token_mint_b",
    "liquidity",
    "fee_rate",
    "sqrt_price",
)
_PUBKEYS = frozenset({"whirlpools_config", "token_mint_a", "token_mint_b"})


class WhirlpoolIdlIncomplete(Exception):
    """The Whirlpool IDL cannot describe the pool account well enough to read it.

    Wraps `idl_layout.LayoutError` on purpose: a caller of this module should not have to
    catch an exception from a layout helper it never called, and an uncaught LayoutError
    would escape a payability check as an unhandled crash rather than a refusal.
    """


@dataclass(frozen=True)
class WhirlpoolLayout:
    """Where each field lives in a Whirlpool account, derived from the IDL."""

    discriminator: bytes
    fields: Mapping[str, tuple[int, int]]  # name -> (offset, width)


@dataclass(frozen=True)
class WhirlpoolAccount:
    """One decoded pool. A typed record rather than a dict, because it crosses a boundary."""

    whirlpools_config: str
    token_mint_a: str
    token_mint_b: str
    tick_spacing: int
    liquidity: int
    fee_rate: int
    sqrt_price: int


@dataclass(frozen=True)
class Venue:
    """A pool that PROVED it is the pool its own configuration describes."""

    pool: str
    tick_spacing: int
    liquidity: int
    fee_rate: int
    sqrt_price: int
    direction: Direction
    verify: VerifyStatus
    token_mint_a: str
    token_mint_b: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "tick_spacing": self.tick_spacing,
            "liquidity": str(self.liquidity),
            "fee_rate": self.fee_rate,
            "sqrt_price": str(self.sqrt_price),
            "direction": self.direction,
            "verify": self.verify,
            "token_mint_a": self.token_mint_a,
            "token_mint_b": self.token_mint_b,
        }


def whirlpool_layout(idl: Mapping[str, Any]) -> WhirlpoolLayout:
    """Locate every field this module reads, from the IDL itself."""
    try:
        disc = account_discriminator(idl, "Whirlpool")
    except LayoutError as exc:
        raise WhirlpoolIdlIncomplete(f"Whirlpool discriminator: {exc}") from exc
    if not disc:
        raise WhirlpoolIdlIncomplete(
            "the Whirlpool IDL declares no discriminator for its pool account"
        )
    located: dict[str, tuple[int, int]] = {}
    for name in _FIELDS:
        try:
            got = field_layout(idl, "Whirlpool", name)
        except LayoutError as exc:
            raise WhirlpoolIdlIncomplete(f"Whirlpool.{name}: {exc}") from exc
        located[name] = (int(got["offset"]), int(got["width"]))
    return WhirlpoolLayout(discriminator=disc, fields=located)


def decode_whirlpool(data: bytes, layout: WhirlpoolLayout) -> WhirlpoolAccount:
    """Read one pool account. Pure: no network, no state."""

    def read(name: str) -> Any:
        offset, width = layout.fields[name]
        chunk = data[offset : offset + width]
        if len(chunk) < width:
            raise WhirlpoolIdlIncomplete(
                f"account data is shorter than the IDL says {name} needs"
            )
        return (
            b58_encode(chunk) if name in _PUBKEYS else int.from_bytes(chunk, "little")
        )

    return WhirlpoolAccount(
        whirlpools_config=read("whirlpools_config"),
        token_mint_a=read("token_mint_a"),
        token_mint_b=read("token_mint_b"),
        tick_spacing=read("tick_spacing"),
        liquidity=read("liquidity"),
        fee_rate=read("fee_rate"),
        sqrt_price=read("sqrt_price"),
    )


def find_venues(
    rpc_url: str,
    held_mint: str,
    needed_mint: str,
    *,
    layout: WhirlpoolLayout,
    recipe: Any,
    rpc_call: RpcCall,
    min_liquidity: int = 1,
) -> list[Venue]:
    """Pools trading this pair, each proven by re-deriving its own address, richest first.

    Both orderings are queried because a pool stores its mints in a fixed order and the
    caller's held/needed pair may be either way round; the ordering that matched is what
    `direction` records, and `swap_v2` needs it.

    ``min_liquidity`` defaults to 1, which excludes a dead pool and deliberately leaves a
    THIN pool visible — sizing it is the caller's problem and a thin pool is a real answer.
    """
    found: list[Venue] = []
    for mint_a, mint_b, direction in (
        (held_mint, needed_mint, "a_to_b"),
        (needed_mint, held_mint, "b_to_a"),
    ):
        rows = _program_accounts(
            rpc_url, mint_a, mint_b, layout=layout, rpc_call=rpc_call
        )
        for row in rows:
            pubkey = row.get("pubkey")
            blob = _row_data(row)
            if not pubkey or blob is None:
                continue
            account = decode_whirlpool(blob, layout)
            if account.liquidity < min_liquidity:
                continue
            derived = derive_pda(
                recipe,
                {
                    "whirlpools_config": account.whirlpools_config,
                    "token_mint_a": account.token_mint_a,
                    "token_mint_b": account.token_mint_b,
                    "tick_spacing": account.tick_spacing,
                },
            ).address
            if derived != pubkey:
                # It proposed and did not dispose. Not our pool — and NOT ranked lower,
                # because a wrong pool that is still reachable is the incident.
                continue
            found.append(
                Venue(
                    pool=pubkey,
                    tick_spacing=account.tick_spacing,
                    liquidity=account.liquidity,
                    fee_rate=account.fee_rate,
                    sqrt_price=account.sqrt_price,
                    direction=direction,  # type: ignore[arg-type]
                    verify="rederived",
                    token_mint_a=account.token_mint_a,
                    token_mint_b=account.token_mint_b,
                )
            )
    return sorted(found, key=lambda v: -v.liquidity)


def _program_accounts(
    rpc_url: str,
    mint_a: str,
    mint_b: str,
    *,
    layout: WhirlpoolLayout,
    rpc_call: RpcCall,
) -> Sequence[Mapping[str, Any]]:
    a_off = layout.fields["token_mint_a"][0]
    b_off = layout.fields["token_mint_b"][0]
    result = rpc_call(
        rpc_url,
        "getProgramAccounts",
        [
            WHIRLPOOL_PROGRAM,
            {
                "encoding": "base64",
                "filters": [
                    {
                        "memcmp": {
                            "offset": 0,
                            "bytes": b58_encode(layout.discriminator),
                        }
                    },
                    {"memcmp": {"offset": a_off, "bytes": mint_a}},
                    {"memcmp": {"offset": b_off, "bytes": mint_b}},
                ],
            },
        ],
    ).get("result")
    return result or []


def _row_data(row: Mapping[str, Any]) -> bytes | None:
    data = ((row.get("account") or {}).get("data")) or None
    if isinstance(data, list) and data:
        return base64.b64decode(data[0])
    return None


def tick_array_start(tick_current: int, *, tick_spacing: int) -> int:
    """The start index of the tick array CONTAINING ``tick_current``.

    Floors toward negative infinity, which is what Python's ``//`` already does and what
    this needs. Truncating toward zero — C semantics, or ``int(a / b)`` — puts a tick of
    -1 into array 0 instead of array -88, and the result is a REAL, derivable, perfectly
    well-formed account for the wrong region of the curve. The swap then fails at the
    program rather than at the derivation, which is the expensive place to find out.
    """
    if tick_spacing <= 0:
        raise WhirlpoolIdlIncomplete(
            f"tick_spacing must be positive, got {tick_spacing}"
        )
    span = TICKS_PER_ARRAY * tick_spacing
    return (tick_current // span) * span


def tick_arrays(
    pool: str, *, tick_current: int, tick_spacing: int, upward: bool, recipe: Any = None
) -> list[str]:
    """The three tick arrays a swap needs, IN THE DIRECTION OF TRAVEL.

    Lifted out of ``scripts/prepare_whirlpool_swap.py``'s ``main()``, where nothing could
    call it: the PDA recipe was covered (``tests/test_whirlpool_config.py`` derives the
    accounts a real Jupiter swap passed) but the arithmetic choosing WHICH three was not.

    The seed is the ASCII DECIMAL STRING of the start index, not its little-endian bytes.
    The IDL declares the arg as ``i32`` and an argument's TYPE does not determine its seed
    ENCODING — that distinction has cost us a wrong address before.
    """
    if recipe is None:
        from .provider_config import load_packaged_provider

        _, apis = load_packaged_provider("orquestra")
        program = apis["whirlpool"].program
        if program is None:  # pragma: no cover - the packaged config always carries it
            raise WhirlpoolIdlIncomplete(
                "the packaged whirlpool config declares no program"
            )
        recipe = dict(program.pdas)["tick_array"]

    span = TICKS_PER_ARRAY * tick_spacing
    start = tick_array_start(tick_current, tick_spacing=tick_spacing)
    steps = (
        [start + span * i for i in range(3)]
        if upward
        else [start - span * i for i in range(3)]
    )
    return [
        derive_pda(recipe, {"whirlpool": pool, "start_tick_index": str(s)}).address
        for s in steps
    ]

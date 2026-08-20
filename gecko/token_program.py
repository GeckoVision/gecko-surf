"""Which token program OWNS a mint — read from the mint account, never inferred.

WHY THIS EXISTS. Two mints can carry the same human label and be different assets on
different token programs. USDG (``2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH``, 6
decimals) is owned by Token-2022; the USDC a ``let_me_buy`` store prices in
(``EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v``) is owned by the classic SPL Token
program. Both read as "a dollar stablecoin" to a person, and a wallet holding one CANNOT
pay with it where the other is asked for: the ATA, the transfer instruction and the
program that must sign the CPI are all different. A menu that shows only the friendly name
hands an agent a look-alike and no way to see the mismatch before it spends.

THE ONE RULE. The token program is the ``owner`` of the MINT account, so it is READ —
never guessed from decimals, from the mint address, from a hardcoded list of known mints,
or from a label. A guessed token program is exactly the wrong-but-well-formed class this
value exists to prevent, so an unreadable mint comes back as UNKNOWN with a stated reason.
Unknown and classic are different values, and defaulting the common case would make the
field worthless precisely when the chain disagrees with the label.

Control plane only: ``getAccountInfo``'s ``owner`` is public chain metadata, decoded in
memory. Nothing here is persisted — the cache lives for the duration of one call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .rpc import RpcCall, RpcError, default_rpc_call

__all__ = [
    "CLASSIC_SPL_TOKEN_PROGRAM",
    "TOKEN_2022_PROGRAM",
    "MintTokenProgram",
    "classify_token_program",
    "read_mint_token_programs",
    "unknown_token_program",
]

#: The two SPL token programs. Which one owns a given mint is on-chain state.
CLASSIC_SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

_NAMES = {
    CLASSIC_SPL_TOKEN_PROGRAM: "classic-spl-token",
    TOKEN_2022_PROGRAM: "token-2022",
}

#: Caps on work driven by untrusted node output. A listing with more distinct mints than
#: this reads the first ones and reports the rest as unknown — bounded work, and the
#: unread ones say they were unread rather than passing as classic.
_MAX_DISTINCT_MINTS = 128
#: How many accounts one ``getMultipleAccounts`` asks for. The RPC spec's own limit is 100.
_CHUNK = 100


@dataclass(frozen=True)
class MintTokenProgram:
    """The program that owns one mint, or an honest statement that it was not read."""

    mint: str
    #: The owning program's base58 address, or ``None`` when it could not be read.
    address: str | None
    #: ``"classic-spl-token"`` / ``"token-2022"`` / the raw owner address when it is
    #: neither / ``"unknown"`` when the mint could not be read. An agent should not have
    #: to recognise base58 to see a mismatch.
    name: str
    #: Why the program is unknown. ``None`` when it was read.
    reason: str | None = None

    @property
    def known(self) -> bool:
        return self.address is not None

    @property
    def recognised(self) -> bool:
        """True when the owner is one of the two token programs we can name."""
        return self.address in _NAMES

    def field(self) -> dict[str, Any]:
        """The shape this value takes on a tool's output — flat, JSON-safe, honest."""
        out: dict[str, Any] = {
            "address": self.address,
            "name": self.name,
            "read": self.known,
            # False means "the owner is not one of the two SPL token programs" — either
            # unknown, or an address that owns this account for some other reason (a
            # non-mint address gives its own loader here). Stated rather than left for a
            # reader to infer from base58 it may not recognise.
            "recognised": self.recognised,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        return out


def classify_token_program(owner: str) -> str:
    """Name an owner program, falling back to its own address rather than to a guess."""
    return _NAMES.get(owner, owner)


def unknown_token_program(mint: str, reason: str) -> MintTokenProgram:
    """The fail-closed value: not read, and the reason it was not.

    Public because every caller that has to invent this value must invent the SAME one —
    "unknown" is a distinct answer from "classic", and a caller free to spell it its own
    way is a caller free to spell it ``classic-spl-token``.
    """
    return MintTokenProgram(mint=mint, address=None, name="unknown", reason=reason)


_unknown = unknown_token_program


def _read_chunk(
    chunk: list[str], *, rpc_url: str, call: RpcCall
) -> dict[str, MintTokenProgram]:
    """One ``getMultipleAccounts`` round trip. Every failure lands as unknown, not classic."""
    try:
        response = call(rpc_url, "getMultipleAccounts", [chunk, {"encoding": "base64"}])
    except RpcError as exc:
        # The failure CLASS only — an RPC error body is untrusted transport output.
        return {
            m: _unknown(m, f"the mint could not be read: RpcError: {exc}")
            for m in chunk
        }
    except (OSError, ValueError) as exc:
        return {
            m: _unknown(m, f"the mint could not be read: {type(exc).__name__}")
            for m in chunk
        }

    result = response.get("result") if isinstance(response, dict) else None
    values = result.get("value") if isinstance(result, dict) else None
    if not isinstance(values, list) or len(values) != len(chunk):
        return {
            m: _unknown(m, "the node did not answer getMultipleAccounts for this mint")
            for m in chunk
        }

    out: dict[str, MintTokenProgram] = {}
    for mint, value in zip(chunk, values):
        if not isinstance(value, dict):
            # A null entry means no account exists at that address on this node — so the
            # "mint" a store prices in is not a mint here at all. That is a fact worth
            # showing, and it is emphatically not "assume classic".
            out[mint] = _unknown(mint, "no account exists at this address on this node")
            continue
        owner = value.get("owner")
        if not isinstance(owner, str) or not owner:
            out[mint] = _unknown(
                mint, "the account carries no owner in the node's answer"
            )
            continue
        out[mint] = MintTokenProgram(
            mint=mint, address=owner, name=classify_token_program(owner)
        )
    return out


def read_mint_token_programs(
    mints: Iterable[str],
    *,
    rpc_url: str,
    rpc_call: RpcCall | None = None,
) -> dict[str, MintTokenProgram]:
    """Read the owning token program of each DISTINCT mint, once, in batches.

    Deduplicates first — a menu prices many products in the same mint, and reading it once
    per product would be one round trip per line item. Returns a mint → :class:`MintTokenProgram`
    map covering every input mint; a mint that could not be read is present and UNKNOWN,
    never absent and never defaulted.
    """
    call = rpc_call or default_rpc_call
    distinct = list(dict.fromkeys(m for m in mints if isinstance(m, str) and m))
    readable, overflow = distinct[:_MAX_DISTINCT_MINTS], distinct[_MAX_DISTINCT_MINTS:]

    out: dict[str, MintTokenProgram] = {
        mint: _unknown(
            mint,
            f"more than {_MAX_DISTINCT_MINTS} distinct mints in one listing; "
            "this one was not read",
        )
        for mint in overflow
    }
    for start in range(0, len(readable), _CHUNK):
        out.update(
            _read_chunk(readable[start : start + _CHUNK], rpc_url=rpc_url, call=call)
        )
    return out

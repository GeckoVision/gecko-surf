"""Ask Pegana about one mint, and report WHAT CAME BACK rather than what it implies.

This module does no judging. It turns two HTTP reads into a `PegReading`, and the single
rule it exists to enforce is that ``tracked`` is established from the HTTP **status**,
never from the shape of a 200 body.

WHY THAT RULE IS THE WHOLE MODULE. The code this replaces
(``scripts/pay_with_any_token.py``) caught bare ``Exception`` and returned "unknown",
which does not block — so an unreachable oracle read as permission to convert. The
obvious repair is worse: treating "the body has no ``symbol`` key" as "Pegana has no
opinion" moves the fail-open rather than removing it, because every degraded 200 then
forges that proof — an empty body, ``{}``, a gateway maintenance page, a rate-limit
served as 200, a schema change that wraps the card in ``data``. Those are the NORMAL
shapes of a degraded third party, so the forged path is the likely one.

A 404 is the only thing that means "I do not track this asset". Everything else that is
not a valid card means "I could not ask", and `peg_guard.verdict_from_reading` blocks on
it. This module needs `netguard.safe_get_status` for exactly that reason: `safe_get`
raises on 404, which would make the one honest answer indistinguishable from a failure.

Control plane: a reading holds a parsed state body in memory to hand to the judge and is
never persisted. ``error`` carries an exception CLASS NAME only — never a URL, a response
body, or a credential.
"""

from __future__ import annotations

import functools
import json
import urllib.error
from collections.abc import Callable, Mapping
from typing import Any

from .netguard import UnsafeUrlError, safe_get_status
from .peg_guard import PegReading, PegReader

__all__ = [
    "PEGANA_BASE",
    "Getter",
    "PeganaError",
    "PegReading",
    "pegana_reader",
    "recorded_peg_reader",
]

#: Pegana — the peg-risk oracle, addressed BY MINT: the same value domain the store and
#: the pool speak, which is why no symbol translation is needed to join them.
PEGANA_BASE = "https://api.pegana.xyz"

#: (url) -> (status, body). The seam. `safe_get_status` in production, a fake in tests.
Getter = Callable[[str], tuple[int, str]]

#: Base58 has no 0, O, I or l. A mint is 32 bytes, so 32-44 characters.
_B58 = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

#: Transport failures. Anything NOT in here — a TypeError, an AttributeError — is our own
#: bug and must propagate: swallowing it would report a broken reader as a peg opinion.
_TRANSPORT = (
    UnsafeUrlError,
    urllib.error.URLError,
    urllib.error.HTTPError,
    TimeoutError,
    OSError,
    json.JSONDecodeError,
)


class PeganaError(Exception):
    """A request we refused to make — a malformed mint, never a transport failure."""


def _validate_mint(mint: str) -> str:
    """Refuse anything that is not a bare base58 mint BEFORE it reaches a URL.

    The mint is interpolated into a path, so a value carrying ``/``, ``..`` or a query
    separator would address a different endpoint than the one this code believes it is
    calling. Validated here rather than trusted from the caller.
    """
    if not isinstance(mint, str):
        raise PeganaError("mint must be a string")
    if not (32 <= len(mint) <= 44) or not set(mint) <= _B58:
        raise PeganaError("mint is not a base58 account address; refusing to fetch")
    return mint


def _card_symbol(body: str) -> str | None:
    """The asset symbol from a card body, or None if this is not a card.

    Deliberately strict. A caller must not be able to tell "untracked" from this — the
    STATUS decides that — so every rejection here lands in "could not ask".
    """
    try:
        parsed: Any = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    symbol = parsed.get("symbol")
    if isinstance(symbol, str) and symbol.strip():
        return symbol.strip()
    return None


def _default_getter(*, max_bytes: int = 64_000, timeout: int = 15) -> Any:
    """The SSRF-safe getter, bound with a byte cap and a timeout."""
    return functools.partial(safe_get_status, max_bytes=max_bytes, timeout=timeout)


def pegana_reader(
    *,
    base: str = PEGANA_BASE,
    get: Getter | None = None,
    max_bytes: int = 64_000,
    timeout: int = 15,
) -> PegReader:
    """Build a `PegReader` that asks Pegana about a mint over two reads.

    Read 1 — ``/v1/assets/by-mint/{mint}`` resolves the mint to an asset card:
      * ``404``                          -> ``tracked=False``. The ONLY proof of no opinion.
      * ``2xx`` that validates as a card -> continue to read 2.
      * anything else                    -> ``tracked=None``. We could not ask.

    Read 2 — ``/v1/assets/{symbol}/state`` is the peg reading itself. Any failure leaves
    ``tracked=True, state_body=None``, which the judge reports as undetermined naming the
    symbol — never as "untracked", which would be a different and false claim.
    """
    fetch: Getter = get or _default_getter(max_bytes=max_bytes, timeout=timeout)
    root = base.rstrip("/")

    def read(mint: str) -> PegReading:
        _validate_mint(mint)
        try:
            status, body = fetch(f"{root}/v1/assets/by-mint/{mint}")
        except _TRANSPORT as exc:
            return PegReading(tracked=None, error=type(exc).__name__)

        if status == 404:
            return PegReading(tracked=False, status=404)
        if not (200 <= status < 300):
            return PegReading(tracked=None, status=status)

        symbol = _card_symbol(body)
        if symbol is None:
            # A 200 we cannot read as a card. NOT evidence of "untracked".
            return PegReading(tracked=None, status=status)

        try:
            state_status, state_body = fetch(f"{root}/v1/assets/{symbol}/state")
        except _TRANSPORT as exc:
            return PegReading(
                tracked=True, symbol=symbol, state_body=None, error=type(exc).__name__
            )

        if not (200 <= state_status < 300):
            return PegReading(tracked=True, symbol=symbol, status=state_status)
        try:
            parsed = json.loads(state_body)
        except (json.JSONDecodeError, TypeError):
            return PegReading(
                tracked=True,
                symbol=symbol,
                error="JSONDecodeError",
                status=state_status,
            )
        if not isinstance(parsed, dict):
            return PegReading(tracked=True, symbol=symbol, status=state_status)
        return PegReading(
            tracked=True, symbol=symbol, state_body=parsed, status=state_status
        )

    return read


def recorded_peg_reader(readings: Mapping[str, PegReading]) -> PegReader:
    """The $0 lane: serve pinned readings, and BLOCK on anything not pinned.

    There is deliberately no ``default_tracked`` parameter. A safety gate whose offline
    simulation is more permissive than its live path cannot falsify the live path, and
    this is the lane every test of the route runs in — a default of "untracked" would
    make every unpinned mint silently convertible in exactly the mode we use to prove
    the guard works.
    """
    pinned = dict(readings)

    def read(mint: str) -> PegReading:
        _validate_mint(mint)
        return pinned.get(mint, PegReading(tracked=None, error="not recorded"))

    return read

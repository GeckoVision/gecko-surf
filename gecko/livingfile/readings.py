"""Each source's read, turned into ``Reading``s: canonical value, our summary, source.

Values are code-built (``checks.plain`` digits, base58, hex), so they are safe to show
a model. The only upstream words kept are the legal sentences, in ``value_summary``.
"""

from __future__ import annotations

from decimal import Decimal

from .chain import MintRead
from .checks import ReservesRead, plain
from .facts import (
    DECIMALS,
    EXTENSIONS,
    FREEZE_AUTHORITY,
    MINT_AUTHORITY,
    MULTIPLIER,
    RESERVES,
    SHARE_PRICE,
    SUPPLY_RAW,
    SUPPLY_SCALED,
    SUPPLY_UI,
    TOKEN_LIQUIDITY,
    TOKEN_PRICE,
    TOKEN_PROGRAM,
    Reading,
)
from .legal import LegalRead
from .market import SharePrice, TokenMarket


def _num(value: Decimal | None) -> str | None:
    return plain(value) if value is not None else None


def _say(name: str, value: str | None, why: str) -> str:
    return f"{name}: {value}" if value is not None else f"{name}: no data ({why})"


def chain_readings(read: MintRead, *, rpc_host: str, at: str) -> list[Reading]:
    supply_src = f"Solana RPC getTokenSupply ({rpc_host})"
    account_src = f"Solana RPC getAccountInfo jsonParsed ({rpc_host})"
    supply_why = read.trouble.get("getTokenSupply", "not in the reply")
    account_why = read.trouble.get("getAccountInfo", "not in the reply")
    slot = f"slot {read.slot}" if read.slot is not None else ""
    program = read.token_program
    named = read.token_program_name
    scaled = read.supply_scaled()
    authority_note = "issuer-controlled: a lender risk by itself"
    pairs: list[tuple[str, str | None, str, str, str]] = [
        (SUPPLY_RAW, read.supply_raw, supply_src, supply_why, slot),
        (
            SUPPLY_UI,
            read.supply_ui_rpc,
            supply_src,
            supply_why,
            "the node's uiAmountString; nodes differ on applying the multiplier",
        ),
        (
            DECIMALS,
            str(read.decimals) if read.decimals is not None else None,
            supply_src,
            supply_why,
            "",
        ),
        (MINT_AUTHORITY, read.mint_authority, account_src, account_why, authority_note),
        (
            FREEZE_AUTHORITY,
            read.freeze_authority,
            account_src,
            account_why,
            authority_note,
        ),
        (
            TOKEN_PROGRAM,
            program,
            account_src,
            account_why,
            f"{named} (the mint account's owner)" if named else "",
        ),
        (
            EXTENSIONS,
            ",".join(read.extensions) if read.extensions is not None else None,
            account_src,
            account_why,
            "",
        ),
        (
            MULTIPLIER,
            _num(read.multiplier),
            account_src,
            account_why,
            read.multiplier_note,
        ),
        (
            SUPPLY_SCALED,
            _num(scaled),
            "computed: supply_raw / 10^decimals x multiplier",
            "an input was not read",
            "Solana supply only",
        ),
    ]
    out = []
    for name, value, source, why, note in pairs:
        summary = _say(name, value, why)
        if name == TOKEN_PROGRAM and named and value:
            summary = f"token_program: {named} ({value})"
        out.append(
            Reading(name, value, source, at, summary, note=note if value else why)
        )
    return out


def market_readings(
    market: TokenMarket, *, jupiter_host: str, at: str
) -> list[Reading]:
    source = f"Jupiter tokens v2 ({jupiter_host})"
    why = market.reason or "not in the reply"
    price, liquidity = _num(market.usd_price), _num(market.liquidity_usd)
    return [
        Reading(
            TOKEN_PRICE,
            price,
            source,
            at,
            _say(TOKEN_PRICE, price, why),
            note=why if price is None else "",
        ),
        Reading(
            TOKEN_LIQUIDITY,
            liquidity,
            source,
            at,
            _say(TOKEN_LIQUIDITY, liquidity, why),
            note=why if liquidity is None else "",
        ),
    ]


def share_reading(share: SharePrice | None, ticker: str, at: str, why: str) -> Reading:
    source = f"financial-datasets /prices/snapshot via Orthogonal ({ticker})"
    if share is None or share.price is None:
        reason = share.reason if share and share.reason else why
        return Reading(
            SHARE_PRICE, None, source, at, _say(SHARE_PRICE, None, reason), note=reason
        )
    value = plain(share.price)
    note = (
        f"snapshot time {share.as_of}"
        if share.as_of
        else "the snapshot carried no time"
    )
    return Reading(
        SHARE_PRICE, value, source, at, _say(SHARE_PRICE, value, ""), note=note
    )


def legal_reading(read: LegalRead, at: str) -> Reading:
    source = f"{read.page.label} ({read.page.url})"
    if read.sha256 is None:
        return Reading(
            read.page.field,
            None,
            source,
            at,
            f"{read.page.field}: no data ({read.reason})",
            note=read.reason,
        )
    return Reading(
        read.page.field,
        read.sha256,
        source,
        at,
        f"{read.page.field}: sha256 {read.sha256[:12]}…, {read.chars} characters, "
        f"{len(read.sentences)} key sentences kept in the file",
        value_summary=" | ".join(read.sentences),
        note="sha256 of the normalized page text",
    )


def reserves_reading(read: ReservesRead, at: str) -> Reading:
    value = _num(read.shares)
    if value is None:
        return Reading(
            RESERVES,
            None,
            read.source,
            at,
            f"reserves: not checked ({read.reason})",
            note=read.reason,
        )
    return Reading(
        RESERVES, value, read.source, at, _say(RESERVES, value, ""), note=read.reason
    )

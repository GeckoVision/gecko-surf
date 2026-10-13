"""The xStocks the living file knows, and where each fact comes from — as DATA.

Mints, tickers and URLs below were checked on 2026-10-08: the four xStocks Jupiter Lend accepts, their Solana mints (RPC ``getTokenSupply``), the
Jupiter tokens endpoint that answered without a key, and the two legal pages that were
read. Every URL is a DEFAULT: the deps take another one, and nothing in the job names a
host. The ticker is the bridge to the paid share price, so an unknown mint is refused
rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

from .errors import LivingFileConfigError, UnknownMintError

#: Base58, 32 to 44 characters: the shape of a Solana address. Not a checksum.
BASE58_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@dataclass(frozen=True)
class XStock:
    mint: str
    symbol: str
    #: The listed share the token tracks; the paid share-price call asks for it.
    ticker: str


NVDAX = XStock("Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh", "NVDAx", "NVDA")
TSLAX = XStock("XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB", "TSLAx", "TSLA")
SPYX = XStock("XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W", "SPYx", "SPY")
QQQX = XStock("Xs8S1uUs1zvS2p7iwtsG3b6fkhpvmwz4GYU3gWAmWHZ", "QQQx", "QQQ")
XSTOCKS: dict[str, XStock] = {x.mint: x for x in (NVDAX, TSLAX, SPYX, QQQX)}


def xstock_for(mint: object) -> XStock:
    if not isinstance(mint, str) or not BASE58_ADDRESS.match(mint.strip()):
        raise UnknownMintError("That is not a Solana mint address.")
    found = XSTOCKS.get(mint.strip())
    if found is None:
        known = ", ".join(f"{x.symbol} {x.mint}" for x in XSTOCKS.values())
        raise UnknownMintError(
            f"The living file knows four xStocks, not this mint. Known: {known}."
        )
    return found


@dataclass(frozen=True)
class LegalPage:
    field: str
    label: str
    url: str


#: Public mainnet RPC: read-only methods only, rate-limited, fine for two calls a run.
DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
#: Tokens v2 search: ``usdPrice`` and ``liquidity`` per token, no key on 2026-10-08.
#: ``{mint}`` is filled with a validated mint; the reply is matched on ``id == mint``.
DEFAULT_JUPITER_URL = "https://api.jup.ag/tokens/v2/search?query={mint}"
LEGAL_PAGES: tuple[LegalPage, ...] = (
    LegalPage(
        "legal_jupiter_docs",
        "Jupiter xStocks docs",
        "https://docs.jup.ag/user-docs/earn/lend/borrow/xstocks",
    ),
    LegalPage(
        "legal_kraken", "Kraken xStocks legal", "https://www.kraken.com/legal/xstocks"
    ),
)
#: Red above this many percent between token and share price. Config, not law.
DEFAULT_GAP_THRESHOLD_PCT = Decimal("2")
#: Why reserves are not read: no address was found to read them from.
RESERVES_NOT_CHECKED = (
    "no documented public Chainlink proof-of-reserve feed address for "
    "this xStock was found (checked offline 2026-10-08; Backed's PoR article predates "
    "xStocks and names no Solana feed). Gecko does not invent an address."
)


def check_https_url(url: object, what: str) -> str:
    """https, a host, no credentials, no fragment. DNS is checked per request."""
    parts = urlsplit(url) if isinstance(url, str) else None
    if (
        parts is None
        or parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise LivingFileConfigError(f"The {what} URL must be a plain https URL.")
    return str(url)


def host_of(url: str) -> str:
    """What a label or a receipt may show of a configured URL: the host, never the
    query, where a paid RPC puts its key."""
    return urlsplit(url).hostname or "unknown host"


def jupiter_url(template: str, mint: str) -> str:
    if "{mint}" not in template:
        raise LivingFileConfigError("The Jupiter URL needs a {mint} placeholder.")
    return check_https_url(template.replace("{mint}", mint), "Jupiter")

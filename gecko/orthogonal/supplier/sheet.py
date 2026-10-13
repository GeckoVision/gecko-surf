"""Read the supplier sheet (name, domain, ticker) from ``~/gecko-lists/`` and nowhere else.

The folder guard, the size cap and the CSV sniffing are the commenter job's
(``..sheet.read_rows``). A row is kept only with a name and a domain that normalizes to
a plain hostname, because the domain is what fills every upstream path; a row that
fails is skipped with a notice that names its line, never its content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ..errors import SheetError
from ..sheet import column, read_rows
from ..spec import DOMAIN_RE

#: Ten suppliers is ~$2 of calls, far past the run cap; the cap is the real limit.
MAX_SUPPLIERS = 10
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_NAME_HEADERS = {"name", "supplier", "company", "nome", "fornecedor", "empresa"}
_DOMAIN_HEADERS = {"domain", "website", "site", "url", "dominio"}
_TICKER_HEADERS = {"ticker", "symbol", "stock"}


@dataclass(frozen=True)
class Supplier:
    name: str
    domain: str
    ticker: str | None = None


@dataclass
class SupplierSheet:
    suppliers: list[Supplier] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


def normalize_domain(raw: str) -> str | None:
    """``"https://www.Jabil.com/about"`` -> ``"jabil.com"``; anything odd -> ``None``."""
    text = raw.strip().lower()
    if not text or len(text) > 253:
        return None
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.username or parts.password or parts.port:
        return None
    host = (parts.hostname or "").rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host if DOMAIN_RE.match(host) else None


def normalize_ticker(raw: str) -> str | None:
    text = raw.strip().upper()
    return text if _TICKER.match(text) else None


def read_suppliers(name: str, folder: Path | None = None) -> SupplierSheet:
    headers, rows = read_rows(name, folder)
    name_col = column(headers, _NAME_HEADERS)
    domain_col = column(headers, _DOMAIN_HEADERS)
    ticker_col = column(headers, _TICKER_HEADERS)
    if name_col is None or domain_col is None:
        raise SheetError(
            "The supplier sheet needs columns named `name` and `domain` "
            "(`ticker` is optional)."
        )
    sheet = SupplierSheet()
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):
        cell_name = row[name_col].strip() if name_col < len(row) else ""
        domain = normalize_domain(row[domain_col]) if domain_col < len(row) else None
        if not cell_name or domain is None:
            sheet.notices.append(f"Row {line} skipped: it needs a name and a domain.")
            continue
        if domain in seen:
            sheet.notices.append(f"Row {line} skipped: that domain is already listed.")
            continue
        ticker = None
        if ticker_col is not None and ticker_col < len(row) and row[ticker_col].strip():
            ticker = normalize_ticker(row[ticker_col])
            if ticker is None:
                sheet.notices.append(
                    f"Row {line}: the ticker is not usable; skipped it."
                )
        seen.add(domain)
        sheet.suppliers.append(Supplier(cell_name[:120], domain, ticker))
    if not sheet.suppliers:
        raise SheetError("The supplier sheet has no usable rows (name + domain).")
    if len(sheet.suppliers) > MAX_SUPPLIERS:
        raise SheetError(
            f"The sheet lists {len(sheet.suppliers)} suppliers; one run takes at most "
            f"{MAX_SUPPLIERS}. Split it."
        )
    return sheet

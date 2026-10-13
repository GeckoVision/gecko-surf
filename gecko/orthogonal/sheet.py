"""Read her customer sheet from ONE folder, in memory, and keep only what matching needs.

The folder is the whole permission model: a sheet name that resolves anywhere else
(``..``, an absolute path, a symlink out) is refused by name before anything is opened.
The sheet is never written anywhere; only the handle set and the names of rows that
have no handle survive this module, and only for the length of one run.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import SheetError, SheetOutsideFolderError

MAX_SHEET_BYTES = 5 * 1024 * 1024

_HANDLE_HEADERS = {"instagram", "ig", "insta", "handle", "instagram_handle", "usuario"}
_NAME_HEADERS = {"name", "nome", "cliente", "customer", "full_name", "nome_completo"}
_HANDLE_CHARS = re.compile(r"^[a-z0-9._]{1,30}$")
_IG_URL = re.compile(r"^(?:https?://)?(?:www\.)?instagram\.com/", re.IGNORECASE)


def default_folder() -> Path:
    return Path.home() / "gecko-lists"


def normalize_handle(raw: str) -> str | None:
    """``"@Ana.Silva "`` / ``instagram.com/ana.silva/`` -> ``"ana.silva"``; junk -> None."""
    text = _IG_URL.sub("", raw.strip())
    text = text.split("?", 1)[0].strip("/").lstrip("@").strip().lower()
    return text if _HANDLE_CHARS.match(text) else None


def normalize_name(raw: str) -> str:
    return " ".join(raw.lower().split())


@dataclass
class CustomerSheet:
    handles: set[str] = field(default_factory=set)
    #: Names of rows WITHOUT a usable handle (a name match there is only "not sure").
    names_without_handle: set[str] = field(default_factory=set)
    has_handle_column: bool = False
    rows: int = 0


def resolve_in_folder(name: str, folder: Path) -> Path:
    root = folder.expanduser().resolve()
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise SheetOutsideFolderError(_display(root), Path(name).name or name)
    return candidate


def _display(folder: Path) -> str:
    """``/home/x/gecko-lists`` -> ``~/gecko-lists/``: what she would type."""
    home = Path.home().resolve()
    shown = (
        f"~/{folder.relative_to(home)}" if folder.is_relative_to(home) else str(folder)
    )
    return shown.rstrip("/") + "/"


def _column(headers: list[str], wanted: set[str], contains: str = "") -> int | None:
    for index, header in enumerate(headers):
        key = header.strip().lower().replace(" ", "_")
        if key in wanted or (contains and contains in key):
            return index
    return None


def read_rows(
    name: str, folder: Path | None = None
) -> tuple[list[str], list[list[str]]]:
    """Headers and non-blank rows of a CSV inside ``folder``; refuses anything outside it."""
    folder = folder if folder is not None else default_folder()
    path = resolve_in_folder(name, folder)
    if path.suffix.lower() != ".csv":
        raise SheetError(f"{path.name} is not a .csv file. Export the sheet as CSV.")
    if not path.is_file():
        raise SheetError(
            f"There is no {path.name} in {_display(path.parent)}. Put it there."
        )
    if path.stat().st_size > MAX_SHEET_BYTES:
        raise SheetError(f"{path.name} is over 5 MB. Keep only the columns needed.")
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    dialect: type[csv.Dialect] | csv.Dialect = csv.excel
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        pass
    reader = csv.reader(io.StringIO(text), dialect)
    headers = next(reader, None)
    if not headers:
        raise SheetError(f"{path.name} is empty.")
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    return headers, rows


def column(headers: list[str], wanted: set[str], contains: str = "") -> int | None:
    return _column(headers, wanted, contains)


def read_customers(name: str, folder: Path | None = None) -> CustomerSheet:
    headers, rows = read_rows(name, folder)
    handle_col = _column(headers, _HANDLE_HEADERS, contains="instagram")
    name_col = _column(headers, _NAME_HEADERS)
    sheet = CustomerSheet(has_handle_column=handle_col is not None)
    for row in rows:
        sheet.rows += 1
        handle = None
        if handle_col is not None and handle_col < len(row):
            handle = normalize_handle(row[handle_col])
        if handle is not None:
            sheet.handles.add(handle)
        elif name_col is not None and name_col < len(row) and row[name_col].strip():
            sheet.names_without_handle.add(normalize_name(row[name_col]))
    return sheet

"""A name check against OFAC's SDN list: public, free, and outside every catalog.

What this is: an exact and a normalized NAME match against one list. What it is not:
sanctions screening (no addresses, no ownership, no other lists, no fuzzy match). So a
hit is reported as "possible match" and a miss as "no match on the SDN list" — never
"sanctioned" and never "clear".

The URL is CONFIG (``SupplierDeps.sdn_url``). The default is the address OFAC's
Sanctions List Service publishes for the SDN XML export; it was confirmed from a sibling
repo's working fetcher (``jev-risk-check-provider/scripts/update-ofac.ts``, snapshot of
2026-10-02), not by fetching it from here. The live fetch goes through the repo's SSRF
guard (``netguard.validate_public_url`` + ``safe_get``) and must be https. The parsed
names are cached under ``~/.gecko/orthogonal/`` with the date, one download per day.
This is public reference data, not anyone's payload.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ...netguard import Resolver, UnsafeUrlError, safe_get, validate_public_url
from ..errors import ListJobError
from .text import exact_key, normalize_company

DEFAULT_SDN_URL = (
    "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
)
#: The XML export is ~30 MB; anything past this is not the file we expect.
MAX_SDN_BYTES = 64 * 1024 * 1024
#: Fewer entries than this and the file is not the SDN list (format change, error page).
MIN_ENTRIES = 1000
_MIN_KEY_LEN = 3

_ENTRY = re.compile(r"<sdnEntry>(.*?)</sdnEntry>", re.DOTALL)
_AKA = re.compile(r"<aka>(.*?)</aka>", re.DOTALL)
_PUBLISH = re.compile(r"<Publish_Date>(\d{2})/(\d{2})/(\d{4})</Publish_Date>")

#: url -> the document text. Live: ``live_fetch``. Recorded: the made-up fixture.
Fetch = Callable[[str], str]


class SanctionsListError(ListJobError):
    code = "sanctions_list_unavailable"


def _tag(block: str, name: str) -> str | None:
    match = re.search(rf"<{name}>(.*?)</{name}>", block, re.DOTALL)
    return html.unescape(match.group(1)).strip() if match else None


def _full_name(block: str) -> str | None:
    parts = [p for p in (_tag(block, "firstName"), _tag(block, "lastName")) if p]
    return " ".join(parts) if parts else None


@dataclass
class SdnList:
    source: str
    publish_date: str | None
    sha256: str
    fetched_on: str
    #: (uid, name, sdnType) — primary names and every a.k.a.
    names: list[tuple[str, str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.exact_index: dict[str, str] = {}
        self.normal_index: dict[str, str] = {}
        for _uid, name, _kind in self.names:
            self.exact_index.setdefault(exact_key(name), name)
            key = normalize_company(name)
            if len(key) >= _MIN_KEY_LEN:
                self.normal_index.setdefault(key, name)

    @property
    def label(self) -> str:
        return f"OFAC SDN list ({self.publish_date or 'date unknown'})"

    def entries(self) -> int:
        return len({uid for uid, _n, _k in self.names})

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass(frozen=True)
class SdnHit:
    """``kind`` is "exact" or "normalized"; ``listed_name`` is OFAC's spelling."""

    kind: str
    listed_name: str
    checked_name: str


def parse_sdn_xml(text: str, *, source: str, today: dt.date) -> SdnList:
    publish = _PUBLISH.search(text)
    names: list[tuple[str, str, str]] = []
    for match in _ENTRY.finditer(text):
        block = match.group(1)
        uid = _tag(block, "uid") or ""
        kind = _tag(block, "sdnType") or ""
        primary = _full_name(re.sub(r"<akaList>.*?</akaList>", "", block, flags=re.S))
        if primary:
            names.append((uid, primary, kind))
        for aka in _AKA.finditer(block):
            alias = _full_name(aka.group(1))
            if alias:
                names.append((uid, alias, kind))
    return SdnList(
        source=source,
        publish_date=(
            f"{publish.group(3)}-{publish.group(1)}-{publish.group(2)}"
            if publish
            else None
        ),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        fetched_on=today.isoformat(),
        names=names,
    )


def live_fetch(url: str, *, resolver: Resolver | None = None) -> str:  # wire edge
    """The SSRF-guarded download. Validated here AND re-validated on every redirect."""
    validate_public_url(url, resolver=resolver)
    return safe_get(url, max_bytes=MAX_SDN_BYTES, timeout=120, resolver=resolver)


def cache_path(cache_dir: Path, today: dt.date) -> Path:
    return cache_dir / f"ofac-sdn-{today.isoformat()}.json"


def _read_cache(path: Path) -> SdnList | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return SdnList(
            source=str(raw["source"]),
            publish_date=raw.get("publish_date"),
            sha256=str(raw["sha256"]),
            fetched_on=str(raw["fetched_on"]),
            names=[(str(u), str(n), str(k)) for u, n, k in raw["names"]],
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None  # a broken cache is re-downloaded, never trusted


def load_sdn(
    url: str,
    *,
    today: dt.date,
    fetch: Fetch,
    cache_dir: Path | None,
    min_entries: int = MIN_ENTRIES,
) -> SdnList:
    """Today's cached list, else one download. ``cache_dir=None`` keeps it in memory."""
    if urlsplit(url).scheme != "https":
        raise SanctionsListError("The SDN list URL must be https.")
    if cache_dir is not None:
        cached = _read_cache(cache_path(cache_dir, today))
        if cached is not None and cached.source == url:
            return cached
    try:
        text = fetch(url)
    except UnsafeUrlError as exc:
        raise SanctionsListError(f"The SDN list URL was refused: {exc}") from None
    except OSError:
        raise SanctionsListError("Could not download the SDN list.") from None
    sdn = parse_sdn_xml(text, source=url, today=today)
    if sdn.entries() < min_entries:
        raise SanctionsListError(
            f"The SDN download has {sdn.entries()} entries; expected at least "
            f"{min_entries}. Not using it."
        )
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path(cache_dir, today).write_text(sdn.to_json(), encoding="utf-8")
    return sdn


def screen(names: list[str], sdn: SdnList) -> SdnHit | None:
    """Exact (case/space-insensitive) first, then normalized (suffixes, accents off)."""
    for name in names:
        listed = sdn.exact_index.get(exact_key(name))
        if listed:
            return SdnHit("exact", listed, name)
    for name in names:
        key = normalize_company(name)
        if len(key) >= _MIN_KEY_LEN and key in sdn.normal_index:
            return SdnHit("normalized", sdn.normal_index[key], name)
    return None

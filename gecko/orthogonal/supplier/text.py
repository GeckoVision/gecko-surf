"""Untrusted upstream text, made safe to keep: sanitized, flattened, capped, CSV-safe.

News titles, trust-signal text and company names are written by people we do not
know. They are DATA for the person reading the table. They are never put in a tool
result (the MCP surface returns ``model_summary`` instead, which code builds from
counts and our own words), so a planted "ignore previous instructions" reaches a file,
not a model that holds tools.
"""

from __future__ import annotations

import re
import unicodedata

from ...sanitize import sanitize_text

#: The longest value summary the table keeps.
SUMMARY_CAP = 160
_SPACE = re.compile(r"\s+")
# Spreadsheet apps execute a cell that starts with one of these.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")
_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "llc",
        "llp",
        "lp",
        "plc",
        "sa",
        "ag",
        "nv",
        "bv",
        "gmbh",
        "srl",
        "spa",
        "pte",
        "pty",
        "the",
    }
)


def untrusted_summary(text: object, cap: int = SUMMARY_CAP) -> str:
    """One line of upstream text, run through the repo sanitizer and capped."""
    if not isinstance(text, str):
        return ""
    cleaned, _poisoned = sanitize_text(text)
    flat = "".join(
        ch if unicodedata.category(ch)[0] != "C" else " " for ch in str(cleaned)
    )
    flat = _SPACE.sub(" ", flat).strip()
    if len(flat) > cap:
        flat = flat[: cap - 1].rstrip() + "…"
    return flat


def csv_safe(value: str) -> str:
    """Neutralize a leading formula character so a spreadsheet shows, not runs, it."""
    return "'" + value if value.startswith(_FORMULA_LEAD) else value


def normalize_company(name: object) -> str:
    """``"Jabil Inc."`` -> ``"jabil"``: accents folded, punctuation and legal suffixes off."""
    if not isinstance(name, str):
        return ""
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).casefold()
    words = re.sub(r"[^a-z0-9]+", " ", folded).split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def exact_key(name: object) -> str:
    """Case- and whitespace-insensitive, nothing else: the "exact" sanctions match."""
    if not isinstance(name, str):
        return ""
    return _SPACE.sub(" ", name).strip().casefold()


def domain_root(domain: object) -> str:
    """``"flex.com"`` -> ``"flex"``: lets a parent NAME be compared with a parent DOMAIN."""
    if not isinstance(domain, str) or "." not in domain:
        return ""
    labels = domain.lower().strip(".").split(".")
    if labels and labels[0] == "www":
        labels = labels[1:]
    return labels[0] if labels else ""

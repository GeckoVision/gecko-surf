"""Contact details are PII: stored whole for the customer, masked when a tool shows them.

Invariant #1 lets a pipeline store what it produces for the one customer who asked, so an
email or a phone number a source returned is kept in full in that customer's own store.
What changes is the view: a tool answer lands in a chat transcript, a screen share or a
recording, so by default the views show ``er***@gmail.com`` and ``+** ** *****-4321``, and
the whole value only when the caller asks for it (``reveal_pii``).

A value is PII when its FIELD is a contact field (the planner names fields in free text,
so the name is matched, not looked up), or when the value itself is an email. Inside any
other text (a bio, a post) emails are masked always, and phone numbers only when written
in international form (``+55 ...``): a bare run of digits is as likely a date or a count.
"""

from __future__ import annotations

import re

__all__ = ["is_pii_field", "mask_value", "masked"]

_CONTACT_FIELD = re.compile(
    r"e_?mail|phone|mobile|cell|whats_?app|telephone|contact_number", re.I
)
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_INTL_PHONE = re.compile(r"\+\d[\d\s().-]{6,}\d")
_KEEP_DIGITS = 4


def is_pii_field(field: str) -> bool:
    return bool(_CONTACT_FIELD.search(field))


def _mask_email(match: re.Match[str]) -> str:
    return f"{match.group(1)[:2]}***@{match.group(2)}"


def _mask_digits(text: str) -> str:
    """Every digit but the last four becomes ``*``; separators stay, so the shape reads."""
    total = sum(ch.isdigit() for ch in text)
    out: list[str] = []
    seen = 0
    for ch in text:
        if ch.isdigit():
            seen += 1
            out.append(ch if seen > total - _KEEP_DIGITS else "*")
        else:
            out.append(ch)
    return "".join(out)


def mask_value(field: str, value: str) -> str:
    """``value`` with its contact details masked."""
    text = _EMAIL.sub(_mask_email, value)
    if is_pii_field(field) and not _EMAIL.search(value):
        return _mask_digits(text)
    return _INTL_PHONE.sub(lambda m: _mask_digits(m.group(0)), text)


def masked(field: str, value: str | None, *, reveal: bool) -> tuple[str | None, bool]:
    """``(value to show, whether anything was masked)``."""
    if value is None or reveal:
        return value, False
    shown = mask_value(field, value)
    return shown, shown != value

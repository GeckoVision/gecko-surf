"""What a person typed, reduced to one of five things Gecko can actually do.

A chat message is not a tool call, and the gap between them is where this module
lives. It is deliberately a small, legible grammar rather than a model: the surface
this feeds (``list_stores`` / ``prepare_purchase``) has exactly two verbs, so a
classifier with five outcomes can be read, tested, and — the part that matters —
FAIL VISIBLY. An unrecognised message becomes :data:`UNKNOWN` and is answered with a
refusal that names the shapes that do work, which is a correct answer. A parser that
guessed would instead spend somebody's blockhash window on a sentence it misread.

Pure and stateless: text in, an :class:`Intent` out. No I/O, no engine, no network,
nothing kept. The caller owns everything that touches a chain.

CONTROL PLANE (invariant #1): the text handed here is user data. It is read, reduced
to the few fields below, and never stored, logged, or emitted by this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "Intent",
    "IntentKind",
    "MAX_TEXT_CHARS",
    "UNKNOWN",
    "parse_intent",
]

#: How much of a message we will look at. A Telegram message can be 4096 characters;
#: none of the shapes below need more than a line, and capping before any regex runs
#: keeps a pathological message from becoming CPU on a public door.
MAX_TEXT_CHARS = 512

IntentKind = Literal["help", "browse", "product", "buy", "unknown"]


@dataclass(frozen=True)
class Intent:
    """One classified message. ``store``/``product``/``buyer`` are present only when
    the text actually named them — an absent field means "not said", never "any"."""

    kind: IntentKind
    store: str | None = None
    product: str | None = None
    buyer: str | None = None


UNKNOWN = Intent(kind="unknown")

#: A Solana address, by shape only. Base58 excludes 0/O/I/l, and a pubkey is 32-44
#: characters. Shape is all this module claims: whether the string is a real account
#: is a question for the chain, and ``prepare_purchase`` is the thing that asks it.
_BUYER_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

#: The verbs that mean "I want the bytes". Checked as a prefix, so "buy" inside a
#: sentence about something else does not trigger a purchase path.
_BUY_PREFIXES: tuple[str, ...] = (
    "/buy",
    "buy ",
    "order ",
    "purchase ",
    "i want to buy ",
    "i'd like to buy ",
)

#: "show me the menu". Substring-matched, because there is no harm in a browse: it
#: costs nothing, expires never, and is the step that should happen before a purchase.
_BROWSE_WORDS: tuple[str, ...] = (
    "list stores",
    "list the stores",
    "stores",
    "store list",
    "menu",
    "what do you sell",
    "what's for sale",
    "whats for sale",
    "what is for sale",
    "for sale",
    "catalog",
    "catalogue",
)

#: "tell me about one thing". Prefix-matched: the tail is the product.
_PRODUCT_PREFIXES: tuple[str, ...] = (
    "/price",
    "how much is ",
    "how much for ",
    "how much does ",
    "price of ",
    "price for ",
    "do you have ",
    "do you sell ",
    "is there ",
)

_HELP_WORDS: frozenset[str] = frozenset(
    {"/start", "/help", "help", "hi", "hello", "hey", "?"}
)

#: Filler a person puts around the thing they want, stripped from an extracted
#: product/store so "a coffee" and "coffee" reach the engine as the same filter.
_LEADING_FILLER: tuple[str, ...] = ("a ", "an ", "the ", "some ", "one ")

#: How the store is named in the shapes we accept. " from " is the common one; the
#: others were added because they are what people write, not because they are prettier.
_STORE_SEPARATORS: tuple[str, ...] = (" from ", " at ", " in store ")

#: What introduces the buyer's own address. The address is also recognised on its own
#: (see :data:`_BUYER_RE`) — these only exist so the words are removed with it.
_BUYER_SEPARATORS: tuple[str, ...] = (" for ", " to ", " as ", " wallet ", " buyer ")


def _clean(fragment: str) -> str | None:
    """A caller-named store/product, trimmed to what a filter can use, or ``None``.

    Strips quotes, trailing punctuation and leading articles. ``None`` for anything
    that reduces to nothing, so an empty filter is never mistaken for "everything".
    """
    text = fragment.strip().strip("\"'`").strip()
    text = text.rstrip(".,!?;:").strip()
    lowered = text.lower()
    for filler in _LEADING_FILLER:
        if lowered.startswith(filler):
            text = text[len(filler) :].strip()
            break
    return text or None


def _split_store(fragment: str) -> tuple[str | None, str | None]:
    """``(product, store)`` from the tail of a buy/product phrase.

    The separator is matched on a lowercased copy and cut from the ORIGINAL, so a
    store's own capitalisation survives into the filter it becomes.
    """
    lowered = fragment.lower()
    for separator in _STORE_SEPARATORS:
        index = lowered.find(separator)
        if index != -1:
            return (
                _clean(fragment[:index]),
                _clean(fragment[index + len(separator) :]),
            )
    return (_clean(fragment), None)


def _take_buyer(text: str) -> tuple[str, str | None]:
    """Pull the first address-shaped token out of ``text``.

    Returns the text with that token — and any word that introduced it — removed, so
    the remainder can be parsed for a product without the address inside it. Gecko
    holds no key, so the buyer can only ever come from the person typing.
    """
    match = _BUYER_RE.search(text)
    if match is None:
        return (text, None)
    buyer = match.group(0)
    head, tail = text[: match.start()], text[match.end() :]
    lowered = head.lower()
    for separator in _BUYER_SEPARATORS:
        if lowered.endswith(separator):
            head = head[: -len(separator)]
            break
    return ((head + " " + tail).strip(), buyer)


def parse_intent(text: str | None) -> Intent:
    """Classify one message. Never raises; anything unreadable is :data:`UNKNOWN`.

    The order is the priority: an explicit command beats a keyword, a buy verb beats
    a browse word ("buy a coffee from the coffee store" is not a request for a menu),
    and a browse word beats a product question (a menu is the cheaper answer).
    """
    if not isinstance(text, str):
        return UNKNOWN
    raw = text.strip()[:MAX_TEXT_CHARS]
    if not raw:
        return UNKNOWN
    lowered = raw.lower()

    if lowered in _HELP_WORDS or lowered.startswith("/help"):
        return Intent(kind="help")

    for prefix in _BUY_PREFIXES:
        if lowered.startswith(prefix):
            tail, buyer = _take_buyer(raw[len(prefix) :])
            product, store = _split_store(tail)
            return Intent(kind="buy", store=store, product=product, buyer=buyer)

    # A browse can still carry a filter: "stores selling water" is a browse for water.
    # The filter is looked for only AFTER the browse word, so the words that MADE it a
    # browse cannot be read as the filter ("what's for sale" is not a hunt for "sale").
    browse_end = _browse_word_end(lowered)
    if browse_end is not None:
        product, store = _split_store(_browse_filter(lowered[browse_end:]))
        return Intent(kind="browse", store=store, product=product)

    for prefix in _PRODUCT_PREFIXES:
        if lowered.startswith(prefix):
            product, store = _split_store(raw[len(prefix) :])
            if product is None and store is None:
                return UNKNOWN
            return Intent(kind="product", store=store, product=product)

    return UNKNOWN


#: Words a browse phrase wraps around its filter, e.g. "stores SELLING water".
_BROWSE_FILTER_SEPARATORS: tuple[str, ...] = (
    " selling ",
    " that sell ",
    " with ",
    " for ",
)


def _browse_word_end(lowered: str) -> int | None:
    """Where the browse phrase ends, or ``None`` when the text contains none.

    The EARLIEST match wins, and the longest one at that position: "what's for sale"
    must match "what's for sale" (leaving no tail) rather than "for sale" — otherwise
    the words that MADE it a browse get read back as a filter for "sale", which is a
    product nobody asked about.
    """
    matches = [
        (index, index + len(word))
        for word in _BROWSE_WORDS
        if (index := lowered.find(word)) != -1
    ]
    if not matches:
        return None
    earliest = min(index for index, _ in matches)
    return max(end for index, end in matches if index == earliest)


def _browse_filter(tail: str) -> str:
    """The filter after a browse phrase, or ``""`` when it names none.

    Runs on the lowercased text because a browse filter is case-insensitive at the
    engine anyway (``list_stores`` matches substrings without regard to case).
    """
    for separator in _BROWSE_FILTER_SEPARATORS:
        index = tail.find(separator)
        if index != -1:
            return tail[index + len(separator) :]
    return ""

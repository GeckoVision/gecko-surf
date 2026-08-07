"""Lexical normalization — the shared vocabulary layer under every lexical ranker.

Two mechanisms, both deterministic, both offline, no model and no vectors:

**Morphological folding.** A user asks "is this stablecoin *depegged*"; the surface
says *depeg* (in its path, its operationId and its summary). Exact-token overlap
scores that op **zero** — not "low", zero — because ``depegged != depeg``. Folding
maps a surface form to a stem and is applied SYMMETRICALLY to the index and the
query, so it is one-way safe: equal tokens stay equal under a function, therefore no
match that worked before can break; only new matches appear.

**The genericity floor.** Every English question carries ``is``/``this``/``it``/``if``.
Counting them as evidence lets the op with the most prose win any question — measured:
``delete_sub`` beat the depeg simulator on "is this stablecoin depegged" with ``is`` +
``this``. Function words are dropped; a query left with nothing is reported as carrying
no intent, which is not silence — the catalog's never-empty prior still returns
candidates, honestly flagged below the confidence floor.

Both are conservative on purpose. A folder's real risk is over-stemming — a FALSE merge
routes confidently to the wrong op — so the rules below refuse to fold short stems,
non-alphabetic tokens, and the ``-ss``/``-us``/``-is`` endings that are not plurals.
"""

from __future__ import annotations

from functools import lru_cache

__all__ = [
    "STOPWORDS",
    "content_tokens",
    "fold_token",
    "fold_tokens",
    "normalize_query",
]

# Function words carry no discriminating power over an API surface. Single source of
# truth: :mod:`gecko.find_start` used to declare its own copy and the catalog scorer had
# none at all, which is exactly how function words came to decide a ranking.
STOPWORDS = frozenset(
    "a an and are as at be by can do does for from get give had has have how i if in "
    "into is it me much my of on or our please should some that the then this to want "
    "was we what when where which who why will with would you your".split()
)

# Doubled endings that are part of the stem, not a doubling introduced by the suffix:
# `pass`/`fill`/`buzz`. Undoubling these would corrupt the stem.
_KEEP_DOUBLE = frozenset({"ll", "ss", "zz", "ff"})

_VOWELS = frozenset("aeiouy")

# A stem must survive with this many characters (and a vowel) before a suffix is
# stripped. Below it the "stem" is not a word: `feed` -> `fe`, `used` -> `us`.
_MIN_STEM = 3


def _has_vowel(text: str) -> bool:
    return any(c in _VOWELS for c in text)


def _viable(stem: str) -> bool:
    return len(stem) >= _MIN_STEM and _has_vowel(stem)


def _undouble(stem: str) -> str:
    """`depegged` -> (strip `ed`) `depegg` -> `depeg`. The consonant was doubled by the
    suffix, so it comes off with it — otherwise the -ed form never rejoins its stem."""
    if (
        len(stem) >= _MIN_STEM
        and stem[-1] == stem[-2]
        and stem[-2:] not in _KEEP_DOUBLE
    ):
        return stem[:-1]
    return stem


# Memoized because scoring folds every surface token of every op on every query, and an
# API's vocabulary is small and fixed. Memoizing the pure FUNCTION (rather than caching
# folded sets on a catalog entry) keeps the ranker stateless: nothing can serve a stale
# vocabulary when a caller swaps the tokenizer underneath it, as the arms harness does.
@lru_cache(maxsize=1 << 16)
def fold_token(token: str) -> str:
    """Map one token to its folded form. Deterministic, idempotent, and total.

    Idempotence is a correctness requirement, not a nicety: the index is folded at score
    time while a caller (``find_start``) may hand in already-normalized query tokens, so
    ``fold(fold(x))`` must equal ``fold(x)`` or the two sides stop agreeing.
    """
    if len(token) <= _MIN_STEM or not token.isalpha():
        return token  # digits, versions, mints, and short words are left alone

    # -ing / -ed first: they can also carry a doubled consonant (`depegged`).
    for suffix in ("ing", "ed"):
        if token.endswith(suffix):
            stem = token[: -len(suffix)]
            return _undouble(stem) if _viable(stem) else token

    if token.endswith("ies") and len(token) >= 5:  # deliveries -> delivery
        token = token[:-3] + "y"
    elif token.endswith(("sses", "shes", "ches", "xes", "zes")):  # matches -> match
        token = token[:-2]
    elif token.endswith("s") and not token.endswith(("ss", "us", "is")):
        # `status`/`address`/`analysis` are not plurals; `assets`/`alerts`/`depegs` are.
        stem = token[:-1]
        if _viable(stem):
            token = stem

    # Derivational tail: the noun and the verb of the same action must meet, or
    # "liquidated" (question) and "liquidation" (summary) stay strangers.
    if token.endswith("ation") and len(token) >= 7:
        return token[:-3]  # simulation -> simulat
    if token.endswith("ate") and len(token) >= 6:
        return token[:-1]  # simulate -> simulat
    return token


def fold_tokens(tokens: set[str]) -> set[str]:
    return {fold_token(t) for t in tokens}


# Folding a function word can disguise it (`does` -> `doe`), which would smuggle it back
# past the floor. Filter on both the surface and the folded form.
_FOLDED_STOPWORDS = STOPWORDS | frozenset(fold_token(w) for w in STOPWORDS)


def content_tokens(tokens: set[str]) -> set[str]:
    """Drop function words. Returns EMPTY when the query was nothing but function words —
    "please do it for me" carries no intent and must not be answered as if it did."""
    return (tokens - STOPWORDS) - _FOLDED_STOPWORDS


def normalize_query(tokens: set[str]) -> set[str]:
    """The one query normalization every lexical ranker shares: fold, then floor.

    Idempotent (both halves are), so a caller that has already normalized — or has
    already dropped its own stopwords, as ``find_start`` does — gets the same set back
    and the index and query sides can never end up on different vocabularies.

    An empty result is INFORMATION, not a failure: the catalog's never-empty prior still
    answers a contentless query (flagged ``is_fallback``, score 0 — visible but honestly
    below the floor), and ``find_start`` reports "no content-bearing terms" rather than
    routing on ``is``/``this``. What must never happen is the pre-fix behaviour, where
    function words scored like evidence and PICKED the endpoint.
    """
    return content_tokens(fold_tokens(content_tokens(tokens)))

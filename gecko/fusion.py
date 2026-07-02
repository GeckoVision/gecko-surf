"""Rank-based fusion of the lexical and dense retrieval arms (S1).

Reciprocal Rank Fusion (``Σ 1/(k + rank)``) over ranked lists joined on ``tool_name`` —
NOT a weighted-score sum over the two arms' incomparable scales. ``k`` is a pre-registered
hyperparameter (the CE-reference default), never tuned on the golden set.

The out-of-scope confidence floor lives in ``client`` (where ``is_fallback`` is set), NOT
here, and is anchored to the LEXICAL arm — see that code + the results write-up. Rationale
learned from the data: the dense arm always returns a nearest neighbour, and (measured on
``voyage-4-lite``) its cosine scores are compressed into a ~0.005-wide band across the whole
pool, so neither an absolute nor a relative-margin dense score separates an out-of-scope
intent from a genuine paraphrase. A dense-score floor is therefore the wrong tool; the
reliable in-scope signal at this scale is lexical corroboration.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

# Pre-registered RRF constant (CE-reference default). NOT tuned on the golden set — the set
# is go/no-go only.
RRF_K = 60


def rrf_fuse(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    """Fuse ranked name-lists into ``{name: rrf_score}``. Each list contributes ``1/(k+rank)``
    (rank 1-based) for every name it ranks; absent names simply contribute nothing."""
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, name in enumerate(ranking, start=1):
            scores[name] += 1.0 / (k + rank)
    return dict(scores)

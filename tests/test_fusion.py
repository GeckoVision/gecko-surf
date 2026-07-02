"""RRF fusion — rank-based, join on tool_name. (The OOS confidence floor is lexical-anchored
in ``client``, not a dense-score threshold — see ``fusion``/``client`` docstrings.)"""

from __future__ import annotations

from gecko.fusion import RRF_K, rrf_fuse


def test_rrf_rewards_agreement_across_arms():
    # A name ranked high by BOTH arms beats one ranked high by only one.
    lex = ["a", "b", "c"]
    dense = ["b", "a", "d"]
    fused = rrf_fuse([lex, dense], k=RRF_K)
    ranked = sorted(fused, key=lambda n: -fused[n])
    assert ranked[0] in {"a", "b"}
    # 'a' (ranks 1,2) and 'b' (ranks 2,1) both beat 'c' (only one arm) and 'd' (only one arm).
    assert fused["a"] > fused["c"] and fused["b"] > fused["d"]


def test_rrf_uses_reciprocal_rank_not_score():
    # Only rank position matters — a name present in one list gets 1/(k+rank).
    fused = rrf_fuse([["x", "y"]], k=60)
    assert abs(fused["x"] - 1 / 61) < 1e-12
    assert abs(fused["y"] - 1 / 62) < 1e-12


def test_rrf_empty_arms():
    assert rrf_fuse([[], []]) == {}


def test_rrf_dense_only_name_still_ranked():
    # A name only the dense arm surfaces (no lexical overlap — the paraphrase case) still
    # earns an RRF contribution, so dense can lift it into the fused top-k.
    fused = rrf_fuse([["lex1", "lex2"], ["dense_only", "lex1"]], k=60)
    assert "dense_only" in fused and fused["dense_only"] == 1 / 61

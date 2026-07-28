"""VAS-5 — the offline-falsifiable Privy verify demo (the flagship REFUTED-badge frame).

A Privy-flavoured CLAIMED surface (from-docs draft) carries two REAL passwordless ops and
one endpoint a docs source hallucinated. Replayed against a PRE-CAPTURED observed-outcome
corpus (a stand-in for a prior ``--live`` run), the fabricated op comes back REFUTED
(``replay:404``) and the real ops VERIFIED — all offline, no network (Pattern B).

Honesty is the whole point: the REFUTED verdict traces to a status that really came off the
wire (``source == "observed"``, 404). The demo replays that observed outcome; it never
synthesizes a 404. The last test pins that gate — the SAME surface in plain recorded mode
(no observed corpus) is all UNVERIFIED, never REFUTED-from-synthesis.
"""

from __future__ import annotations

import json
from pathlib import Path

from gecko import report, verify
from gecko.access import NoAuthSession
from gecko.client import AgentApiClient

_FIXTURES = Path(__file__).parent / "fixtures"
_PRIVY_SPEC = _FIXTURES / "privy_claimed_openapi.json"
_OBSERVED_CORPUS = _FIXTURES / "privy_observed_corpus.jsonl"

_REAL_OPS = ("initPasswordless", "authenticatePasswordless")
_FABRICATED_OP = "loginWithEmail"


def _verify_section(html: str) -> str:
    """Isolate the verified-against-reality section (the verdict tier). The rest of the
    scorecard — e.g. the Playground — legitimately echoes surface text (op summaries,
    synthesized sample args); the verdict tier is where a response payload would leak."""
    start = html.index('<section class="card verify">')
    return html[start : html.index("</section>", start)]


def _client() -> AgentApiClient:
    # NoAuthSession: nothing to inject, so the surface never degrades and the demo is $0.
    return AgentApiClient(str(_PRIVY_SPEC), session=NoAuthSession())


def test_fabricated_op_refuted_real_ops_verified_offline() -> None:
    """The flagship assertion: the hallucinated endpoint is REFUTED from an observed 404,
    the real ops VERIFIED from observed 200s — no network touched."""
    observed = verify.load_observed_corpus(_OBSERVED_CORPUS)
    client = _client()

    result = verify.verify_docs(client, outcomes=observed)
    reported = result["report"]

    assert reported[_FABRICATED_OP]["status"] == "REFUTED"
    # the refutation NAMES the observed 404 — honesty intact, no body.
    assert reported[_FABRICATED_OP]["basis"] == ["replay:404"]
    for op_id in _REAL_OPS:
        assert reported[op_id]["status"] == "VERIFIED"
        assert reported[op_id]["basis"] == ["replay:200"]
    assert result["summary"] == {"verified": 2, "refuted": 1, "unverified": 0}


def test_refuted_traces_to_an_observed_wire_status() -> None:
    """The honesty invariant, made explicit: the outcome that earns REFUTED is
    ``source == "observed"`` with a 404 that came off the wire — not synthesized."""
    observed = verify.load_observed_corpus(_OBSERVED_CORPUS)
    fabricated = observed[_FABRICATED_OP]
    assert fabricated.source == "observed"
    assert fabricated.status == 404


def test_observed_corpus_is_control_plane_clean() -> None:
    """The corpus carries status + op id + shape metadata only — never a body/secret or an
    arg VALUE (invariant #1). Every row is on the §1 allowlist, and only the body-SENT
    boolean is recorded — never the email/password the caller actually supplied."""
    from gecko.corpus import ALLOWED_KEYS

    for line in _OBSERVED_CORPUS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        assert set(record) <= ALLOWED_KEYS  # no non-allowlisted (body-shaped) key
        # a body was sent, but its VALUES (the email/password) are absent by construction.
        assert record["body_present"] is True
        assert record["params_present"] == []
        assert record["arg_shape"] == {}


def test_scorecard_renders_red_refuted_badge_offline() -> None:
    """``build_scorecard(..., verify=True, verify_outcomes=...)`` renders the red REFUTED
    badge and the fabricated op's ``METHOD /path`` — offline, control-plane clean."""
    observed = verify.load_observed_corpus(_OBSERVED_CORPUS)

    html = report.build_scorecard(
        str(_PRIVY_SPEC), verify=True, verify_outcomes=observed
    )
    section = _verify_section(html)

    # the red REFUTED badge (v-refuted is the one strong-red accent) for the fabricated op.
    assert "v-refuted" in section
    assert ">REFUTED<" in section
    assert "POST" in section and "/api/v1/auth/email/login" in section
    # the real ops render VERIFIED.
    assert ">VERIFIED<" in section
    # control plane: the verdict tier renders status + basis + provenance + the templated
    # METHOD/path only — never a response body value or a synthesized sample arg.
    assert "sample" not in section
    assert "hallucinated" not in section.lower()


def test_observed_gate_intact_recorded_mode_is_all_unverified() -> None:
    """The gate that must NOT weaken: the SAME surface with no observed corpus (plain
    recorded mode) synthesizes a 200 for every op — and each stays honestly UNVERIFIED.
    A recorded run can never mint REFUTED (or VERIFIED) from a fabricated status."""
    client = _client()

    result = verify.verify_docs(client, mode="recorded")

    statuses = {op_id: entry["status"] for op_id, entry in result["report"].items()}
    assert set(statuses.values()) == {"UNVERIFIED"}
    assert result["summary"] == {"verified": 0, "refuted": 0, "unverified": 3}
    # the fabricated op is NOT refuted here — REFUTED only ever comes from an observed wire.
    assert statuses[_FABRICATED_OP] == "UNVERIFIED"

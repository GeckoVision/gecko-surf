"""TxODDS settlement agent (Phase B) — the off-chain half of the on-chain-trust demo.

It embodies BOTH theses in one loop:
  1. Comprehension — Gecko turns TxLINE into first-call-correct tools; the agent watches
     a fixture's scores and fetches the 3-stage Merkle proof, first try.
  2. Security gateway — EVERY TxLINE call is risk-scored by ``gecko.risk`` before it runs;
     a poisoned/malformed/anomalous call is blocked, and the audit trail is the demo's
     "kill feed".

It then maps the TxLINE proof onto the on-chain ``validate_stat`` arguments and returns a
``SettlePlan`` — the exact payload a thin (founder-gated, devnet) submit step feeds to the
``gecko-settlement`` program, which CPIs ``txoracle::validate_stat`` to settle trustlessly.

Offline / $0 by default (``recorded`` mode synthesises from the schema — matches end after
the bounty deadline, so this is the right mode). ``live`` swaps only the transport edge.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gecko.access import stub_session  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.risk import RiskAssessment, assess_from_client  # noqa: E402

SPEC = str(_REPO_ROOT / "tests" / "fixtures" / "txodds_docs.yaml")

_SCORES_SNAPSHOT = "getApiScoresSnapshotFixtureid"
_STAT_VALIDATION = "getApiScoresStat-validation"


@dataclass(frozen=True)
class TraderPredicate:
    """What a market resolves on, e.g. threshold=2, comparison='GreaterThan' → stat > 2."""

    threshold: int
    comparison: str = "GreaterThan"


@dataclass(frozen=True)
class SettlePlan:
    """The on-chain ``validate_stat`` args, mapped from the TxLINE 3-stage Merkle proof.

    A thin devnet submit step serialises this into the ``gecko-settlement::settle``
    instruction (which CPIs ``txoracle::validate_stat``). Producing it correctly IS the
    Gecko value — the agent drove a painful, auth-gated, on-chain-anchored API first try.
    """

    ts: int
    fixture_summary: Any
    fixture_proof: Any
    main_tree_proof: Any
    predicate: TraderPredicate
    stat_a: dict[str, Any]  # StatTerm
    stat_b: dict[str, Any] | None = None


@dataclass(frozen=True)
class GuardedCall:
    """One risk-scored call — the security-gateway audit record (the kill feed)."""

    tool: str
    args: dict[str, Any]
    risk: RiskAssessment


class BlockedCall(Exception):
    def __init__(self, tool: str, risk: RiskAssessment) -> None:
        super().__init__(
            f"blocked {tool}: {risk.reasons[0].message if risk.reasons else 'high risk'}"
        )
        self.tool = tool
        self.risk = risk


class SettlementAgent:
    def __init__(self, spec: str = SPEC, *, mode: str = "recorded") -> None:
        # stub_session presents the two-token TxLINE auth (no real token) so the
        # auth-gated ops are usable; live mode swaps in a real access.Session.
        self._client = AgentApiClient(spec, session=stub_session())
        self._mode = mode
        self.audit: list[GuardedCall] = []

    @property
    def tools(self) -> int:
        return len(self._client.list_tools())

    def _guarded(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        # THE SECURITY GATEWAY: score every call before it executes.
        risk = assess_from_client(self._client, tool, args)
        self.audit.append(GuardedCall(tool, args, risk))
        if risk.decision == "block":
            raise BlockedCall(tool, risk)
        return self._client.call(tool, args, mode=self._mode)

    def watch_scores(self, fixture_id: int) -> dict[str, Any]:
        return self._guarded(_SCORES_SNAPSHOT, {"fixtureId": fixture_id})

    def fetch_proof(
        self, fixture_id: int, seq: int, stat_key: int, stat_key2: int | None = None
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "fixtureId": fixture_id,
            "seq": seq,
            "statKey": stat_key,
        }
        if stat_key2 is not None:
            args["statKey2"] = stat_key2
        return self._guarded(_STAT_VALIDATION, args)

    @staticmethod
    def build_settle_plan(
        proof: dict[str, Any], predicate: TraderPredicate
    ) -> SettlePlan:
        d = proof.get("data") or {}
        stat_a = {
            "stat_to_prove": d.get("statToProve"),
            "event_stat_root": d.get("eventStatRoot"),
            "stat_proof": d.get("statProof") or [],
        }
        stat_b = None
        if d.get("statToProve2"):
            stat_b = {
                "stat_to_prove": d.get("statToProve2"),
                "event_stat_root": d.get("eventStatRoot"),
                "stat_proof": d.get("statProof2") or [],
            }
        return SettlePlan(
            ts=d.get("ts", 0),
            fixture_summary=d.get("summary"),
            fixture_proof=d.get("subTreeProof"),
            main_tree_proof=d.get("mainTreeProof"),
            predicate=predicate,
            stat_a=stat_a,
            stat_b=stat_b,
        )

    def settle(
        self, fixture_id: int, seq: int, stat_key: int, predicate: TraderPredicate
    ) -> SettlePlan:
        """Full off-chain flow: watch → prove (both risk-scored) → build the settle args."""
        self.watch_scores(fixture_id)
        proof = self.fetch_proof(fixture_id, seq, stat_key)
        return self.build_settle_plan(proof, predicate)

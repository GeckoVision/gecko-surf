"""Jito: the painful-API showcase — human-only docs in, first-call-correct tools out.

Jito's Block Engine (bundles / low-latency sends / tips) publishes NO OpenAPI, no
llms.txt, nothing machine-readable: the whole API lives in one ~93KB human doc page
(docs.jito.wtf/lowlatencytxnsend/) that even 403s default script user-agents. It's
JSON-RPC (method-in-body), auth is an optional rate-limit UUID header, and the real
routes are per-method — everything an agent has to reverse-engineer from prose.

The Gecko path this demo reproduces, offline / $0:
    1. `gecko from-docs <url>` recovered a draft OpenAPI from the prose (all 5 methods).
    2. A human review pass (this spec) confirmed the real routes from Jito's own curl
       examples, pinned the JSON-RPC envelopes, and encoded the gotchas (max-5 bundle,
       min 1000-lamport tip, base64-vs-base58 encoding, tip-in-same-tx).
    3. The engine comprehends it into question-shaped tools an agent picks by intent —
       and every call below is falsified in recorded mode before a lamport moves.

    uv run python examples/jito_demo/demo.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gecko.access import public_session  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.evaluate import evaluate_tasks  # noqa: E402

SPEC = str(Path(__file__).resolve().parent / "spec" / "jito_openapi.json")

# Representative agent goals. Each: a natural-language intent + the operation a
# correct agent must land on + the args a correct first call carries. The bundle
# flow (tips → send → track → confirm) is the whole consumer journey.
TASKS: list[dict[str, Any]] = [
    {
        "goal": "send an atomic bundle of signed transactions",
        "expect_op": "sendBundle",
        "args": {
            "body": {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendBundle",
                "params": [["dGVzdFR4MQ==", "dGVzdFR4Mg=="], {"encoding": "base64"}],
            }
        },
    },
    {
        "goal": "which accounts do I tip for my bundle to be accepted",
        "expect_op": "getTipAccounts",
        "args": {
            "body": {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTipAccounts",
                "params": [],
            }
        },
    },
    {
        "goal": "did my bundle land on chain and in which slot",
        "expect_op": "getBundleStatuses",
        "args": {
            "body": {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBundleStatuses",
                "params": [["b31e5fae4923f345218403ac1ab242b46a72d4f2a38d131f47"]],
            }
        },
    },
    {
        "goal": "track my submitted bundle while it is still in flight",
        "expect_op": "getInflightBundleStatuses",
        "args": {
            "body": {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getInflightBundleStatuses",
                "params": [["b31e5fae4923f345218403ac1ab242b46a72d4f2a38d131f47"]],
            }
        },
    },
    {
        "goal": "send one transaction with low latency",
        "expect_op": "sendTransaction",
        "args": {
            "body": {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": ["dGVzdFR4MQ==", {"encoding": "base64"}],
            }
        },
    },
]


@dataclass(frozen=True)
class DemoReport:
    """Everything the render needs — computed live, so the numbers can't drift."""

    ops_total: int
    surfaced: int
    card: dict[str, Any]
    bundle_url: str
    bundle_method: str
    tips_top1: str
    inflight_top1: str


def build_report() -> DemoReport:
    """Comprehend the reviewed Jito surface and score it. Pure/offline: no network,
    no keys, no lamports — recorded mode synthesizes responses from the schemas."""
    client = AgentApiClient(SPEC, session=public_session())
    surfaced = client.list_tools()

    card = evaluate_tasks(client, TASKS)

    # The wire-level proof: the prepared sendBundle call hits Jito's REAL route with
    # the JSON-RPC envelope in the body — the part an agent reading prose gets wrong.
    sent = client.call("sendBundle", TASKS[0]["args"], mode="recorded")

    return DemoReport(
        ops_total=len(client.operations),
        surfaced=len(surfaced),
        card=card,
        bundle_url=str(sent["request"]),
        bundle_method=str(sent["method"]),
        tips_top1=client.search("which accounts do I tip", limit=1)[0]["name"],
        inflight_top1=client.search(
            "track my bundle while it is still in flight", limit=1
        )[0]["name"],
    )


def main() -> None:
    r = build_report()
    print("Jito Block Engine — comprehended by Gecko (offline, $0)")
    print("=" * 60)
    print(f"human-only docs -> {r.ops_total} operations -> {r.surfaced} agent tools")
    print(f"first-call-correct scorecard: {r.card}")
    print(f"sendBundle wire target: {r.bundle_method} {r.bundle_url}")
    print(f"'which accounts do I tip'      -> {r.tips_top1}")
    print(f"'track my in-flight bundle'    -> {r.inflight_top1}")
    print()
    print("No OpenAPI, no llms.txt, a 403 for scripts — and the agent still calls it")
    print("right the first time. That's the comprehension layer.")


if __name__ == "__main__":
    main()

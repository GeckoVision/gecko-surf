"""Surfpool-for-APIs: verify a payment *charge* is first-call-correct, offline, $0.

Surfpool forks the chain to test your program; Gecko comprehends + simulates the API
your program calls. Here the API is a payment API and the test is: is the charge call
correct — right unit (integer centavos), required idempotency key present, bearer auth
injected — verified in **recorded** mode before a single cent (or live call) moves.

Engine-only: needs nothing but ``gecko`` (no anthropic/telegram). Runs fully offline.

    uv run pytest examples/charge_before_live/ -q
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from gecko import AgentApiClient, testgen
from gecko.caller import CallError
from gecko.sample import example_from_schema

# Absolute path so the test is correct regardless of the caller's working directory.
SPEC_PATH = str(Path(__file__).parent / "spec" / "payapi_openapi.json")

CHARGE_TOOL = "createCharge"


class _StubBearerSession:
    """A stub bearer session — the documented adapter seam (``auth_headers()``).

    Models the real API's single ``bearerAuth`` scheme with a throwaway token: no
    real secret, and the token is injected by the access layer at call time, never
    surfaced to the agent (architecture invariant #4). A session WITH auth is also
    what makes the bearer-gated charge/transfer tools visible to the agent at all.
    """

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer STUB_TEST_TOKEN_not_a_real_secret"}


def _client() -> AgentApiClient:
    return AgentApiClient(SPEC_PATH, session=_StubBearerSession())


def _charge_args() -> dict[str, Any]:
    """Synthesized, schema-valid args for the charge tool (recorded mode)."""
    client = _client()
    tool = next(t for t in client.list_tools() if t["name"] == CHARGE_TOOL)
    args = example_from_schema(tool["inputSchema"])
    assert isinstance(args, dict)
    return args


def test_charge_tool_discovered_by_intent() -> None:
    """An agent asking to 'charge a customer' lands on the charge endpoint."""
    hits = _client().search("charge a customer")
    names = [h["name"] for h in hits]
    assert CHARGE_TOOL in names, f"charge tool not discovered; got {names}"
    # Lexical catalog ranks the charge op first for this intent.
    assert names[0] == CHARGE_TOOL, f"expected {CHARGE_TOOL} ranked first; got {names}"


def test_auth_is_hidden_and_unit_is_surfaced() -> None:
    """Comprehension: auth is invisible to the agent; the centavos unit is surfaced."""
    tool = next(t for t in _client().list_tools() if t["name"] == CHARGE_TOOL)
    props = tool["inputSchema"]["properties"]

    # Auth is gated but injected for the agent — never an agent-facing input.
    assert tool["requires_auth"] is True
    assert "bearerAuth" in tool["auth_schemes"]
    assert "Authorization" not in props and "authorization" not in props

    # The required idempotency key and body are genuinely required.
    assert set(tool["inputSchema"]["required"]) == {"Idempotency-Key", "body"}
    assert set(props["body"]["required"]) == {"amount", "currency"}

    # The unit trap is surfaced structurally: amount is an integer (centavos).
    assert props["body"]["properties"]["amount"]["type"] == "integer"


def test_recorded_charge_is_well_formed() -> None:
    """Synthesized valid args -> a well-formed first call (2xx + data), $0 offline."""
    resp = _client().call(CHARGE_TOOL, _charge_args(), mode="recorded")

    assert resp["mode"] == "recorded"  # no network, no spend
    assert resp["status"] in (200, 201)
    data = resp["data"]
    assert isinstance(data, dict)
    assert "id" in data and "status" in data
    # The synthesized charge carries the unit correctly: integer centavos.
    assert isinstance(data["amount"], int)


def test_missing_idempotency_key_is_caught_locally() -> None:
    """Dropping the required idempotency key is caught before any wire call."""
    args = _charge_args()
    bad = {k: v for k, v in args.items() if k != "Idempotency-Key"}
    with pytest.raises(CallError) as exc:
        _client().call(CHARGE_TOOL, bad, mode="recorded")
    assert "Idempotency-Key" in str(exc.value)


def test_missing_amount_is_caught_locally() -> None:
    """Dropping the required amount (in the body) is caught before any wire call."""
    bad = copy.deepcopy(_charge_args())
    bad["body"].pop("amount")
    with pytest.raises(CallError) as exc:
        _client().call(CHARGE_TOOL, bad, mode="recorded")
    assert "amount" in str(exc.value)


def test_testgen_every_check_passes() -> None:
    """The shipped generator's first-call-correctness checks all pass (recorded)."""
    results = testgen.check(SPEC_PATH, session=_StubBearerSession())
    passed, total = testgen.summary(results)
    failed = [(r.tool, r.kind, r.detail) for r in results if not r.ok]
    assert not failed, f"unexpected failures: {failed}"
    # 2 usable tools x (well-formed + required-guard) = 4 checks.
    assert (passed, total) == (4, 4)

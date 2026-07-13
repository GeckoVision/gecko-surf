"""Task 3.2 — the multi-step correlation proof: gate (c) STATE across a session.

The frontier claim: on ONE ``SimWorld``/session, a deposit then two withdrawals
correlate — the second withdrawal fails BECAUSE the first debited the balance. The
rule is auto-derived from ``risk`` (verb + amount shape), so adding API #2 needs zero
new sandbox code (invariant #2). Nothing here touches a real API or an LLM; the
balances are fabricated ``Decimal``s under opaque keys (invariant #1).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from gecko.ingest import extract_operations
from gecko.sandbox import SimStore, evaluate

SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Wallet", "version": "1"},
    "paths": {
        "/deposit": {
            "post": {
                "operationId": "createDeposit",
                "summary": "Deposit funds.",
                "parameters": [
                    {
                        "name": "amount",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "number"},
                    },
                    {
                        "name": "account",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"balance": {"type": "number"}},
                                }
                            }
                        }
                    }
                },
            }
        },
        "/withdraw": {
            "post": {
                "operationId": "createWithdraw",
                "summary": "Withdraw funds.",
                "parameters": [
                    {
                        "name": "amount",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "number"},
                    }
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"balance": {"type": "number"}},
                                }
                            }
                        }
                    },
                    "422": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "error_code": {"type": "string"},
                                        "detail": {"type": "string"},
                                    },
                                    "required": ["error_code"],
                                }
                            }
                        }
                    },
                },
            }
        },
    },
}

_OPS = {op.operation_id: op for op in extract_operations(SPEC)}


def test_deposit_then_two_withdrawals_correlate_on_one_session() -> None:
    store = SimStore()
    world = store.get_or_create("sess-1", now=1.0)

    r1 = evaluate(_OPS["createDeposit"], {"amount": 100}, world=world)
    assert r1.status == 200
    assert world.balances["self"] == Decimal("100")

    r2 = evaluate(_OPS["createWithdraw"], {"amount": 150}, world=world)
    assert r2.status == 422
    assert "state.insufficient" in r2.signals
    assert "state.insufficient" in r2.remediation
    # answered with THIS API's own declared 422 error shape
    assert r2.data["error_code"] == "sample"
    assert world.balances["self"] == Decimal("100")  # unchanged after the failed debit

    r3 = evaluate(_OPS["createWithdraw"], {"amount": 80}, world=world)
    assert r3.status == 200
    assert world.balances["self"] == Decimal("20")


def test_state_gate_is_isolated_per_session_in_evaluate() -> None:
    store = SimStore()
    w1 = store.get_or_create("s1", now=1.0)
    w2 = store.get_or_create("s2", now=1.0)
    evaluate(_OPS["createDeposit"], {"amount": 50}, world=w1)
    assert w1.balances["self"] == Decimal("50")
    assert "self" not in w2.balances  # the other session never saw the deposit


def test_no_world_leaves_evaluate_stateless() -> None:
    # Without a SimWorld the state gate is a no-op — a well-formed call still 200s.
    r = evaluate(_OPS["createWithdraw"], {"amount": 999}, world=None)
    assert r.status == 200
    assert r.signals == []


def test_balance_keys_are_opaque_hashes_not_raw_recipients() -> None:
    # A recipient-bearing call buckets under a hash of the account, never the raw string.
    store = SimStore()
    world = store.get_or_create("sess", now=1.0)
    secret_acct = "acct-super-secret-1234"
    evaluate(
        _OPS["createDeposit"],
        {"amount": 10, "account": secret_acct},
        world=world,
    )
    assert secret_acct not in world.balances
    assert "self" not in world.balances  # a recipient WAS present -> non-self bucket
    assert secret_acct not in "".join(world.balances)

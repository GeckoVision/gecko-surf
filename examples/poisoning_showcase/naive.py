"""A naive OpenAPI -> tool baseline, for CONTRAST only (test-only, never shipped).

This is what a common "just turn the OpenAPI into MCP tools" pipeline does, and what
Gecko must beat: it **trusts the spec**. Each helper is the vulnerable path a poisoned
spec is designed to exploit; the tests assert Gecko does NOT do these things.

Single source of truth: the naive baseline now lives in the package at
``gecko.redteam._naive`` (the battle-test harness reuses it as its ``defenses=none`` arm).
This module re-exports it so the showcase keeps its ``naive.*`` call sites and there is
never a second copy drifting from the harness's.
"""

from __future__ import annotations

from gecko.redteam._naive import (
    naive_auth_host,
    naive_description,
    naive_input_schema,
    naive_query_auth_url,
)

__all__ = [
    "naive_auth_host",
    "naive_description",
    "naive_input_schema",
    "naive_query_auth_url",
]

"""Phase 4: the surfpool $0 derive→verify harness.

Offline (Pattern B): the verify logic is driven with an injected RPC — a recovered
recipe deriving to an address owned by the program passes; a wrong/missing account
fails. The real on-chain gate against a live surfpool mainnet fork is env-gated
(GECKO_SURFPOOL_E2E=1) so the default suite stays offline and $0.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from gecko.pda import ConstantPdaSeedNode, PdaNode
from gecko.pda_extract import from_source
from gecko.pda_testkit import (
    SurfpoolError,
    SurfpoolFork,
    verify_derivation,
)
from tests.test_pda_extract import ORE_PROGRAM, ORE_SOURCE

ORE_CONFIG = "9c9X7aDRAF41faiDs94ELjT19UrGnn72wBW9hPsS4Awy"

CONFIG_NODE = PdaNode(
    name="config",
    seeds=(ConstantPdaSeedNode(b"config", encoding="utf8"),),
    program_id=ORE_PROGRAM,
)


def _fake_rpc(value: Any):
    """An RPC that records the address queried and returns a canned account value."""
    calls: list[tuple[str, list[Any]]] = []

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        calls.append((method, params))
        return {"result": {"value": value}}

    rpc.calls = calls  # type: ignore[attr-defined]
    return rpc


def test_verify_passes_when_account_exists_and_owner_matches() -> None:
    rpc = _fake_rpc({"owner": ORE_PROGRAM, "lamports": 2_500_000})
    check = verify_derivation(CONFIG_NODE, rpc_call=rpc)

    assert check.address == ORE_CONFIG  # derived the real mainnet address
    assert check.exists is True
    assert check.owner == ORE_PROGRAM
    assert check.owner_matches is True
    # it queried getAccountInfo for exactly that derived address
    assert rpc.calls[0][0] == "getAccountInfo"
    assert rpc.calls[0][1][0] == ORE_CONFIG


def test_verify_reports_missing_account() -> None:
    """A wrong recipe derives an address that holds nothing — the gate catches it."""
    check = verify_derivation(CONFIG_NODE, rpc_call=_fake_rpc(None))
    assert check.exists is False
    assert check.owner is None
    assert check.owner_matches is None


def test_verify_flags_owner_mismatch() -> None:
    rpc = _fake_rpc({"owner": "11111111111111111111111111111111"})
    check = verify_derivation(CONFIG_NODE, rpc_call=rpc)
    assert check.exists is True
    assert check.owner_matches is False


def test_surfpool_fork_errors_without_binary() -> None:
    with pytest.raises(SurfpoolError):
        with SurfpoolFork("https://example.invalid", binary="surfpool-does-not-exist"):
            pass


# --- the real on-chain gate: env-gated, $0, no key ------------------------


@pytest.mark.skipif(
    not os.getenv("GECKO_SURFPOOL_E2E"),
    reason="set GECKO_SURFPOOL_E2E=1 (and have surfpool + a mainnet RPC) to run the on-chain gate",
)
def test_recovered_recipes_hold_real_ore_state_on_fork() -> None:
    """The automated version of the manual proof: fork mainnet locally, derive
    config/treasury/board from SOURCE-recovered recipes, and confirm each address
    holds the real deployed ORE account (owner == ORE) — $0, no key, no broadcast."""
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    nodes = from_source(ORE_SOURCE, program_id=ORE_PROGRAM)
    with SurfpoolFork(mainnet) as fork:
        for name in ("config", "treasury", "board"):
            check = verify_derivation(nodes[name], rpc_url=fork.rpc_url)
            assert check.exists, f"{name} PDA not found on fork"
            assert check.owner_matches, f"{name} not owned by ORE ({check.owner})"

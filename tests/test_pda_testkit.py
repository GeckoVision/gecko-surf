"""Phase 4: the surfpool $0 derive→verify harness.

Offline (Pattern B): the verify logic is driven with an injected RPC — a recovered
recipe deriving to an address owned by the program passes; a wrong/missing account
fails. The real on-chain gate against a live surfpool mainnet fork is env-gated
(GECKO_SURFPOOL_E2E=1) so the default suite stays offline and $0.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
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


@pytest.mark.skipif(os.name != "posix", reason="process-group reaping is POSIX-only")
def test_surfpool_fork_reaps_child_process(tmp_path: Path) -> None:
    """__exit__ must reap the whole process group — a plain terminate() would leave
    surfpool's child validator alive holding the RPC port. A fake 'surfpool' spawns a
    child that records its pid; after the context exits, that child must be gone."""
    pidfile = tmp_path / "child.pid"
    fake = tmp_path / "surfpool"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "sleep 30 &\n"  # the 'validator' child in the same process group
        f"echo $! > {pidfile}\n"
        "wait\n"
    )
    fake.chmod(0o755)

    def healthy(_url: str, _method: str, _params: list[Any]) -> dict[str, Any]:
        return {"result": "ok"}

    with SurfpoolFork(
        "http://local.invalid", binary=str(fake), rpc_call=healthy, ready_timeout=5
    ):
        for _ in range(50):  # wait for the child pid to be WRITTEN, not just the file
            # The redirect creates the file before `echo` writes into it; a read in
            # that gap saw '' (QA round, 2026-09-02) and int('') failed the sweep.
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
    recorded = pidfile.read_text().strip() if pidfile.exists() else ""
    assert recorded, "fake surfpool never recorded its child pid"
    child_pid = int(recorded)

    # after the context exits, the child must have been reaped with the group
    for _ in range(30):
        try:
            os.kill(child_pid, 0)  # 0 = existence probe: raises when the pid is gone
            time.sleep(0.1)
        except ProcessLookupError:
            break
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


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


# --- fork ports: a collision must not wear the costume of coverage --------------------
#
# Two test files both hardcoded FORK_PORT = 8937. Run in one session — and `-n auto` makes
# that the default — the loser's surfpool cannot bind, raises SurfpoolError, and every
# fork fixture in this repo turns that into `pytest.skip`. The run then reports SKIPPED,
# which reads as "this environment cannot do forks" when the truth is "this gate was
# broken by another test". A skip is a claim about the environment; a port collision is
# a claim about us, and the two must not look the same.


def test_no_two_test_files_hardcode_the_same_fork_port() -> None:
    import re
    from collections import defaultdict
    from pathlib import Path

    tests_dir = Path(__file__).parent
    by_port: dict[int, list[str]] = defaultdict(list)
    for path in sorted(tests_dir.glob("test_*.py")):
        for match in re.finditer(r"^FORK_PORT\s*=\s*(\d+)", path.read_text(), re.M):
            by_port[int(match.group(1))].append(path.name)

    clashes = {port: files for port, files in by_port.items() if len(files) > 1}
    assert not clashes, (
        "these files would fight over one port, and the loser reports SKIPPED rather "
        f"than failing: {clashes}. Use `free_port()` instead of a literal."
    )


def test_free_port_returns_something_bindable_and_does_not_repeat() -> None:
    from gecko.pda_testkit import free_port

    ports = {free_port() for _ in range(8)}
    assert len(ports) >= 2, "free_port handed out one port eight times"
    for port in ports:
        assert 1024 < port < 65535


def test_free_port_leaves_the_next_port_free_too() -> None:
    """surfpool binds an RPC port AND a websocket port at ``port + 1``. A helper that only
    checks the first hands back a port whose neighbour is taken, and the failure surfaces
    as a websocket error nobody connects to a port choice."""
    import socket

    from gecko.pda_testkit import free_port

    port = free_port()
    for candidate in (port, port + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", candidate))  # must not raise


def test_every_fork_handler_distinguishes_a_broken_gate_from_an_absent_one() -> None:
    """A `SurfpoolError` means the fork did not start. WHY decides how to report it.

    Not installed -> skip: nothing was measured, and that is expected.
    Installed and would not start -> fail: nothing was measured, and it should have been.

    Sixteen files collapsed both into `pytest.skip`, so a port collision, a stale process
    on the RPC port, or a validator that crashed on boot all read as "no surfpool on this
    machine" — the one thing they are not.
    """
    from pathlib import Path

    tests_dir = Path(__file__).parent
    offenders = []
    for path in sorted(tests_dir.glob("test_*.py")):
        text = path.read_text()
        if "except SurfpoolError" not in text:
            continue
        for block in text.split("except SurfpoolError")[1:]:
            head = block[:600]
            if "pytest.skip" in head and "start_failure_is_a_broken_gate" not in head:
                offenders.append(path.name)
                break
    assert not offenders, (
        "these files report a failed-to-start surfpool as a SKIP, which claims the "
        f"environment cannot do what it demonstrably can: {sorted(set(offenders))}"
    )


def test_the_broken_gate_helper_answers_from_the_binary_not_from_a_guess() -> None:
    import shutil

    from gecko.pda_testkit import start_failure_is_a_broken_gate

    assert start_failure_is_a_broken_gate() is (shutil.which("surfpool") is not None)
    assert start_failure_is_a_broken_gate("definitely-not-a-real-binary-xyz") is False

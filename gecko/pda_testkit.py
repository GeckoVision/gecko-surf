"""Verify recovered PDA recipes against REAL deployed state on a surfpool fork — $0.

Phase 4 of the Program Surface. The offline suite proves a recipe derives to the
right *address*; this proves the recovered seeds derive the address that actually
holds the program's live account on mainnet — the strongest correctness gate, at $0
and with no key, no signature, no broadcast.

The chain (Pattern B — falsifiable offline first, on-chain last):
  1. derive a PDA from its recovered recipe (:func:`gecko.pda.derive_pda`);
  2. ``getAccountInfo`` against a **surfpool mainnet fork** on ``127.0.0.1`` (a LOCAL
     validator lazily backed by mainnet — not mainnet itself);
  3. assert the account EXISTS and is owned by the program → the seeds were right.

Simulation (``simulateTransaction``, never ``sendTransaction``) is the optional final
rung once an instruction is built; the derive→verify gate above already falsifies a
wrong recipe.

The RPC call is injected (``rpc_call``) so the verify logic is unit-testable with no
network; the default posts JSON-RPC to the local fork. ``SurfpoolFork`` manages the
validator subprocess. This module is imported only by tests/scripts — never by the
comprehension engine — and it reads only public on-chain metadata (owner/existence),
never account payload data (control-plane invariant #1).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Mapping

from .pda import PdaNode, derive_pda

# The JSON-RPC transport now lives in the domain-neutral gecko.rpc module (layering fix).
# Re-exported here as back-compat so existing callers (pda_resolve, pumpfun, tests) that
# import RpcCall/LOCAL_RPC/_default_rpc_call from pda_testkit keep working unchanged.
from .rpc import LOCAL_RPC, RpcCall, default_rpc_call as _default_rpc_call

__all__ = [
    "DerivationCheck",
    "RpcCall",
    "LOCAL_RPC",
    "verify_derivation",
    "SurfpoolFork",
    "SurfpoolError",
]


class SurfpoolError(Exception):
    """Raised when the surfpool fork can't be started or never becomes ready."""


@dataclass(frozen=True)
class DerivationCheck:
    """The result of verifying a derived PDA against fork state."""

    account: str
    address: str
    exists: bool
    owner: str | None
    owner_matches: bool | None  # None when no program id was available to compare


def verify_derivation(
    node: PdaNode,
    bindings: Mapping[str, str | int | bytes] | None = None,
    *,
    rpc_url: str = LOCAL_RPC,
    program_id: str | None = None,
    rpc_call: RpcCall | None = None,
) -> DerivationCheck:
    """Derive ``node`` and check the address holds a real account owned by the program.

    ``rpc_call`` is injectable for offline testing; by default it posts to the local
    surfpool fork. The account's *owner* and existence are public metadata — the
    payload is never read or stored.
    """
    call = rpc_call or _default_rpc_call
    derived = derive_pda(node, bindings, program_id=program_id)
    resp = call(rpc_url, "getAccountInfo", [derived.address, {"encoding": "base64"}])
    value = (resp.get("result") or {}).get("value")
    exists = value is not None
    owner = value.get("owner") if isinstance(value, dict) else None
    expected = program_id or node.program_id
    owner_matches = (owner == expected) if (exists and expected) else None
    return DerivationCheck(
        account=node.name,
        address=derived.address,
        exists=exists,
        owner=owner,
        owner_matches=owner_matches,
    )


class SurfpoolFork:
    """Context manager for a local surfpool mainnet-fork validator.

    ``with SurfpoolFork(mainnet_rpc) as fork:`` starts
    ``surfpool start --no-tui --no-deploy --rpc-url <mainnet> --port <port>``, waits for
    the RPC to answer ``getHealth``, and terminates it on exit. Lazily fetches mainnet
    accounts on access, so verifying a handful of PDAs costs nothing and touches no key.
    """

    def __init__(
        self,
        mainnet_rpc: str,
        *,
        port: int = 8899,
        binary: str = "surfpool",
        ready_timeout: float = 30.0,
        rpc_call: RpcCall | None = None,
    ) -> None:
        self.mainnet_rpc = mainnet_rpc
        self.port = port
        self.binary = binary
        self.ready_timeout = ready_timeout
        self._rpc_call = rpc_call or _default_rpc_call
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def rpc_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> SurfpoolFork:
        if shutil.which(self.binary) is None:
            raise SurfpoolError(
                f"{self.binary!r} not found on PATH — install surfpool to run the on-chain gate"
            )
        self._proc = subprocess.Popen(
            [
                self.binary,
                "start",
                "--no-tui",
                "--no-deploy",
                "--rpc-url",
                self.mainnet_rpc,
                "--port",
                str(self.port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # New session/process group: surfpool spawns a child validator, and a plain
            # terminate() on the parent would leave that child holding the RPC port. Making
            # the parent a group leader lets __exit__ signal the WHOLE group, reaping both.
            start_new_session=True,
        )
        self._await_ready()
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._proc is not None:
            self._signal_group(signal.SIGTERM)
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._signal_group(signal.SIGKILL)
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            self._proc = None

    def _signal_group(self, sig: int) -> None:
        """Signal the validator's whole process group (surfpool + its child), tolerating
        an already-exited process."""
        if self._proc is None:
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            # already gone, or we couldn't address the group — fall back to the direct pid
            try:
                self._proc.send_signal(sig)
            except ProcessLookupError:
                pass

    def _await_ready(self) -> None:
        deadline = time.monotonic() + self.ready_timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise SurfpoolError("surfpool exited before becoming ready")
            try:
                resp = self._rpc_call(self.rpc_url, "getHealth", [])
                if resp.get("result") == "ok":
                    return
            except Exception as exc:  # not yet listening
                last = exc
            time.sleep(0.5)
        raise SurfpoolError(
            f"surfpool RPC not ready within {self.ready_timeout}s"
        ) from last

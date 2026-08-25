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

import base64

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
    "SurfpoolStatus",
    "surfpool_status",
    "verify_derivation",
    "SurfpoolFork",
    "SurfpoolError",
]


class SurfpoolError(Exception):
    """Raised when the surfpool fork can't be started or never becomes ready."""


@dataclass(frozen=True)
class SurfpoolStatus:
    """Whether a fork leg CAN run, as a value a caller must render rather than ignore.

    ``shutil.which`` already answers this in one line, so the point of a type is not the
    lookup — it is that "the fork leg did not run" becomes a fact a demo prints and a test
    names, instead of a branch that quietly does nothing. This project has already shipped
    a release that passed only because surfpool happened to be running on the machine; the
    inverse failure (a fork claim that silently degraded to no fork at all) is the same bug
    read from the other end. Both are prevented by making absence LOUD rather than falsy.
    """

    available: bool
    binary: str
    #: The resolved executable path, or ``None`` when it is not on ``PATH``.
    path: str | None
    #: One line, written to be printed verbatim.
    detail: str


def surfpool_status(binary: str = "surfpool") -> SurfpoolStatus:
    """Is ``binary`` on ``PATH``? Never raises, never starts anything, never touches a node.

    Deliberately does NOT probe an already-running validator. A fork leg that would accept
    "something is answering on 8899" inherits whatever that something is, which is exactly
    how an ambient process ends up standing in for a controlled one.
    """
    found = shutil.which(binary)
    if found is None:
        return SurfpoolStatus(
            available=False,
            binary=binary,
            path=None,
            detail=(
                f"{binary!r} is not on PATH — the fork leg was NOT run and NOTHING here "
                f"was proved against a validator"
            ),
        )
    return SurfpoolStatus(
        available=True,
        binary=binary,
        path=found,
        detail=f"{binary!r} found at {found} — the fork leg can run",
    )


@dataclass(frozen=True)
class DerivationCheck:
    """The result of verifying a derived PDA against fork state."""

    account: str
    address: str
    exists: bool
    owner: str | None
    owner_matches: bool | None  # None when no program id was available to compare
    #: Does the account's 8-byte Anchor type tag match the one expected?
    #:
    #: ``None`` when no discriminator was supplied or the data could not be read — an
    #: unverifiable claim and a verified one must never be the same value. Owner-match
    #: alone cannot answer this: EVERY account a program owns matches that program's
    #: owner, so a derivation landing on the wrong TYPE passes an owner-only check.
    discriminator_matches: bool | None = None


def verify_derivation(
    node: PdaNode,
    bindings: Mapping[str, str | int | bytes] | None = None,
    *,
    rpc_url: str = LOCAL_RPC,
    program_id: str | None = None,
    rpc_call: RpcCall | None = None,
    discriminator: bytes | None = None,
) -> DerivationCheck:
    """Derive ``node`` and check the address holds a real account owned by the program.

    ``rpc_call`` is injectable for offline testing; by default it posts to the local
    surfpool fork. The account's *owner*, existence and 8-byte type tag are public
    structural metadata — no other byte of the payload is read, compared, or stored.

    ``discriminator`` is the type tag the caller expects, as
    :func:`gecko.idl_layout.account_discriminator` reads it from the IDL. Supplying it
    turns the check from "the program owns this" into "the program owns this AND it is
    the type we meant", which is the only part that distinguishes one of a program's
    account types from another.
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
        discriminator_matches=_discriminator_matches(value, discriminator),
    )


def _discriminator_matches(value: object, expected: bytes | None) -> bool | None:
    """Compare the account's leading type tag, or return ``None``.

    Reads exactly the first ``len(expected)`` bytes and compares them. The rest of the
    account data is never decoded, returned, or retained, and the observed tag is not
    reported either — a boolean answers the question, and echoing account bytes back
    would put payload where a verdict belongs.

    Every failure path is ``None``, never ``False``: a malformed response means we could
    not check, which is a different fact from checking and disagreeing.
    """
    if not expected or not isinstance(value, dict):
        return None
    raw = value.get("data")
    encoded = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
    if not isinstance(encoded, str):
        return None
    try:
        head = base64.b64decode(encoded)[: len(expected)]
    except (ValueError, TypeError):
        return None
    if len(head) < len(expected):
        return None
    return head == expected


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
        ws_port: int | None = None,
        binary: str = "surfpool",
        ready_timeout: float = 30.0,
        rpc_call: RpcCall | None = None,
    ) -> None:
        self.mainnet_rpc = mainnet_rpc
        self.port = port
        # `--port` moves the RPC port and NOTHING ELSE: surfpool's WebSocket port is
        # separate and defaults to 8900, so two forks on different `port` values still
        # collide on it and the second dies with "WebSocket port 8900 is already in use"
        # — reported by the caller as the far less obvious "exited before becoming ready".
        # Defaulting to port+1 reproduces surfpool's own pairing (8899/8900), so existing
        # callers are unchanged while a second fork can now actually run.
        self.ws_port = port + 1 if ws_port is None else ws_port
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
                "--ws-port",
                str(self.ws_port),
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

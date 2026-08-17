"""The sandbox package — two things Gecko does OFF the wire, under one name.

* :mod:`gecko.sandbox.probe` — the offline probe sandbox. Validates an agent's call
  against the API's own comprehended schema and answers with a SYNTHETIC result. It
  never reaches the wire, never injects auth, never persists anything. This was
  ``gecko/sandbox.py``; it became a submodule when the package was created, and every
  name it exported is re-exported here, so ``from gecko.sandbox import evaluate`` and
  ``gecko.sandbox.SimStore`` keep working unchanged.

* :mod:`gecko.sandbox.surfnet` — the fork sandbox. The ONLY place in this repo where a
  private key may exist. Everything else in Gecko ends at UNSIGNED bytes; code here may
  hold a keypair and sign, and it may do so only against a local surfpool fork that has
  PROVED itself over the wire. That ordering is enforced by the type signature, not by a
  flag: :func:`~gecko.sandbox.surfnet.ephemeral_signer` accepts a
  :class:`~gecko.sandbox.surfnet.SurfnetProof`, and the only way to obtain one is
  :func:`~gecko.sandbox.surfnet.prove_surfnet`, which does the round-trip first.

* :mod:`gecko.sandbox.cheatcodes` — the surfpool state-rewriting calls, each taking a
  proof rather than a URL, with every parameter shape measured against a running fork.

* :mod:`gecko.sandbox.rehearse` — the whole purchase loop, on a fork: fund, PREPARE
  THROUGH THE PRODUCTION PATH, sign, land, and then judge it by what the ledger shows
  actually moved. It is the only code here that produces a signature.

* :mod:`gecko.sandbox.deliver` — the same loop for the STORE side, and the one place that
  can see a transaction which succeeds while doing nothing: ``mark_as_delivered`` on an
  evicted receipt id confirms, charges a fee, and changes no row. It takes the store with a
  cheatcode (fork only), lands the delivery, and then READS THE ROW BACK.

* :mod:`gecko.sandbox.agents` — the two roles those loops are FOR: a waiter that refuses
  to guess which product was meant, and a kitchen that marks an order delivered and then
  checks the row came back changed. Both decisions are injected callables, deterministic
  today and model-shaped later; neither role holds a fact about the chain.

* :mod:`gecko.sandbox.try_purchase` — that loop as the ``try_purchase`` MCP tool, mounted
  beside the real ``prepare_purchase``. It adds one narrowing the library form does not
  need: the endpoint must be on THIS machine, checked before the proof and long before a
  key, because the surface it mounts on is public.

They share the name because they share the promise: nothing in this package touches
mainnet, and nothing in it is persisted.
"""

from __future__ import annotations

from .agents import (
    RECEIPTS_CAP,
    AgentError,
    ChainQueue,
    Confirmation,
    Kitchen,
    KitchenPolicy,
    Order,
    OrderRefused,
    ResidentRow,
    Ticket,
    Waiter,
    WaiterPolicy,
    exact_name_only,
    oldest_first,
    read_chain_queue,
)
from .cheatcodes import (
    CheatcodeError,
    ClockJump,
    FundedSol,
    FundedToken,
    ResetAccount,
    TimeTravelError,
    fund_sol,
    fund_token,
    reset_account,
    time_travel,
)
from .deliver import (
    MARK_AS_DELIVERED_DISCRIMINATOR,
    Delivery,
    DeliveryError,
    TakenStore,
    rehearse_delivery,
)
from .probe import (
    PROBE_MODE_NOTE,
    SimResult,
    SimStore,
    SimWorld,
    evaluate,
)
from .rehearse import (
    LamportDelta,
    Refusal,
    Rehearsal,
    RehearsalError,
    TokenDelta,
    WrittenReceipt,
    rehearse_purchase,
)
from .try_purchase import (
    HOW_TO_START_A_FORK,
    TRY_PURCHASE_TOOL,
    try_purchase_result,
)
from .surfnet import (
    SURFNET_INFO_METHOD,
    EphemeralSigner,
    EphemeralSignerError,
    NotASurfnetError,
    SandboxError,
    SurfnetProof,
    ephemeral_signer,
    prove_surfnet,
)

__all__ = [
    # probe sandbox (unchanged public surface of the former gecko/sandbox.py)
    "PROBE_MODE_NOTE",
    "SimResult",
    "SimStore",
    "SimWorld",
    "evaluate",
    # fork sandbox
    "SURFNET_INFO_METHOD",
    "EphemeralSigner",
    "EphemeralSignerError",
    "NotASurfnetError",
    "SandboxError",
    "SurfnetProof",
    "ephemeral_signer",
    "prove_surfnet",
    # fork cheatcodes — every one takes a SurfnetProof, never a URL
    "CheatcodeError",
    "ClockJump",
    "FundedSol",
    "FundedToken",
    "ResetAccount",
    "TimeTravelError",
    "fund_sol",
    "fund_token",
    "reset_account",
    "time_travel",
    # the rehearsal — the only path in this repo that signs and lands anything
    "LamportDelta",
    "Refusal",
    "Rehearsal",
    "RehearsalError",
    "TokenDelta",
    "WrittenReceipt",
    "rehearse_purchase",
    # the store side — the transaction that succeeds and changes nothing
    "MARK_AS_DELIVERED_DISCRIMINATOR",
    "Delivery",
    "DeliveryError",
    "TakenStore",
    "rehearse_delivery",
    # the two roles — thin, with the decision seams injected
    "RECEIPTS_CAP",
    "AgentError",
    "ChainQueue",
    "Confirmation",
    "Kitchen",
    "KitchenPolicy",
    "Order",
    "OrderRefused",
    "ResidentRow",
    "Ticket",
    "Waiter",
    "WaiterPolicy",
    "exact_name_only",
    "oldest_first",
    "read_chain_queue",
    # the rehearsal as an MCP tool — mounted beside `prepare_purchase`
    "HOW_TO_START_A_FORK",
    "TRY_PURCHASE_TOOL",
    "try_purchase_result",
]

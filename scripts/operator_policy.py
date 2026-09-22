"""What the OPERATOR authored, by hand, and nothing read off the chain at run time.

One copy, imported by every runner that spends from a wallet we operate, so two scripts
cannot hold two different pins for the same mint.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko.simulate import AcceptedMint  # noqa: E402

USDG_MINT = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"

# The operator's acceptance of USDG's Token-2022 extensions, AUTHORED here on 2026-09-18
# from a jsonParsed read of the mint and never copied off the chain at run time: the
# simulation measures the USDG leg only while the mint still reads exactly this set with
# the hook reserved and pointing nowhere (programId null). If Paxos adds an extension or
# points the hook at a program, the run refuses as mint-extensions-changed and a human
# looks again. The transfer fee RATE (0 bps on 2026-09-18) is not pinned: a raised fee
# lowers what the swap returns, which plan_swap's minimum-out defends, not this gate.
# Founder decision 2026-09-18: explicit per-mint acceptance, in the policy.
USDG_ACCEPTED = AcceptedMint(
    mint=USDG_MINT,
    extensions=frozenset(
        {
            "mintCloseAuthority",
            "permanentDelegate",
            "transferFeeConfig",
            "confidentialTransferMint",
            "confidentialTransferFeeConfig",
            "transferHook",
            "metadataPointer",
            "tokenMetadata",
        }
    ),
    transfer_hook_program=None,
)

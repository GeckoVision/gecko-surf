"""Agentic wallet in the middle — the 3-step user surface, $0 offline.

The model: the user only **(1) funds** a wallet, **(2) sets it up**, and **(3) authorizes a
policy** (one signature delegating bounded authority). The agent — with **Gecko** as the
API-comprehension **brain** — does everything else: comprehends the paywalled TxLINE API,
builds the correct on-chain subscribe + settle transactions, and hands each to the wallet,
which **signs only within the user's authorized policy**.

Gecko never holds keys or funds (control-plane only). The wallet is the policy-gated **hands**.
This demo models a **$0 sandbox wallet** (like `pay --sandbox`: an auto-funded ephemeral
wallet, no real USDC) so the whole flow is falsifiable offline. For mainnet, an enclave wallet
(Privy / OKX OnchainOS) plugs in behind the same `WalletSeam` — an injected boundary, exactly
like `gecko.access.Session` — with pay.sh as the x402 rail composed on top (the wallet signs;
the rail settles). Gecko never becomes the signer.

    uv run python -m examples.txline_sharp_agent.wallet_sim
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Policy:
    """What the user authorized once — the bound the wallet signs within."""

    max_spend_usdc: float
    allowed_purposes: frozenset[str]


@dataclass(frozen=True)
class TxIntent:
    """A transaction the agent (Gecko) built and asks the wallet to sign."""

    purpose: str
    amount_usdc: float
    description: str


@dataclass(frozen=True)
class SignResult:
    intent: TxIntent
    ref: str  # opaque sandbox reference — NOT a real signature (no keys here)


class PolicyViolation(Exception):
    """The wallet refused: over cap, off-purpose, or unfunded. The bound held."""


@runtime_checkable
class WalletSeam(Protocol):
    """The hands. Any wallet (pay.sh sandbox/live, Privy, OKX OnchainOS) satisfies this."""

    def funded_usdc(self) -> float: ...
    def authorize(self, policy: Policy) -> None: ...
    def sign_within_policy(self, intent: TxIntent) -> SignResult: ...


@dataclass
class SandboxWallet:
    """A $0 ephemeral wallet (models `pay --sandbox`). Auto-funded, no real USDC, no real
    keys. Enforces the user's policy: signs within it, rejects anything over cap or
    off-purpose. Deterministic — the whole demo is falsifiable offline (Pattern B)."""

    funded: float = 100.0
    _policy: Policy | None = field(default=None, init=False)
    _spent: float = field(default=0.0, init=False)

    def funded_usdc(self) -> float:
        return round(self.funded - self._spent, 6)

    def authorize(self, policy: Policy) -> None:
        self._policy = policy

    def sign_within_policy(self, intent: TxIntent) -> SignResult:
        if self._policy is None:
            raise PolicyViolation(
                "no policy authorized — the user hasn't delegated authority"
            )
        if intent.purpose not in self._policy.allowed_purposes:
            raise PolicyViolation(
                f"purpose {intent.purpose!r} is not in the authorized policy"
            )
        if self._spent + intent.amount_usdc > self._policy.max_spend_usdc:
            raise PolicyViolation(
                f"would exceed the ${self._policy.max_spend_usdc:g} policy cap"
            )
        if intent.amount_usdc > self.funded_usdc():
            raise PolicyViolation("insufficient sandbox funds")
        self._spent = round(self._spent + intent.amount_usdc, 6)
        # deterministic opaque ref — there is NO signing here (no keys, no broadcast)
        digest = hashlib.sha256(
            f"{intent.purpose}:{intent.amount_usdc}:{self._spent}".encode()
        ).hexdigest()[:16]
        return SignResult(intent=intent, ref=f"sandbox:{digest}")


# Illustrative USDC costs (the demo is about the WALLET MODEL, not exact TxLINE pricing).
_SUBSCRIBE_USDC = 20.0
_SETTLE_FEE_USDC = 1.0


def run(wallet: WalletSeam | None = None) -> int:
    wallet = wallet or SandboxWallet(funded=100.0)
    rule = "─" * 68
    print(
        f"{rule}\n  Agentic wallet in the middle — 3 user steps, the agent does the rest\n{rule}"
    )

    # ---- The user's WHOLE surface: 3 acts ----
    print("\n  USER (3 steps):")
    print(
        f"    1 · fund the wallet        → ${wallet.funded_usdc():g} USDC (sandbox, $0)"
    )
    print("    2 · set up the wallet       → done (ephemeral sandbox wallet)")
    policy = Policy(
        max_spend_usdc=50.0,
        allowed_purposes=frozenset({"txline-subscription", "market-settlement"}),
    )
    wallet.authorize(policy)
    print(
        f"    3 · authorize ONE policy    → spend ≤ ${policy.max_spend_usdc:g} for "
        "{txline-subscription, market-settlement}"
    )

    # ---- The agent (Gecko brain) does everything else ----
    print("\n  AGENT (Gecko builds each tx; the wallet signs within the policy):")
    signed = []
    for intent in (
        TxIntent(
            "txline-subscription",
            _SUBSCRIBE_USDC,
            "subscribe to TxLINE (on-chain, USDC)",
        ),
        TxIntent(
            "market-settlement",
            _SETTLE_FEE_USDC,
            "settle a prediction market on the Merkle proof",
        ),
    ):
        res = wallet.sign_within_policy(intent)
        signed.append(res)
        print(f"    ✓ {intent.description:52s} ${intent.amount_usdc:>5g}  [{res.ref}]")

    # ---- The bound holds: an off-policy request is refused ----
    print("\n  The policy is a real bound — an over-cap request is refused:")
    try:
        wallet.sign_within_policy(
            TxIntent("market-settlement", 40.0, "oversized settle")
        )
        print("    ✗ SIGNED (should not happen)")
    except PolicyViolation as exc:
        print(f"    ✓ refused: {exc}")

    print(f"\n{rule}")
    print(
        f"  {len(signed)} txns signed within policy · ${wallet.funded_usdc():g} USDC left."
    )
    print(
        "  The user read no docs, built no transaction, handled no key. Gecko is the brain"
    )
    print(
        "  in the middle; the wallet is the policy-gated hands. Gecko never held funds or keys."
    )
    print(
        "  For mainnet, swap SandboxWallet for a Privy/OKX enclave wallet behind WalletSeam —"
    )
    print(
        "  with pay.sh as the x402 rail on top (the wallet signs; the rail settles).\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

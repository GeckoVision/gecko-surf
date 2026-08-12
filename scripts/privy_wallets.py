"""What Privy wallets exist, and what each one holds. READ-ONLY — nothing here signs.

    uv run python scripts/privy_wallets.py                    # list every wallet
    uv run python scripts/privy_wallets.py --mint <mint>      # + that SPL balance

Answers the question that has to be settled before any autonomous run: WHICH wallet, at
WHICH address, holding WHAT. Guessing any of the three is how an agent signs a useless
transaction, or the wrong one.

Credentials come from Gecko's own chain (keyring → command → env), so the app secret lives
in the OS keychain rather than a shell profile. Nothing is printed but wallet ids,
addresses and balances — never a secret, never a key.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gecko.rpc import default_rpc_call  # noqa: E402
from scripts.privy_backend import (  # noqa: E402
    PRIVY_API_BASE,
    PrivyBackendError,
    PrivyRequest,
    _default_transport,
    resolve_config,
)

USDC_MAINNET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"


def _get(path: str, config: dict[str, str]) -> dict[str, Any]:
    import base64

    request = PrivyRequest(
        method="GET",
        url=f"{PRIVY_API_BASE}{path}",
        body=None,
        privy_headers={"privy-app-id": config["PRIVY_APP_ID"]},
    )
    basic = base64.b64encode(
        f"{config['PRIVY_APP_ID']}:{config['PRIVY_APP_SECRET']}".encode()
    ).decode("ascii")
    return _default_transport(
        request,
        {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/json",
            "privy-app-id": config["PRIVY_APP_ID"],
        },
    )


def sol_balance(address: str, rpc_url: str) -> float:
    try:
        answer = default_rpc_call(rpc_url, "getBalance", [address])
        return int((answer.get("result") or {}).get("value") or 0) / 1_000_000_000
    except Exception:  # noqa: BLE001 - an absent account reads as zero
        return 0.0


def token_balance(owner: str, mint: str, rpc_url: str) -> tuple[float, str | None]:
    """The owner's balance of ``mint``, and the token account holding it (``None`` if there
    is no account at all — which is a different fact from a zero balance)."""
    try:
        answer = default_rpc_call(
            rpc_url,
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        accounts = (answer.get("result") or {}).get("value") or []
        if not accounts:
            return 0.0, None
        first = accounts[0]
        parsed = first["account"]["data"]["parsed"]["info"]["tokenAmount"]
        return float(parsed.get("uiAmount") or 0), first.get("pubkey")
    except Exception as exc:  # noqa: BLE001
        print(f"   (balance lookup failed: {type(exc).__name__})")
        return 0.0, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mint", default=USDC_MAINNET, help="SPL mint to report")
    parser.add_argument("--rpc-url", default=DEFAULT_RPC)
    args = parser.parse_args(argv)

    config = resolve_config()
    missing = [
        name for name in ("PRIVY_APP_ID", "PRIVY_APP_SECRET") if not config.get(name)
    ]
    if missing:
        print(f"STOP: not configured: {', '.join(missing)}")
        return 2

    try:
        listed = _get("/v1/wallets", config)
    except PrivyBackendError as exc:
        print(f"STOP: {exc}")
        return 2
    except urllib.error.HTTPError as exc:  # pragma: no cover - transport detail
        print(f"STOP: Privy answered HTTP {exc.code}")
        return 2

    wallets = listed.get("data") if isinstance(listed, dict) else None
    if not isinstance(wallets, list):
        print(f"STOP: unexpected shape from Privy: {sorted(listed)[:6]}")
        return 2

    print(f"{len(wallets)} wallet(s) in this Privy app\n")
    configured = config.get("PRIVY_WALLET_ID")
    for wallet in wallets:
        wallet_id = wallet.get("id")
        chain = wallet.get("chain_type")
        address = wallet.get("address")
        marker = "  <- PRIVY_WALLET_ID" if wallet_id == configured else ""
        print(f"  id      {wallet_id}{marker}")
        print(f"  chain   {chain}")
        print(f"  address {address}")
        if chain == "solana" and address:
            sol = sol_balance(address, args.rpc_url)
            amount, account = token_balance(address, args.mint, args.rpc_url)
            print(f"  SOL     {sol:.9f}   (fees come from here)")
            print(f"  token   {amount} of {args.mint}")
            print(f"  ATA     {account or 'NONE — no token account exists yet'}")
        print()

    if not configured:
        print(
            "PRIVY_WALLET_ID is not set. Pick the solana wallet above and run:\n"
            "  gecko auth set PRIVY_WALLET_ID"
        )
    print(json.dumps({"wallets": len(wallets)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

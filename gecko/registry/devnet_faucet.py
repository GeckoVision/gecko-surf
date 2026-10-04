"""The bootcamp's DEVNET faucet: one call funds a student's buyer for the class store.

    POST /registry/class-wallet/faucet   {"buyer": "<base58 address>"}

Sends what `make smoke` needs and nobody else can give: the class "USDC" and the
lookalike token (our own devnet mints), plus SOL up to a small target. Before this,
every student posted an address and waited for the instructor to run a script.

THE FIRST SERVER-HELD KEY THAT SIGNS, AND WHY IT IS BOUNDED THE WAY IT IS. Gecko's
store tools never sign, and this changes nothing there. This key is:

- DEVNET ONLY. The RPC's genesis hash is checked against devnet's before every send,
  so a misconfigured RPC URL refuses rather than spending on another cluster.
- NOT THE MINT AUTHORITY. It is a separate faucet wallet the instructor pre-loads;
  this code only TRANSFERS from it. A leaked key loses some devnet tokens, nothing more.
- IDEMPOTENT ON-CHAIN. A buyer that already holds a class token is not sent more of
  it, and SOL is topped up only to the target. Asking twice is free and harmless, and
  the rule survives a restart because it lives on the chain, not in this process.

No Gecko key is needed: most students have none, and the tokens are worthless. The
per-IP bucket in the route bounds how fast anyone can drain the faucet.
"""

from __future__ import annotations

import json
import logging
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gecko.landing import ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_PROGRAM_ID
from gecko.rpc import RpcError, default_rpc_call
from gecko.store_accounts import derive_ata

logger = logging.getLogger(__name__)

DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
DEVNET_RPC = "https://api.devnet.solana.com"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"

#: The class "USDC" the class store prices in, and the lookalike that lets the scam-token
#: case reach the student's own mint check. Both 6 decimals, both ours, both devnet.
CLASS_MINTS = (
    "Eoqdd43nFQ9HzGq8HjBRVLCV6aTqCFRiwHy1ZVQheYSi",
    "BRPT4Sr7CWcJhfdwMJektzvLFKjgzVBK2AfrW4nPCEM6",
)
CLASS_DECIMALS = 6
TOKENS_EACH = 20
SOL_TARGET_LAMPORTS = 50_000_000  # 0.05 SOL: fees and rent for a week of buys

KEY_ENV = "GECKO_DEVNET_FAUCET_KEY"
RPC_ENV = "GECKO_DEVNET_FAUCET_RPC"

RpcCall = Callable[[str, str, list[Any]], dict[str, Any]]


class FaucetError(Exception):
    """A refusal the caller can act on. Never carries key material."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Funded:
    buyer: str
    sent_lamports: int
    sent_tokens: dict[str, int]
    signature: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "buyer": self.buyer,
            "network": "devnet",
            "sent_sol": self.sent_lamports / 1e9,
            "sent_tokens": self.sent_tokens,
            "signature": self.signature,
            "explorer": (
                f"https://explorer.solana.com/tx/{self.signature}?cluster=devnet"
                if self.signature
                else None
            ),
            "note": (
                "already funded: nothing to send"
                if self.signature is None
                else "funded on devnet"
            ),
        }


def _pubkey(address: str) -> Any:
    from solders.pubkey import Pubkey

    try:
        key = Pubkey.from_string(address)
    except (ValueError, TypeError) as error:
        raise FaucetError(
            400, "bad-address", "buyer must be a base58 Solana address"
        ) from error
    if not key.is_on_curve():
        raise FaucetError(
            400,
            "not-a-wallet",
            "that address is a program-derived account, not a wallet: send your buyer address",
        )
    return key


def _token_transfer(source: str, destination: str, owner: str, amount: int) -> Any:
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    # SPL Token `Transfer` (tag 3, u64 amount): source, destination, owner (signer).
    return Instruction(
        Pubkey.from_string(TOKEN_PROGRAM_ID),
        bytes([3]) + struct.pack("<Q", amount),
        [
            AccountMeta(Pubkey.from_string(source), is_signer=False, is_writable=True),
            AccountMeta(
                Pubkey.from_string(destination), is_signer=False, is_writable=True
            ),
            AccountMeta(Pubkey.from_string(owner), is_signer=True, is_writable=False),
        ],
    )


def _create_ata_idempotent(payer: str, owner: str, mint: str) -> Any:
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    ata = derive_ata(owner, mint, token_program=TOKEN_PROGRAM_ID)
    keys = [
        (payer, True, True),
        (ata, False, True),
        (owner, False, False),
        (mint, False, False),
        (SYSTEM_PROGRAM_ID, False, False),
        (TOKEN_PROGRAM_ID, False, False),
    ]
    return Instruction(
        Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID),
        bytes([1]),  # CreateIdempotent: a second call is a no-op, never a failure
        [
            AccountMeta(Pubkey.from_string(k), is_signer=s, is_writable=w)
            for k, s, w in keys
        ],
    )


class DevnetFaucet:
    def __init__(
        self,
        keypair: Any,
        *,
        rpc_url: str = DEVNET_RPC,
        rpc_call: RpcCall = default_rpc_call,
        mints: tuple[str, ...] = CLASS_MINTS,
        tokens_each: int = TOKENS_EACH,
        decimals: int = CLASS_DECIMALS,
        sol_target_lamports: int = SOL_TARGET_LAMPORTS,
    ) -> None:
        self._keypair = keypair
        self._rpc_url = rpc_url
        self._rpc = rpc_call
        self.mints = mints
        self.raw_each = tokens_each * 10**decimals
        self.sol_target = sol_target_lamports

    @property
    def address(self) -> str:
        return str(self._keypair.pubkey())

    def _call(self, method: str, params: list[Any]) -> Any:
        try:
            return self._rpc(self._rpc_url, method, params).get("result")
        except (RpcError, OSError, ValueError) as error:
            raise FaucetError(
                502, "rpc-failed", f"devnet RPC failed on {method}"
            ) from error

    def _assert_devnet(self) -> None:
        if self._call("getGenesisHash", []) != DEVNET_GENESIS:
            raise FaucetError(
                503, "not-devnet", "the faucet's RPC is not devnet: refusing"
            )

    def _lamports(self, address: str) -> int:
        result = self._call("getBalance", [address, {"commitment": "confirmed"}]) or {}
        return int(result.get("value", 0))

    def _token_raw(self, owner: str, mint: str) -> int:
        result = (
            self._call(
                "getTokenAccountsByOwner",
                [
                    owner,
                    {"mint": mint},
                    {"encoding": "jsonParsed", "commitment": "confirmed"},
                ],
            )
            or {}
        )
        return sum(
            int(acct["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            for acct in result.get("value", [])
        )

    def fund(self, buyer: str) -> Funded:
        """Top `buyer` up to the class kit. Raises FaucetError; never partially signs."""
        from solders.hash import Hash
        from solders.message import Message
        from solders.system_program import TransferParams, transfer
        from solders.transaction import Transaction

        buyer_key = _pubkey(buyer)
        if str(buyer_key) == self.address:
            raise FaucetError(400, "bad-address", "that is the faucet's own address")
        self._assert_devnet()

        faucet = self.address
        instructions = []
        sent_tokens: dict[str, int] = {}
        for mint in self.mints:
            if self._token_raw(buyer, mint) > 0:
                continue
            source = derive_ata(faucet, mint, token_program=TOKEN_PROGRAM_ID)
            if self._token_raw(faucet, mint) < self.raw_each:
                raise FaucetError(
                    503, "faucet-empty", "the class faucet is out of tokens"
                )
            instructions.append(_create_ata_idempotent(faucet, buyer, mint))
            instructions.append(
                _token_transfer(
                    source,
                    derive_ata(buyer, mint, token_program=TOKEN_PROGRAM_ID),
                    faucet,
                    self.raw_each,
                )
            )
            sent_tokens[mint] = self.raw_each

        lamports = max(0, self.sol_target - self._lamports(buyer))
        if lamports:
            if self._lamports(faucet) < lamports + 10_000_000:
                raise FaucetError(503, "faucet-empty", "the class faucet is out of SOL")
            instructions.insert(
                0,
                transfer(
                    TransferParams(
                        from_pubkey=self._keypair.pubkey(),
                        to_pubkey=buyer_key,
                        lamports=lamports,
                    )
                ),
            )
        if not instructions:
            return Funded(buyer, 0, {}, None)

        blockhash = (
            self._call("getLatestBlockhash", [{"commitment": "confirmed"}]) or {}
        )["value"]["blockhash"]
        message = Message.new_with_blockhash(
            instructions, self._keypair.pubkey(), Hash.from_string(blockhash)
        )
        tx = Transaction([self._keypair], message, Hash.from_string(blockhash))
        import base64

        # Checked again right before the bytes leave: the genesis check above is a
        # separate request, and the route must never send a signed transaction anywhere
        # but devnet.
        self._assert_devnet()
        signature = self._call(
            "sendTransaction",
            [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64"}],
        )
        if not isinstance(signature, str):
            raise FaucetError(
                502, "send-failed", "devnet did not accept the transaction"
            )
        logger.info("devnet faucet: funded a buyer (signature %s)", signature)
        return Funded(buyer, lamports, sent_tokens, signature)


def build_faucet_from_env() -> DevnetFaucet | None:
    """The faucet, or None (the route answers 503) when no key is configured.

    The key is a JSON byte array, the `solana-keygen` file format, in one env var.
    It is never logged; a malformed value disables the faucet rather than the server.
    """
    raw = (os.environ.get(KEY_ENV) or "").strip()
    if not raw or raw == "__unset__":
        return None
    try:
        from solders.keypair import Keypair

        keypair = Keypair.from_bytes(bytes(json.loads(raw)))
    except Exception:  # noqa: BLE001 - a bad secret disables the faucet, not the server
        logger.warning("devnet faucet: %s is set but unreadable (redacted)", KEY_ENV)
        return None
    return DevnetFaucet(keypair, rpc_url=os.environ.get(RPC_ENV) or DEVNET_RPC)

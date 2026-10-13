"""The chain half of recorded mode: a mainnet-SHAPED node and Helius's sender.

:class:`RecordedChain` reads the amounts out of the transaction it is sent, so every
answer follows the bytes: a USDC payment (one ``TransferChecked`` to the treasury), a SOL
payment (one System ``Transfer`` to the settlement wallet), and a settlement (wrap, sync,
``swap_v2`` at a recorded rate, ``TransferChecked`` to the treasury). It can be told to
show a rebate to the relay in a SETTLEMENT's slot. :class:`RecordedHeliusPost` forwards
to it and keeps the ``rebate-address`` it was asked for, never the URL (which carries
the API key). Every answer is synthesized: this proves what billing does GIVEN such
answers, not that mainnet gives them.
"""

from __future__ import annotations

import base64
import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from ..autonomous_purchase import SWAP_V2_DISCRIMINATOR, USDC_MINT
from ..landing import NATIVE_SOL_MINT, SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID

__all__ = ["RecordedChain", "RecordedHeliusPost"]

_TRANSFER_CHECKED = 12
_FEE_LAMPORTS = 5_000
_POOL_SOL = 10**12
_POOL_USDC = 10**11


@dataclass(frozen=True)
class _Tx:
    """What one transaction does, read from its bytes."""

    kind: Literal["usdc", "sol", "settle"]
    signer: str
    #: USDC to the treasury (usdc, settle); lamports moved (sol: paid, settle: swapped).
    usdc: int
    lamports: int


@dataclass(frozen=True)
class _Landed:
    signature: str
    slot: int
    tx: _Tx
    usdc_out: int
    treasury_before: int
    settlement_usdc_before: int
    lamports_before: Mapping[str, int]


def _read(tx_base64: str) -> _Tx:
    from solders.transaction import Transaction

    tx = Transaction.from_bytes(base64.b64decode(tx_base64))
    keys = [str(k) for k in tx.message.account_keys]
    usdc = lamports = sol_in = 0
    swapped = False
    for ix in tx.message.instructions:
        program, data = keys[ix.program_id_index], bytes(ix.data)
        if program == TOKEN_PROGRAM_ID and data[:1] == bytes([_TRANSFER_CHECKED]):
            usdc = struct.unpack_from("<Q", data, 1)[0]
        elif program == SYSTEM_PROGRAM_ID and len(data) == 12:
            lamports = struct.unpack_from("<Q", data, 4)[0]
        elif data[:8] == bytes(SWAP_V2_DISCRIMINATOR):
            swapped = True
            sol_in = struct.unpack_from("<Q", data, 8)[0]
    if swapped:
        return _Tx("settle", keys[0], usdc, sol_in)
    if lamports:
        return _Tx("sol", keys[0], 0, lamports)
    return _Tx("usdc", keys[0], usdc, 0)


def _row(index: int, owner: str, mint: str, amount: int, decimals: int = 6) -> dict:
    return {
        "accountIndex": index,
        "mint": mint,
        "owner": owner,
        "programId": TOKEN_PROGRAM_ID,
        "uiTokenAmount": {
            "amount": str(amount),
            "decimals": decimals,
            "uiAmountString": str(amount / 10**decimals),
        },
    }


@dataclass
class RecordedChain:
    """A replaying node: ``(url, method, params) -> JSON-RPC reply``.

    ``rate_micro`` is micro-USDC per SOL at the pool. ``rebates`` maps the 0-based index
    of a SETTLEMENT to the lamports a separate transaction pays the relay (the configured
    rebate address) in that settlement's slot: what a Helius backrun rebate looks like
    on chain. ``relay_lamports`` is the relay's balance, for the hot-balance check.
    """

    payer: str
    fee_wallet: str
    relay: str
    pool: str
    rate_micro: int
    settlement: str = ""
    rebates: Mapping[int, int] = field(default_factory=dict)
    payer_lamports: int = 1_000_000_000
    settlement_lamports: int = 2_000_000
    relay_lamports: int = 50_000_000
    payer_usdc: int = 0
    settlement_usdc: int = 0
    first_slot: int = 400_000_000
    landed: list[_Landed] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    _treasury_usdc: int = 0

    # -- what the story reads back -------------------------------------------------

    @property
    def paid(self) -> list[_Landed]:
        """The user's payments, in order."""
        return [x for x in self.landed if x.tx.kind != "settle"]

    @property
    def settled(self) -> list[_Landed]:
        """Gecko's settlements, in order."""
        return [x for x in self.landed if x.tx.kind == "settle"]

    # -- the node ------------------------------------------------------------------

    def _lamports(self) -> dict[str, int]:
        return {
            self.payer: self.payer_lamports,
            self.settlement: self.settlement_lamports,
            self.relay: self.relay_lamports,
        }

    def _usdc_out(self, tx: _Tx) -> int:
        return (
            tx.lamports * self.rate_micro // 1_000_000_000 if tx.kind == "settle" else 0
        )

    def _after(
        self, tx: _Tx, before: Mapping[str, int] | None = None
    ) -> dict[str, int]:
        """Every tracked lamport balance after ``tx`` (from ``before``, default now)."""
        after = dict(self._lamports() if before is None else before)
        after[tx.signer] = after.get(tx.signer, 0) - _FEE_LAMPORTS
        if tx.kind == "sol":
            after[self.payer] -= tx.lamports
            after[self.settlement] += tx.lamports
        elif tx.kind == "settle":
            after[self.settlement] -= tx.lamports
        return after

    def _rows(self, tx: _Tx, usdc_out: int, treasury: int, held: int) -> Any:
        if tx.kind == "sol":
            return [], []
        if tx.kind == "usdc":
            pre = [
                _row(1, self.payer, USDC_MINT, self.payer_usdc),
                _row(2, self.fee_wallet, USDC_MINT, treasury),
            ]
            post = [
                _row(1, self.payer, USDC_MINT, self.payer_usdc - tx.usdc),
                _row(2, self.fee_wallet, USDC_MINT, treasury + tx.usdc),
            ]
            return pre, post
        pre = [
            _row(1, self.settlement, USDC_MINT, held),
            _row(2, self.fee_wallet, USDC_MINT, treasury),
            _row(5, self.pool, NATIVE_SOL_MINT, _POOL_SOL, 9),
            _row(6, self.pool, USDC_MINT, _POOL_USDC),
        ]
        post = [
            # The settlement wallet keeps whatever the swap paid above what it forwarded.
            _row(1, self.settlement, USDC_MINT, held + usdc_out - tx.usdc),
            _row(2, self.fee_wallet, USDC_MINT, treasury + tx.usdc),
            _row(5, self.pool, NATIVE_SOL_MINT, _POOL_SOL + tx.lamports, 9),
            _row(6, self.pool, USDC_MINT, _POOL_USDC - usdc_out),
        ]
        return pre, post

    def _rebate_signature(self, index: int) -> str:
        return f"{index + 1}" + "R" * 87

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.methods.append(method)
        slot = self.first_slot + 10 * len(self.landed)
        if method == "getLatestBlockhash":
            blockhash = "6xCk4Xgb64QofLjfh5Q5sy47W5dURHagdcDWWhhoAqgo"
            return {
                "result": {
                    "value": {"blockhash": blockhash, "lastValidBlockHeight": 655}
                }
            }
        if method == "getRecentPrioritizationFees":
            return {"result": []}
        if method == "getAccountInfo":
            lamports = self._lamports().get(params[0], 2_039_280)
            return {"result": {"value": {"lamports": lamports, "data": ["", "base64"]}}}
        if method == "getBalance":
            return {"result": {"value": self._lamports().get(params[0], 0)}}
        if method == "getTokenAccountBalance":
            from .payment_tx import usdc_ata

            held = (
                self.settlement_usdc
                if self.settlement and params[0] == usdc_ata(self.settlement)
                else self.payer_usdc
            )
            return {"result": {"value": {"amount": str(held)}}}
        if method == "getSlot":
            return {"result": slot}
        if method == "simulateTransaction":
            tx = _read(params[0])
            tracked = ((params[1] or {}).get("accounts") or {}).get("addresses") or [
                tx.signer
            ]
            usdc_out = self._usdc_out(tx)
            pre, post = self._rows(
                tx, usdc_out, self._treasury_usdc, self.settlement_usdc
            )
            value = {
                "err": None,
                "logs": ["Program log: recorded simulation"],
                "unitsConsumed": 30_000,
                "accounts": [
                    {
                        "lamports": self._after(tx).get(tracked[0], 0),
                        "data": ["", "base64"],
                    }
                ],
                "preTokenBalances": pre,
                "postTokenBalances": post,
            }
            return {"result": {"context": {"slot": slot - 1}, "value": value}}
        if method == "sendTransaction":
            return {"result": self._land(_read(params[0]), slot)}
        if method == "getSignatureStatuses":
            known = {x.signature: x.slot for x in self.landed}
            statuses: list[dict[str, Any] | None] = [
                {"confirmationStatus": "confirmed", "err": None, "slot": known[s]}
                if s in known
                else None
                for s in params[0]
            ]
            return {"result": {"value": statuses}}
        if method == "getTransaction":
            return {"result": self._transaction(params[0])}
        if method == "getSignaturesForAddress":
            rows: list[dict[str, Any]] = []
            for index, swap in enumerate(self.settled):
                if self.rebates.get(index):
                    sig = self._rebate_signature(index)
                    rows.append({"signature": sig, "slot": swap.slot, "err": None})
            return {"result": list(reversed(rows))}
        return {"error": {"code": -32601, "message": f"unrecorded method {method}"}}

    def _land(self, tx: _Tx, slot: int) -> str:
        prefix = "S" if tx.kind == "settle" else "P"
        signature = f"{len(self.landed) + 1}" + prefix * 87
        usdc_out = self._usdc_out(tx)
        self.landed.append(
            _Landed(
                signature,
                slot,
                tx,
                usdc_out,
                self._treasury_usdc,
                self.settlement_usdc,
                self._lamports(),
            )
        )
        after = self._after(tx)
        self.payer_lamports = after[self.payer]
        if self.settlement:
            self.settlement_lamports = after[self.settlement]
        self._treasury_usdc += tx.usdc
        if tx.kind == "usdc":
            self.payer_usdc -= tx.usdc
        if tx.kind == "settle":
            self.settlement_usdc += usdc_out - tx.usdc
        return signature

    def _transaction(self, signature: str) -> dict[str, Any] | None:
        for landed in self.landed:
            if signature != landed.signature:
                continue
            pre, post = self._rows(
                landed.tx,
                landed.usdc_out,
                landed.treasury_before,
                landed.settlement_usdc_before,
            )
            keys = [self.payer, self.settlement, self.relay]
            before = [landed.lamports_before.get(k, 0) for k in keys]
            after_map = self._after(landed.tx, landed.lamports_before)
            return {
                "slot": landed.slot,
                "transaction": {"message": {"accountKeys": keys}},
                "meta": {
                    "err": None,
                    "computeUnitsConsumed": 29_000,
                    "preBalances": before,
                    "postBalances": [after_map.get(k, 0) for k in keys],
                    "preTokenBalances": pre,
                    "postTokenBalances": post,
                },
            }
        for index, swap in enumerate(self.settled):
            lamports = self.rebates.get(index, 0)
            if lamports and signature == self._rebate_signature(index):
                searcher = "11111111111111111111111111111112"
                return {
                    "slot": swap.slot,
                    "transaction": {"message": {"accountKeys": [searcher, self.relay]}},
                    "meta": {
                        "err": None,
                        "preBalances": [10**10, 1_000_000],
                        "postBalances": [10**10 - lamports, 1_000_000 + lamports],
                    },
                }
        return None


@dataclass
class RecordedHeliusPost:
    """Helius's sender endpoint: forwards to the node, keeps the rebate address asked
    for. The URL is parsed and dropped, because it carries the API key."""

    chain: RecordedChain
    rebate_addresses: list[str | None] = field(default_factory=list)

    def __call__(self, url: str, body: bytes) -> dict[str, Any]:
        query = parse_qs(urlsplit(url).query)
        asked = query.get("rebate-address") or []
        request = json.loads(body)
        if request["method"] == "sendTransaction":
            self.rebate_addresses.append(asked[0] if asked else None)
        return self.chain("helius", request["method"], request["params"])

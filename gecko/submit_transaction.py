"""Submit a Gecko-verified signed transaction and rebroadcast until it lands.

The friction report (2026-09-01, a real Claude-web run) measured the gap this
closes: Gecko returned prose instructions for a rebroadcast loop and shipped no
tool that runs it, so an agent WITHOUT a shell could plan, simulate and sign a
purchase — and then stop. Worse, the agent under the ~60s blockhash clock dropped
``verify_signed_transaction`` from its critical path because it was a separate
round trip ("verification after broadcast is theatre"). Bundling the check into
the submit makes the safety free instead of optional.

**The binding is REQUIRED, and that is the whole security design.** This tool
broadcasts ONLY bytes that verify, at ``exact`` strength, against a binding a
Gecko receipt issued — so it cannot be used as an open relay for arbitrary
signed transactions, and the verification an agent under time pressure would
drop is structurally impossible to skip. Gecko still never signs and never holds
a key: the caller brings bytes their own wallet signed; this is transport for a
decision the receipt already attested.

The rebroadcast loop is the measured fix for one-shot sends on public RPC
(mainnet tx #24's first attempt expired unlanded; the identical bytes
rebroadcast every ~1.5s landed in seconds). Resubmitting the same signed bytes
is idempotent — same signature — so the loop carries no double-spend risk by
construction.
"""

from __future__ import annotations

import ipaddress
import time
import urllib.parse
from typing import Any, Callable, Mapping

from .rpc import RpcCall, RpcError, default_rpc_call
from .tools import tool_annotations
from .verify_signed import verify_signed

__all__ = [
    "SUBMIT_TRANSACTION_TOOL",
    "submit_transaction_result",
]

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
_REBROADCAST_EVERY_S = 1.5
#: Hard wall-clock cap — the block-height budget is the real stop, this is the
#: backstop against a stalled RPC keeping the tool call open forever.
_MAX_SECONDS = 120


class SubmitRefused(Exception):
    """The submission was refused before anything reached the network."""


def _ensure_public_rpc(url: str) -> str:
    """An ``rpc_url`` the CALLER supplies is untrusted input: http(s) only and no
    private/loopback/link-local hosts (SSRF is the same hazard here as in ingest)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SubmitRefused(f"rpc_url must be an http(s) URL, got {url!r}")
    try:
        addr = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return url  # a hostname; the public resolvers decide
    if not addr.is_global:
        raise SubmitRefused("rpc_url points at a private or local address")
    return url


def _refuse(code: str, reason: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"refused": True, "code": code, "reason": reason}
    out.update(extra)
    return out


def submit_transaction_result(
    arguments: Any,
    *,
    rpc_call: RpcCall | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Verify → send → rebroadcast → confirm, as one call. Never raises; every
    failure is a structured refusal or an honest ``{expired: true, spent: false}``."""
    args: Mapping[str, Any] = arguments if isinstance(arguments, dict) else {}
    transaction = str(args.get("transaction") or "")
    binding = str(args.get("binding") or "")
    last_valid_raw = args.get("last_valid_block_height")
    if not transaction:
        return _refuse("transaction-required", "no signed transaction to submit")
    if not binding:
        return _refuse(
            "binding-required",
            "this tool broadcasts ONLY bytes that verify against the binding a "
            "Gecko receipt issued — pass the `binding` from the same result that "
            "gave you the transaction. Without it there is nothing tying these "
            "bytes to a simulation that passed, and Gecko does not relay "
            "unverified transactions",
        )
    if not isinstance(last_valid_raw, int) or isinstance(last_valid_raw, bool):
        return _refuse(
            "expiry-required",
            "pass `last_valid_block_height` from the prepare result's `expires` — "
            "it is what lets this tool stop honestly instead of rebroadcasting a "
            "dead transaction forever",
        )
    last_valid = int(last_valid_raw)

    try:
        rpc_url = _ensure_public_rpc(str(args.get("rpc_url") or DEFAULT_RPC))
    except SubmitRefused as exc:
        return _refuse("rpc-url-refused", str(exc))
    call = rpc_call or default_rpc_call

    # 1. THE NON-DROPPABLE CHECK — exact binding, before anything touches the wire.
    verdict = verify_signed(transaction=transaction, binding=binding)
    if not verdict.verified:
        return _refuse(
            "verify-failed",
            f"the signed bytes do not verify against the binding: {verdict.reason}",
            verify={
                "verified": verdict.verified,
                "binding_matches": verdict.binding_matches,
                "signed": verdict.signed,
            },
        )

    # 2. FIRST SEND — preflight on, our own retries off (the loop below is the retry).
    try:
        first = call(
            rpc_url,
            "sendTransaction",
            [
                transaction,
                {
                    "encoding": "base64",
                    "preflightCommitment": "confirmed",
                    "maxRetries": 0,
                },
            ],
        )
    except RpcError as exc:
        return _refuse("send-failed", f"sendTransaction was rejected: {exc}")
    if "error" in first and first["error"]:
        return _refuse(
            "send-failed",
            f"sendTransaction was rejected: {first['error']}",
        )
    signature = str(first.get("result") or "")
    if not signature:
        return _refuse("send-failed", "sendTransaction returned no signature")

    # 3. REBROADCAST UNTIL CONFIRMED OR THE BUDGET IS SPENT. Same bytes, same
    #    signature — idempotent by construction.
    started = clock()
    while clock() - started < _MAX_SECONDS:
        sleep(_REBROADCAST_EVERY_S)
        try:
            call(
                rpc_url,
                "sendTransaction",
                [
                    transaction,
                    {"encoding": "base64", "skipPreflight": True, "maxRetries": 0},
                ],
            )
        except RpcError:
            pass  # a failed rebroadcast is retried next tick; the status poll decides
        try:
            statuses = call(rpc_url, "getSignatureStatuses", [[signature]])
        except RpcError:
            continue
        status = ((statuses.get("result") or {}).get("value") or [None])[0]
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            return {
                "refused": False,
                "confirmed": True,
                "signature": signature,
                "slot": status.get("slot"),
                "err": status.get("err"),
                "confirmation_status": status.get("confirmationStatus"),
            }
        try:
            height_res = call(rpc_url, "getBlockHeight", [{"commitment": "confirmed"}])
        except RpcError:
            continue
        height = height_res.get("result")
        if isinstance(height, int) and height > last_valid + 5:
            return {
                "refused": False,
                "confirmed": False,
                "expired": True,
                "spent": False,
                "signature": signature,
                "reason": (
                    f"the blockhash budget is spent (height {height} > "
                    f"{last_valid}); this transaction can no longer land and no "
                    "funds moved. Re-run the prepare step for fresh bytes and "
                    "sign THOSE — never re-submit these"
                ),
            }
    return {
        "refused": False,
        "confirmed": False,
        "expired": False,
        "signature": signature,
        "reason": (
            f"gave up polling after {_MAX_SECONDS}s with the transaction neither "
            "confirmed nor expired — check getSignatureStatuses for this "
            "signature before doing ANYTHING else; it may still land"
        ),
    }


SUBMIT_TRANSACTION_TOOL: dict[str, Any] = {
    "name": "submit_transaction",
    "annotations": tool_annotations(
        read_only=False,
        destructive=True,
        idempotent=True,
        open_world=True,
        title="Submit a verified transaction",
    ),
    "description": (
        "Broadcast a SIGNED transaction that Gecko prepared, with the checks and "
        "the rebroadcast loop built in: verifies the bytes against the receipt's "
        "`binding` from the prepare result (re-verified here at exact strength before anything is sent, and refused without it, so this cannot relay "
        "arbitrary transactions), sends with maxRetries 0, rebroadcasts the same "
        "bytes every ~1.5s (idempotent - same signature) until confirmed or the "
        "block height passes `last_valid_block_height`, and returns {signature, "
        "slot, confirmed} or an honest {expired: true, spent: false}. Use this "
        "instead of hand-rolling sendTransaction: a one-shot send on public RPC "
        "routinely expires unlanded (measured), and this is the flow's last mile "
        "for agents with no shell. Gecko still never signs: you bring bytes your "
        "own wallet signed."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "transaction": {
                "type": "string",
                "description": "base64 of the SIGNED transaction your signer returned",
            },
            "binding": {
                "type": "string",
                "description": "the `binding` from the prepare result whose receipt attested these bytes (required)",
            },
            "last_valid_block_height": {
                "type": "integer",
                "description": "`expires.last_valid_block_height` from the same prepare result (required)",
            },
            "rpc_url": {
                "type": "string",
                "description": "optional http(s) RPC endpoint; defaults to the public mainnet RPC",
            },
        },
        "required": ["transaction", "binding", "last_valid_block_height"],
    },
}

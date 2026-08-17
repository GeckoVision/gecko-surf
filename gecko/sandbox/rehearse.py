"""The whole purchase, rehearsed on a fork — and JUDGED by what actually moved.

WHAT THIS ADDS THAT NOTHING ELSE IN THE REPO HAS. Every other path stops at a receipt:
"these bytes would land against the state observed at that slot". That is a statement
about form. It is silent on the only question a buyer has — *does the money go where the
plan says it goes, and how much of it* — because answering that requires the transaction
to actually execute, and until now nothing here signed anything. So a rehearsal signs, on
a local fork of mainnet, with a key that cannot exist until the RPC has proved it is not
mainnet, and then reads the LEDGER: the buyer's token account fell by exactly the price,
the store's rose by exactly the price, the fee payer lost exactly the fee, and the program
wrote a receipt row naming this buyer. Not "which accounts are writable" — what changed,
and by how much.

THE SIX STEPS, and the second one is the point::

    FUND     cheatcodes give the buyer EXACTLY the price, plus SOL for the fee
    PREPARE  gecko.prepare_purchase — THE PRODUCTION PATH, UNCHANGED
    SIGN     the ephemeral key, which only exists because the fork proved itself
    LAND     sendTransaction at the PROVEN endpoint, then confirm
    JUDGE    the four deltas and the receipt the program wrote
    RESET    surfnet_resetAccount, so the next rehearsal starts clean

Step 2 calls :func:`gecko.prepare_purchase.prepare_purchase_result` — the same function
the public MCP surface serves, with its own store read, its own plan refusals, its own
builder, its own blockhash patch, its own simulation and its own binding check. It is not
reimplemented here and it is not copied. A rehearsal of a *copy* proves something about
the copy. The single accommodation it needed was a seam for the URL guard
(:data:`gecko.prepare_purchase.UrlGuard`), because that path correctly refuses loopback
and a fork lives on ``127.0.0.1``; the guard handed to it here is STRICTER than the
default, admitting exactly one URL — the surfnet that proved itself.

THREE BINDINGS TIE THE SIGNATURE TO THE FORK, and each is checked before anything leaves:

* the signer's :attr:`~gecko.sandbox.surfnet.EphemeralSigner.rpc_url` must be the proof's
  — a key proven against one fork may not be used against another;
* ``submit.rpc_url``, which the production path chose, must be the proof's — this is what
  stops bytes verified on a fork from being aimed at mainnet;
* the transaction's fee payer must be the ephemeral pubkey — signing a message that pays
  from someone else's account produces a signature the network rejects, and asking why
  after the fact is the expensive way to learn it.

WHAT A GREEN REHEARSAL DOES NOT PROVE. Measured on 2026-08-17 against surfpool 1.1.1,
same buyer, same store, same product, same minute: **mainnet simulated 36,399 CU and the
fork simulated 42,494** — the same 42,494 it then charged when the transaction landed. So
the fork is self-consistent and 16.7% away from the chain it forks. A fork is not a
compute oracle, and a CU budget sized here is sized wrong. Two more asymmetries are worth
the same suspicion: :func:`~gecko.sandbox.cheatcodes.fund_token` creates the buyer's ATA
out of nothing, so a flow that never creates one passes here and fails live (and the
~0.00204 SOL of rent a real buyer pays never appears in the SOL delta); and the fork's
block height tracks its own produced blocks rather than mainnet's, so the expiry window
this path reports is not the window a real buyer races.

Control-plane invariant #1 holds throughout. Nothing is written to disk. The key is
ephemeral and never leaves memory. The receipts vec is walked with the same bounds-checked
cursor :func:`gecko.store_directory.decode_store` uses, and the ONLY row it returns is the
one whose buyer is the ephemeral key this process generated — every other buyer's bytes
are compared and dropped, never carried out.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..landing import RPC_COMMITMENT, TOKEN_PROGRAM_ID
from ..netguard import UnsafeUrlError
from ..prepare_purchase import UrlGuard, prepare_purchase_result
from ..rpc import RpcCall, default_rpc_call
from ..simulate import BuildCall
from ..store_accounts import derive_ata, receipts_pda, resolve_store
from ..store_directory import StoreDecodeError, _Cursor
from .cheatcodes import (
    _decode_token_account,
    fund_sol,
    fund_token,
    reset_account,
)
from .surfnet import EphemeralSigner, SandboxError, SurfnetProof

__all__ = [
    "LamportDelta",
    "Refusal",
    "Rehearsal",
    "RehearsalError",
    "TokenDelta",
    "WrittenReceipt",
    "rehearse_purchase",
]

#: SOL handed to the buyer for fees. Generous on purpose — the point of the rehearsal is
#: the token ledger, and an under-funded fee payer turns every run into the same boring
#: failure. It is also why the SOL delta is REPORTED rather than asserted: on a fork this
#: number is a choice, not a fact about the world.
DEFAULT_FEE_LAMPORTS = 50_000_000

#: How long to wait for the fork to confirm. A local validator answers in a second or two;
#: this is the ceiling before the run says "sent, not confirmed" instead of hanging.
CONFIRM_TIMEOUT_SECONDS = 30.0


class RehearsalError(SandboxError):
    """The rehearsal could not be attempted at all — a binding that does not hold.

    Distinct from a :class:`Refusal`, which is a step the run DECLINED and recorded while
    still producing a result. This is raised for the three bindings in the module
    docstring: they are wrong-by-construction, and continuing past one would mean signing
    or sending against something other than the proven fork.
    """


@dataclass(frozen=True)
class Refusal:
    """Something the run would not do, and why. Carried in the result, never swallowed."""

    step: str
    reason: str


@dataclass(frozen=True)
class TokenDelta:
    """One SPL token account across the transaction, decoded from its own bytes.

    ``None`` on either side means the account did not exist at that moment, which is a
    different fact from zero and is kept different.
    """

    account: str
    owner: str
    mint: str
    before: int | None
    after: int | None

    @property
    def moved(self) -> int | None:
        """``after - before``, or ``None`` if either side was absent."""
        if self.before is None or self.after is None:
            return None
        return self.after - self.before


@dataclass(frozen=True)
class LamportDelta:
    """An address's SOL across the transaction — the fee, and anything else that moved."""

    address: str
    before: int | None
    after: int | None

    @property
    def moved(self) -> int | None:
        if self.before is None or self.after is None:
            return None
        return self.after - self.before


@dataclass(frozen=True)
class WrittenReceipt:
    """The row the PROGRAM wrote, read back out of the store's own account.

    This is the half of "did it work" that a simulation cannot answer: the program's own
    record of the sale, in its own storage, after it ran.

    Only the row whose buyer equals the ephemeral pubkey is returned, and the buyer field
    itself is deliberately absent — it is compared and dropped. Every other row belongs to
    a real customer of a real storefront, and the fork mirrors their bytes faithfully.
    """

    receipt_id: int
    product_name: str
    price_raw: int
    table_number: int
    delivered: bool
    #: ``receipts.total_purchases`` on either side. The counter is the store's own, so a
    #: purchase that did not increment it is a purchase the program did not record.
    total_purchases_before: int | None
    total_purchases_after: int | None


@dataclass(frozen=True)
class Rehearsal:
    """What the loop did and what the chain shows for it. Every field is measured.

    :attr:`discrepancies` is the one to read first. A run can land, confirm, and still be
    wrong — a price that does not match, a credit that does not equal the debit, a receipt
    the program never wrote — and each of those lands here as a sentence rather than being
    smoothed into a boolean.
    """

    store: str
    product: str
    #: The price the STORE's own account declares, in the mint's base units. Everything
    #: the ledger is judged against is this number; nothing here supplies a price.
    price_raw: int
    mint: str
    buyer: str
    landed: bool
    signature: str | None = None
    #: What ``prepare_purchase``'s simulation predicted, and what the chain then charged.
    #: Kept apart on purpose: they agreed exactly on the fork, and the pair is the only
    #: evidence for that claim.
    simulated_units: int | None = None
    units_consumed: int | None = None
    fee_lamports: int | None = None
    buyer_token: TokenDelta | None = None
    store_token: TokenDelta | None = None
    buyer_sol: LamportDelta | None = None
    receipt: WrittenReceipt | None = None
    #: ``expires.blocks_remaining`` as the production path computed it, recorded because
    #: on a fork it is NOT the window a real buyer races (see the module docstring).
    window_blocks: int | None = None
    refusals: tuple[Refusal, ...] = ()
    #: The ledger's own objections. Empty is the only passing value.
    discrepancies: tuple[str, ...] = ()
    #: Addresses whose local state was dropped at the end, with what they read afterwards.
    reset: tuple[str, ...] = ()

    @property
    def balanced(self) -> bool | None:
        """Did the buyer fall by exactly the price and the store rise by exactly it?

        ``None`` when the transaction never landed — an unanswered question, which is not
        the same as a negative answer and must not render as one.
        """
        if not self.landed:
            return None
        return not self.discrepancies


def rehearse_purchase(
    proof: SurfnetProof,
    *,
    buyer: EphemeralSigner,
    store: str,
    product: str,
    table_number: int = 0,
    fee_lamports: int = DEFAULT_FEE_LAMPORTS,
    rpc_call: RpcCall | None = None,
    build_call: BuildCall | None = None,
) -> Rehearsal:
    """Fund, prepare, sign, land, judge and reset — one purchase, on a proven surfnet.

    ``buyer`` is an :class:`~gecko.sandbox.surfnet.EphemeralSigner`, not a pubkey: the
    address that pays and the key that signs must be the same thing, and passing a string
    would let them drift. Its ``rpc_url`` must be ``proof.rpc_url``.

    ``store``/``product`` name a real storefront; the PRICE is never a parameter. It is
    read from the store's own account, funded to the base unit, and then used as the
    number the ledger is judged against — a price supplied here would be a price this
    function checks against itself.

    ``rpc_call``/``build_call`` are the injected seams, so the loop is falsifiable offline
    with a fake transport and no validator.

    :raises RehearsalError: if any of the three fork bindings does not hold.
    :raises CheatcodeError: if a cheatcode reported success and the state disagrees.
    """
    if buyer.rpc_url != proof.rpc_url:
        raise RehearsalError(
            f"this signer was proven against {buyer.rpc_url} and the proof names "
            f"{proof.rpc_url} — a key is bound to the ONE fork that proved itself, and "
            "using it against another endpoint is the exact move this package exists to "
            "make unrepresentable"
        )
    # The store, its product and its price — read from the fork, which mirrors mainnet.
    # Deliberately the same `resolve_store` the production path will call again in step 2:
    # the price is needed BEFORE preparing (to fund), and re-reading is a read, not a guess.
    resolved = resolve_store(store, rpc_url=proof.rpc_url, rpc_call=rpc_call)
    listed = resolved.accounts_for(product).product
    touched = (
        buyer.pubkey,
        derive_ata(buyer.pubkey, listed.mint, token_program=TOKEN_PROGRAM_ID),
        derive_ata(resolved.authority, listed.mint),
        receipts_pda(resolved.store_name),
    )
    blank = Rehearsal(
        store=resolved.store_name,
        product=listed.name,
        price_raw=listed.price_raw,
        mint=listed.mint,
        buyer=buyer.pubkey,
        landed=False,
    )

    # 6. RESET runs in a `finally` — and the result is only assembled AFTER it, so the
    # cleanup is part of what the caller receives rather than something that happened to
    # a value already returned. A run that refused at step 2 has still funded, and leaving
    # that behind means the NEXT rehearsal starts from a state nobody chose.
    try:
        result = _run(
            blank,
            proof=proof,
            buyer=buyer,
            authority=resolved.authority,
            listed_mint=listed.mint,
            table_number=table_number,
            fee_lamports=fee_lamports,
            touched=touched,
            call=rpc_call or default_rpc_call,
            rpc_call=rpc_call,
            build_call=build_call,
        )
    finally:
        # Measured: this reverts TRANSACTION writes too, not only cheatcode overrides —
        # the receipts vec went back to 20 rows and total_purchases 128 -> 127.
        cleaned = _reset_all(proof, touched, rpc_call)
    return _with(result, reset=cleaned)


def _run(
    blank: Rehearsal,
    *,
    proof: SurfnetProof,
    buyer: EphemeralSigner,
    authority: str,
    listed_mint: str,
    table_number: int,
    fee_lamports: int,
    touched: tuple[str, ...],
    call: RpcCall,
    rpc_call: RpcCall | None,
    build_call: BuildCall | None,
) -> Rehearsal:
    """Steps 1 to 5. Split out so RESET can be a ``finally`` around the whole of it."""
    _, buyer_ata, store_ata, receipts = touched

    # 1. FUND — exactly the price, so an over- or under-transfer cannot hide in slack.
    fund_token(proof, buyer.pubkey, listed_mint, blank.price_raw, rpc_call=rpc_call)
    fund_sol(proof, buyer.pubkey, fee_lamports, rpc_call=rpc_call)

    # 2. PREPARE — the production path, unchanged.
    prepared = prepare_purchase_result(
        {
            "store": blank.store,
            "product": blank.product,
            "buyer": buyer.pubkey,
            "network": "fork",
            "rpc_url": proof.rpc_url,
            "table": table_number,
        },
        rpc_call=rpc_call,
        build_call=build_call,
        url_guard=_only_this_surfnet(proof),
    )
    error = prepared.get("error")
    if error:
        return _with(
            blank, refusals=(Refusal("prepare", f"the path errored: {error}"),)
        )
    if prepared.get("refused"):
        return _with(
            blank,
            refusals=(
                Refusal(
                    "prepare", f"[{prepared.get('code')}] {prepared.get('reason')}"
                ),
            ),
        )
    unsigned = (prepared.get("transaction") or {}).get("unsigned_transaction")
    submit = prepared.get("submit") or {}
    if not isinstance(unsigned, str) or not unsigned:
        return _with(
            blank,
            refusals=(Refusal("prepare", "approved but returned no transaction"),),
        )
    # THE BINDING THAT KEEPS A SIGNATURE ON THE FORK. The production path chose this URL;
    # if it is not the endpoint that proved itself, the bytes do not get signed.
    if submit.get("rpc_url") != proof.rpc_url:
        raise RehearsalError(
            f"the prepared transaction says to submit at {submit.get('rpc_url')!r}, "
            f"which is not the proven surfnet {proof.rpc_url} — refusing to sign bytes "
            "destined for an endpoint that has proved nothing"
        )

    # 3. SIGN.
    signed, _signature = _sign(unsigned, buyer)

    # The BEFORE half of the judgement, taken at the last possible moment before landing.
    before = _ledger(call, proof.rpc_url, buyer.pubkey, buyer_ata, store_ata, receipts)

    # 4. LAND.
    sent = call(
        proof.rpc_url,
        "sendTransaction",
        [
            signed,
            {
                "encoding": "base64",
                # The blockhash was issued at `confirmed`; sendTransaction's preflight
                # defaults to the STRICTER `finalized` and rejects these bytes every time
                # with a BlockhashNotFound that names neither cause nor remedy. The
                # production path says so in `submit.preflight_commitment`; this reads it
                # from there rather than restating the value.
                "preflightCommitment": submit.get(
                    "preflight_commitment", RPC_COMMITMENT
                ),
                "maxRetries": 3,
            },
        ],
    )
    landed_signature = (sent or {}).get("result")
    if not isinstance(landed_signature, str):
        return _with(
            blank,
            refusals=(Refusal("land", f"the node returned no signature: {sent!r}"),),
        )
    if not _confirm(call, proof.rpc_url, landed_signature):
        return _with(
            blank,
            signature=landed_signature,
            refusals=(
                Refusal(
                    "land",
                    f"sent {landed_signature} and it did not confirm within "
                    f"{CONFIRM_TIMEOUT_SECONDS:.0f}s — nothing below is evidence",
                ),
            ),
        )

    # 5. JUDGE — what actually moved.
    after = _ledger(call, proof.rpc_url, buyer.pubkey, buyer_ata, store_ata, receipts)
    return _judge(
        blank,
        signature=landed_signature,
        before=before,
        after=after,
        meta=_transaction_meta(call, proof.rpc_url, landed_signature),
        simulated_units=prepared.get("units_consumed"),
        window_blocks=(prepared.get("expires") or {}).get("blocks_remaining"),
        buyer_ata=buyer_ata,
        store_ata=store_ata,
        buyer=buyer.pubkey,
        authority=authority,
        mint=listed_mint,
    )


def _only_this_surfnet(proof: SurfnetProof) -> UrlGuard:
    """A URL guard that admits ONE address: the endpoint that proved itself.

    Strictly narrower than the SSRF guard it stands in for — that one admits every public
    host on the internet and refuses loopback; this one refuses everything except a single
    string. It raises :class:`~gecko.netguard.UnsafeUrlError` because that is the type the
    production path already catches, so a mismatch comes back as that path's own
    ``argument-invalid`` refusal rather than as an exception from inside a rehearsal.
    """

    def guard(url: str) -> None:
        if url != proof.rpc_url:
            raise UnsafeUrlError(
                f"{url} is not the surfnet that proved itself ({proof.rpc_url}); a "
                "rehearsal reaches exactly one endpoint"
            )

    return guard


def _sign(unsigned_base64: str, buyer: EphemeralSigner) -> tuple[str, str]:
    """(signed base64, signature base58) — over the bytes the path handed back.

    The message is taken from the transaction the production path returned and nothing is
    re-assembled: re-deriving a message from parts is how a signature ends up covering
    something other than what was verified.

    The fee payer is checked against the signing key HERE, before the signature exists.
    Solana takes the first account key as the fee payer, so a mismatch produces a
    perfectly valid signature that the network rejects — a failure that costs a round trip
    to diagnose and reads like a node problem.
    """
    from solders.signature import Signature
    from solders.transaction import Transaction

    transaction = Transaction.from_bytes(base64.b64decode(unsigned_base64))
    message = transaction.message
    fee_payer = str(message.account_keys[0])
    if fee_payer != buyer.pubkey:
        raise RehearsalError(
            f"the prepared transaction pays from {fee_payer}, which is not the ephemeral "
            f"signer {buyer.pubkey} — signing it would produce a signature the network "
            "rejects"
        )
    signature = Signature.from_bytes(buyer.sign(bytes(message)))
    signed = Transaction.populate(message, [signature])
    return base64.b64encode(bytes(signed)).decode(), str(signature)


@dataclass(frozen=True)
class _Ledger:
    """One reading of every account this purchase can move. Public state, never stored."""

    buyer_lamports: int | None
    buyer_token: int | None
    store_token: int | None
    total_purchases: int | None
    receipts_raw: bytes | None = field(default=None, repr=False)


def _account(call: RpcCall, rpc_url: str, address: str) -> Mapping[str, Any] | None:
    response = call(rpc_url, "getAccountInfo", [address, {"encoding": "base64"}])
    value = (response.get("result") or {}).get("value")
    return value if isinstance(value, Mapping) else None


def _ledger(
    call: RpcCall,
    rpc_url: str,
    buyer: str,
    buyer_ata: str,
    store_ata: str,
    receipts: str,
) -> _Ledger:
    """Read all four accounts at one moment. Four targeted reads, no program scan."""
    buyer_account = _account(call, rpc_url, buyer)
    buyer_token = _account(call, rpc_url, buyer_ata)
    store_token = _account(call, rpc_url, store_ata)
    receipts_account = _account(call, rpc_url, receipts)
    raw = _raw_data(receipts_account)
    return _Ledger(
        buyer_lamports=_lamports(buyer_account),
        buyer_token=_decode_token_account(buyer_token)[2],
        store_token=_decode_token_account(store_token)[2],
        total_purchases=_total_purchases(raw),
        receipts_raw=raw,
    )


def _lamports(account: Mapping[str, Any] | None) -> int | None:
    if account is None:
        return None
    value = account.get("lamports")
    return value if isinstance(value, int) else None


def _raw_data(account: Mapping[str, Any] | None) -> bytes | None:
    if account is None:
        return None
    data = account.get("data")
    encoded = data[0] if isinstance(data, list) and data else data
    if not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded)
    except (ValueError, TypeError):
        return None


def _walk_receipts(raw: bytes) -> tuple[list[dict[str, Any]], int]:
    """Every receipt row's NON-BUYER fields plus its buyer, and ``total_purchases``.

    The walk is the one :func:`gecko.store_directory.decode_store` performs and skips, run
    with that module's own bounds-checked cursor rather than a second implementation of
    the byte math — the layout has exactly one owner. What this adds is that the rows are
    kept for the length of one comparison; :func:`_written_receipt` drops every row that
    is not the ephemeral buyer's, and no other buyer's bytes leave this function's caller.
    """
    cursor = _Cursor(raw, 8)  # the Anchor discriminator
    rows = cursor.u32()
    if rows > 10_000:
        raise StoreDecodeError("the receipts vec declares an implausible length")
    out: list[dict[str, Any]] = []
    for _ in range(rows):
        receipt_id = cursor.u64()
        buyer = cursor.pubkey_base58()
        delivered = cursor.u8()
        price = cursor.u64()
        cursor.u64()  # timestamp — the fork's clock, not evidence of anything
        table = cursor.u8()
        name = cursor.string()
        out.append(
            {
                "receipt_id": receipt_id,
                "buyer": buyer,
                "delivered": bool(delivered),
                "price_raw": price,
                "table_number": table,
                "product_name": name,
            }
        )
    return out, cursor.u64()


def _total_purchases(raw: bytes | None) -> int | None:
    if raw is None:
        return None
    try:
        return _walk_receipts(raw)[1]
    except StoreDecodeError:
        return None


def _written_receipt(
    before: _Ledger, after: _Ledger, buyer: str
) -> tuple[WrittenReceipt | None, str | None]:
    """(the row the program wrote for THIS buyer, refusal reason).

    Takes the LAST matching row: the vec is a rolling window (measured: it holds 20 rows
    and drops the oldest), so a buyer who has been here before appears more than once and
    the newest is the one this run caused.
    """
    if after.receipts_raw is None:
        return None, "the store account could not be read after the purchase"
    try:
        rows, total_after = _walk_receipts(after.receipts_raw)
    except StoreDecodeError as exc:
        return None, f"the receipts vec did not decode after the purchase: {exc}"
    mine = [row for row in rows if row["buyer"] == buyer]
    if not mine:
        return None, (
            "the transaction confirmed but the store's receipts vec carries no row for "
            "this buyer — the program did not record the sale"
        )
    row = mine[-1]
    return (
        WrittenReceipt(
            receipt_id=int(row["receipt_id"]),
            product_name=str(row["product_name"]),
            price_raw=int(row["price_raw"]),
            table_number=int(row["table_number"]),
            delivered=bool(row["delivered"]),
            total_purchases_before=before.total_purchases,
            total_purchases_after=total_after,
        ),
        None,
    )


def _confirm(call: RpcCall, rpc_url: str, signature: str) -> bool:
    """Poll until the fork confirms, or give up and say so. Never claims a timeout landed."""
    deadline = time.monotonic() + CONFIRM_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = call(
            rpc_url,
            "getSignatureStatuses",
            [[signature], {"searchTransactionHistory": True}],
        )
        values = (response.get("result") or {}).get("value") or [None]
        status = values[0]
        if isinstance(status, Mapping):
            if status.get("err") is not None:
                return False
            if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                return True
        time.sleep(0.5)
    return False


def _transaction_meta(
    call: RpcCall, rpc_url: str, signature: str
) -> Mapping[str, Any] | None:
    """The landed transaction's ``meta`` — the CU the chain CHARGED, and the fee it took."""
    response = call(
        rpc_url,
        "getTransaction",
        [
            signature,
            {
                "encoding": "json",
                "commitment": RPC_COMMITMENT,
                "maxSupportedTransactionVersion": 0,
            },
        ],
    )
    result = response.get("result")
    if not isinstance(result, Mapping):
        return None
    meta = result.get("meta")
    return meta if isinstance(meta, Mapping) else None


def _judge(
    partial: Rehearsal,
    *,
    signature: str,
    before: _Ledger,
    after: _Ledger,
    meta: Mapping[str, Any] | None,
    simulated_units: Any,
    window_blocks: Any,
    buyer_ata: str,
    store_ata: str,
    buyer: str,
    authority: str,
    mint: str,
) -> Rehearsal:
    """Turn two readings into deltas, and the deltas into a verdict that can say NO.

    The two assertions are stated as arithmetic on the store's OWN declared price, and
    each failure is its own sentence: "-100000 but the price is 99000" and "the buyer paid
    and the store was not credited" are different defects, and collapsing them into one
    boolean throws away which one happened.
    """
    buyer_token = TokenDelta(
        account=buyer_ata,
        owner=buyer,
        mint=mint,
        before=before.buyer_token,
        after=after.buyer_token,
    )
    store_token = TokenDelta(
        account=store_ata,
        owner=authority,
        mint=mint,
        before=before.store_token,
        after=after.store_token,
    )
    buyer_sol = LamportDelta(
        address=buyer, before=before.buyer_lamports, after=after.buyer_lamports
    )
    receipt, receipt_refusal = _written_receipt(before, after, buyer)

    price = partial.price_raw
    problems: list[str] = []
    if buyer_token.moved != -price:
        problems.append(
            f"the buyer's token account moved {buyer_token.moved!r}, not {-price} — the "
            f"store's own account prices {partial.product!r} at {price}"
        )
    if store_token.moved != price:
        problems.append(
            f"the store's token account moved {store_token.moved!r}, not {price}"
        )
    if (
        buyer_token.moved is not None
        and store_token.moved is not None
        and buyer_token.moved + store_token.moved != 0
    ):
        # Stated separately from the two above because it is a different claim: the two
        # sides can each be wrong by the same amount and still balance, and they can each
        # equal the price while a third account is credited. Both readings matter.
        problems.append(
            f"the debit and the credit do not cancel: {buyer_token.moved} + "
            f"{store_token.moved} != 0 — some other account took the difference"
        )
    if receipt_refusal:
        problems.append(receipt_refusal)
    elif receipt is not None and receipt.price_raw != price:
        problems.append(
            f"the receipt the program wrote records {receipt.price_raw}, not {price}"
        )
    return _with(
        partial,
        landed=True,
        signature=signature,
        simulated_units=simulated_units if isinstance(simulated_units, int) else None,
        units_consumed=_int_or_none(meta, "computeUnitsConsumed"),
        fee_lamports=_int_or_none(meta, "fee"),
        buyer_token=buyer_token,
        store_token=store_token,
        buyer_sol=buyer_sol,
        receipt=receipt,
        window_blocks=window_blocks if isinstance(window_blocks, int) else None,
        discrepancies=tuple(problems),
    )


def _int_or_none(meta: Mapping[str, Any] | None, key: str) -> int | None:
    if meta is None:
        return None
    value = meta.get(key)
    return value if isinstance(value, int) else None


def _reset_all(
    proof: SurfnetProof, addresses: tuple[str, ...], rpc_call: RpcCall | None
) -> tuple[str, ...]:
    """Drop every local override, and report each one as a line rather than a boolean.

    A failure here is recorded, never raised: the run is over, and an exception thrown out
    of a ``finally`` would replace whatever the rehearsal actually found with a cleanup
    problem.
    """
    lines: list[str] = []
    for address in addresses:
        try:
            outcome = reset_account(proof, address, rpc_call=rpc_call)
        except Exception as exc:  # noqa: BLE001 - see docstring
            lines.append(f"{address}: reset FAILED ({type(exc).__name__}: {exc})")
            continue
        lines.append(
            f"{address}: {outcome.lamports_before} -> {outcome.lamports_after} lamports"
        )
    return tuple(lines)


def _with(rehearsal: Rehearsal, **changes: Any) -> Rehearsal:
    """A frozen dataclass, advanced. Kept explicit so nothing mutates a result in place."""
    from dataclasses import replace

    return replace(rehearsal, **changes)

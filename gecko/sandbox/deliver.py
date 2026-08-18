"""A delivery, rehearsed on a fork — and the transaction that SUCCEEDS while doing nothing.

WHAT THIS ADDS THAT :mod:`gecko.sandbox.rehearse` DOES NOT. A purchase moves money, so the
ledger can be interrogated afterwards and it answers loudly: the buyer fell by the price,
the store rose by it, or something is wrong. ``mark_as_delivered`` moves nothing. Its
entire effect is one boolean inside the store's own account, and the program's own
implementation is a SCAN: it walks the receipts vec looking for ``receipt_id``, flips
``was_delivered`` if it finds it, and — measured, and this is the finding — returns ``Ok``
if it does not. No error, no event, no log. The signature confirms, the fee is charged,
the CU is reported, and a venue's delivery queue silently keeps an order it believes it
has just fulfilled.

That failure is invisible to every other path in this repo, and it is invisible for a
structural reason: a simulation attests that bytes would land, and these bytes DO land. So
the only instrument that can see it is one that executes the transaction and then reads the
row back. That is this module, and :attr:`Delivery.silently_did_nothing` is the field it
exists to set.

WHY THE ROW GOES MISSING IN THE FIRST PLACE. The receipts vec is capped at 20 and the
account never reallocs (fixed 3,681 bytes). At the cap ``make_purchase`` does
``receipts.remove(0)`` — the oldest receipt is evicted with no error, no event and no log.
A store with 125 purchases holds 20; the other 105 are gone from chain, and every one of
their ids now marks-as-delivered successfully and does nothing.

THE SIX STEPS, and the first one is the one that needs the justification::

    TAKE     rewrite the store's `authority` field to the ephemeral pubkey, with a
             cheatcode, and VERIFY IT BY READING IT BACK
    FUND     lamports for the fee, nothing else — this instruction moves no tokens
    BUILD    mark_as_delivered, with ONE store name filling both the wire arg and the seed
    SIGN     the ephemeral key, which only exists because the fork proved itself
    LAND     sendTransaction at the PROVEN endpoint, then confirm
    READ     walk the receipts vec and report what the row actually says
    RESET    surfnet_resetAccount, so the merchant's real authority comes straight back

STEP 1 IS A FORK-ONLY ACT AND IT IS NOT A CAPABILITY ANYWHERE ELSE. The IDL declares
``authority`` ``writable, signer``, so rehearsing a delivery the way the surface describes
it means holding the merchant's key — which we do not, must not, and would not accept if
offered. On a local fork there is a third option that is neither: surfpool's
``surfnet_setAccount`` overwrites an account's bytes directly, so the store can be made to
name a key we DO hold. That is a local validator state edit, not a transaction: no
signature, no broadcast, and no counterpart anywhere on a real chain, where the authority
field can only be changed by the program and the program will only do it for the current
authority. Reading it back is not politeness — surfpool answers ``{"value": null}`` to
every successful write and silently ignores fields it does not recognise, so an unverified
take would make every step after it a lie told confidently.

AND THE STEP TURNED OUT NOT TO BE REQUIRED, WHICH IS ITSELF THE SECOND FINDING. Measured
2026-08-17 on a clean fork with no take at all: a freshly generated key with no
relationship to the storefront landed ``mark_as_delivered`` and flipped the row — 34,870
CU, ``err: null``, logs ending ``Marked purchase as delivered 126 19``. Anchor's ``signer``
flag checks that SOMEBODY signed, never WHO, and this instruction carries no ``require!``
against ``receipts.authority`` (the guard the same program does apply in ``add_product``,
``update_details``, ``update_telegram_channel``, ``delete_product`` and ``delete_store``).
So anyone can clear anyone's delivery queue for a 5,000-lamport fee, and the IDL shows
nothing an eye could catch. ``take_the_store=False`` rehearses exactly that, and the run
reports it as a discrepancy rather than as a footnote.

WHAT THE FORK STILL DOES NOT PROVE. The compute figure is the fork's, and the fork was
measured 16.7% away from mainnet on the purchase path; worse here, because compute for this
instruction is NOT constant — it scans the vec, so it tracks resident bytes (34,858 and
34,137 measured a day apart, both correct). :attr:`Delivery.resident_bytes` is reported
beside the CU for exactly that reason. And a take is not an authorisation: this says
nothing about whether the real merchant's key would have been accepted, only about what the
program does once a signature from the account's declared authority is present.

Control-plane invariant #1 holds throughout. Nothing is written to disk, the key is
ephemeral and never leaves memory, and the receipts vec is walked with the same
bounds-checked cursor :func:`gecko.store_directory.decode_store` uses. The walk must READ
every row to reach the one it was asked about — the vec is sequential and has no index —
and the ONLY thing that leaves is the delivery flag of the id the caller named. No buyer,
no price, no other row.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from typing import Any, Mapping

from ..fork_preflight import AccountState, surfnet_overlay
from ..landing import RPC_COMMITMENT, latest_blockhash
from ..rpc import RpcCall, default_rpc_call
from ..store_accounts import receipts_pda, resolve_store
from ..store_directory import (
    LET_ME_BUY_PROGRAM_ID,
    StoreDecodeError,
    authority_span,
    decode_store,
)
from .cheatcodes import CheatcodeError, fund_sol
from .rehearse import (
    CONFIRM_TIMEOUT_SECONDS,
    DEFAULT_FEE_LAMPORTS,
    Refusal,
    _account,
    _confirm,
    _int_or_none,
    _raw_data,
    _reset_all,
    _sign,
    _transaction_meta,
    _walk_receipts,
)
from .surfnet import EphemeralSigner, SandboxError, SurfnetProof

__all__ = [
    "MARK_AS_DELIVERED_DISCRIMINATOR",
    "Delivery",
    "DeliveryError",
    "TakenStore",
    "rehearse_delivery",
]

#: Anchor's eight-byte selector for ``mark_as_delivered`` — ``sha256("global:mark_as_
#: delivered")`` truncated to 8, and identical to the ``discriminator`` array the program's
#: own IDL declares. Pinned as a literal, exactly as ``make_purchase``'s is in
#: :mod:`gecko.autonomous_purchase`, so a rename upstream refuses here instead of quietly
#: encoding a call no dispatcher matches. A test asserts both derivations agree.
MARK_AS_DELIVERED_DISCRIMINATOR = bytes.fromhex("702ee6449861c23e")


class DeliveryError(SandboxError):
    """The rehearsal could not be attempted, or a step's own verification failed.

    Distinct from a :class:`~gecko.sandbox.rehearse.Refusal`, which is a step the run
    DECLINED and recorded while still producing a result. This is raised where continuing
    would mean reporting on state nobody established: a signer bound to another fork, a
    store account that is not this store's, a build whose seed and argument disagree.
    """


@dataclass(frozen=True)
class TakenStore:
    """The authority rewrite, with both sides read from the account rather than assumed.

    ``authority_before`` is the merchant's real pubkey as the forked chain has it — public
    chain state, already the ``authority`` field of every ``list_stores`` row.
    """

    address: str
    authority_before: str
    authority_after: str
    #: Bytes of the account outside the 32-byte authority span that changed. Empty is the
    #: only acceptable value: a "take" that also moved the receipts vec would invalidate
    #: every reading below, and a cheatcode is not owed the benefit of the doubt.
    collateral_changes: int
    data_len: int


@dataclass(frozen=True)
class Delivery:
    """What the delivery did — and, separately, what it CHANGED. They are not the same.

    :attr:`silently_did_nothing` is the field to read first. It is ``True`` when the
    transaction confirmed and the row was absent or unchanged, which is the program's
    measured behaviour on an evicted receipt id and reads from every other vantage point in
    this repo as a clean success.
    """

    store: str
    receipt_id: int
    #: The ephemeral key that signed as the store's authority. Fork-only, see the module
    #: docstring: on any real chain this is the merchant's key and nothing else.
    authority: str
    landed: bool
    #: The authority rewrite, or ``None`` when the run deliberately did not take the store
    #: (``take_the_store=False``) — in which case the signer is a stranger to this
    #: storefront and a flipped row is the program failing to check who signed.
    took_the_store: TakenStore | None = None
    signature: str | None = None
    #: What the chain CHARGED. Not constant for this instruction — it scans the vec, so it
    #: tracks :attr:`resident_bytes`, and a budget sized from one store is wrong for another.
    units_consumed: int | None = None
    fee_lamports: int | None = None
    #: Was there a row with this id in the vec AFTER the transaction? The whole question.
    receipt_found: bool = False
    #: The row's flag on either side. ``None`` means the row was not there at that moment,
    #: which is a different fact from ``False`` and is kept different.
    was_delivered_before: bool | None = None
    was_delivered_after: bool | None = None
    #: THE FINDING. True when the transaction succeeded and the row was absent or unchanged.
    silently_did_nothing: bool = False
    #: How many rows the vec held, and how many bytes the account carries. Reported beside
    #: the CU because compute here is a function of them.
    #: False when the store account could not be read AFTER the transaction. It is a field
    #: rather than an inference from a nullable count, because two of this module's answers
    #: turn on it and an adversarial pass produced both of them out of a transient RPC miss:
    #: a row that is genuinely ABSENT is a known fact and the finding, while an account that
    #: could not be READ is an unanswered question and neither.
    store_readable: bool = True
    resident_receipts: int | None = None
    resident_bytes: int | None = None
    refusals: tuple[Refusal, ...] = ()
    #: What the read-back objects to, one sentence each. Empty is the only passing value.
    discrepancies: tuple[str, ...] = ()
    #: Addresses whose local state was dropped at the end, with what they read afterwards.
    reset: tuple[str, ...] = ()

    @property
    def delivered(self) -> bool | None:
        """Did this run flip the row from not-delivered to delivered?

        ``None`` when the transaction never landed, and ``None`` when it landed but the
        store could not be read afterwards — both are unanswered questions, and neither
        may render as a negative answer. The second case is the one that bit: an
        adversarial pass showed a transient read miss reporting a delivery as not made,
        beside a `silently_did_nothing` it had also manufactured. An RPC hiccup must not
        be able to produce either half of this module's finding.
        """
        if not self.landed or not self.store_readable:
            return None
        return self.was_delivered_before is False and self.was_delivered_after is True


def rehearse_delivery(
    proof: SurfnetProof,
    *,
    authority: EphemeralSigner,
    store: str,
    receipt_id: int,
    take_the_store: bool = True,
    fee_lamports: int = DEFAULT_FEE_LAMPORTS,
    rpc_call: RpcCall | None = None,
) -> Delivery:
    """Take the store, fund, build, sign, land, read the row back, and reset.

    ``authority`` is an :class:`~gecko.sandbox.surfnet.EphemeralSigner`, not a pubkey: the
    account the store is made to name and the key that signs must be the same thing, and a
    string would let them drift. Its ``rpc_url`` must be ``proof.rpc_url``.

    ``store`` names a real storefront, read from its own account here exactly as
    :func:`~gecko.sandbox.rehearse.rehearse_purchase` reads it. ``receipt_id`` is a row id
    in that store's receipts vec — and an id that is NOT in it is the interesting input,
    not an error: the whole point is what the program does with one.

    ``take_the_store=False`` skips step 1 and signs with a key the store does NOT name.
    That exists because the answer turned out to matter: MEASURED 2026-08-17 on a clean
    fork, a freshly generated key with no relationship to the storefront landed
    ``mark_as_delivered`` and flipped the row (34,870 CU, clean logs, ``err: null``). The
    IDL marks ``authority`` ``writable, signer``, which makes Anchor check that SOMEBODY
    signed — never WHO — and this instruction carries no ``require!`` against
    ``receipts.authority``. So the take is a faithful model of a merchant delivering, and
    it is not a requirement the program imposes; the run reports that as a discrepancy
    rather than leaving it in a comment.

    :raises DeliveryError: if the signer is bound to another fork, if the receipt id is not
        a u64, or if a step's own verification fails.
    :raises CheatcodeError: if the take reported success and the account disagrees.
    """
    if authority.rpc_url != proof.rpc_url:
        raise DeliveryError(
            f"this signer was proven against {authority.rpc_url} and the proof names "
            f"{proof.rpc_url} — a key is bound to the ONE fork that proved itself, and "
            "using it against another endpoint is the exact move this package exists to "
            "make unrepresentable"
        )
    if receipt_id < 0 or receipt_id >= 2**64:
        raise DeliveryError(
            f"receipt_id {receipt_id} is not a u64, so no instruction can carry it"
        )
    # The store, from its own account — which is also what proves the name resolves to the
    # PDA the instruction will name, before anything is rewritten.
    resolved = resolve_store(store, rpc_url=proof.rpc_url, rpc_call=rpc_call)
    touched = (resolved.receipts, authority.pubkey)
    blank = Delivery(
        store=resolved.store_name,
        receipt_id=receipt_id,
        authority=authority.pubkey,
        landed=False,
    )

    # RESET runs in a `finally`, and the result is assembled AFTER it. This matters more
    # here than in a purchase: what was rewritten is the STORE's authority field, and
    # leaving that behind would hand the next run a storefront owned by a key that no
    # longer exists in any process.
    try:
        result = _run(
            blank,
            proof=proof,
            authority=authority,
            receipts=resolved.receipts,
            take_the_store=take_the_store,
            fee_lamports=fee_lamports,
            call=rpc_call or default_rpc_call,
            rpc_call=rpc_call,
        )
    finally:
        cleaned = _reset_all(proof, touched, rpc_call)
    return replace(result, reset=cleaned)


def _run(
    blank: Delivery,
    *,
    proof: SurfnetProof,
    authority: EphemeralSigner,
    receipts: str,
    take_the_store: bool,
    fee_lamports: int,
    call: RpcCall,
    rpc_call: RpcCall | None,
) -> Delivery:
    """Steps 1 to 5. Split out so RESET can be a ``finally`` around the whole of it."""
    # 1. TAKE THE STORE — and prove the write took, before anything depends on it.
    taken = (
        _take_the_store(
            proof, receipts=receipts, new_authority=authority.pubkey, call=call
        )
        if take_the_store
        else None
    )

    # 2. FUND. Lamports only: this instruction transfers no tokens, so a token balance
    #    here would be scenery.
    fund_sol(proof, authority.pubkey, fee_lamports, rpc_call=rpc_call)

    # 3. BUILD.
    blockhash, _last_valid = latest_blockhash(proof.rpc_url, rpc_call)
    unsigned = _build_mark_as_delivered(
        store_name=blank.store,
        receipt_id=blank.receipt_id,
        authority=authority.pubkey,
        blockhash=blockhash,
    )

    # 4. SIGN — through the rehearsal's own signer, which checks the fee payer against the
    #    signing key BEFORE a signature exists.
    signed, _signature = _sign(unsigned, authority)

    # The BEFORE half, at the last possible moment: the vec as it stands after the take.
    before = _row(call, proof.rpc_url, receipts, blank.receipt_id)

    # 5. LAND.
    sent = call(
        proof.rpc_url,
        "sendTransaction",
        [
            signed,
            {
                "encoding": "base64",
                # The blockhash was issued at `confirmed`; sendTransaction's preflight
                # defaults to the stricter `finalized` and rejects these bytes every time
                # with a BlockhashNotFound that names neither cause nor remedy.
                "preflightCommitment": RPC_COMMITMENT,
                "maxRetries": 3,
            },
        ],
    )
    signature = (sent or {}).get("result")
    if not isinstance(signature, str):
        return replace(
            blank,
            took_the_store=taken,
            refusals=(Refusal("land", f"the node returned no signature: {sent!r}"),),
        )
    if not _confirm(call, proof.rpc_url, signature):
        return replace(
            blank,
            took_the_store=taken,
            signature=signature,
            refusals=(
                Refusal(
                    "land",
                    f"sent {signature} and it did not confirm within "
                    f"{CONFIRM_TIMEOUT_SECONDS:.0f}s — nothing below is evidence",
                ),
            ),
        )

    # 6. READ IT BACK. A confirmed signature is not the claim this module makes.
    after = _row(call, proof.rpc_url, receipts, blank.receipt_id)
    return _judge(
        blank,
        taken=taken,
        signature=signature,
        before=before,
        after=after,
        meta=_transaction_meta(call, proof.rpc_url, signature),
    )


# ---------------------------------------------------------------------------
# 1. the take
# ---------------------------------------------------------------------------


def _take_the_store(
    proof: SurfnetProof, *, receipts: str, new_authority: str, call: RpcCall
) -> TakenStore:
    """Rewrite the store's ``authority`` field to ``new_authority``. FORK ONLY.

    There is no equivalent of this on any real chain, and that is the point rather than a
    caveat: the field can only be changed by the program, and the program changes it only
    for whoever already holds it. Here the account's bytes are overwritten directly by the
    validator, so a delivery can be rehearsed without anybody's merchant key.

    The write goes through :func:`gecko.fork_preflight.surfnet_overlay`, which owns the one
    base64-to-hex transcode ``surfnet_setAccount`` needs — measured there, and a second
    implementation of it is how the two drift. Everything except those 32 bytes is passed
    back exactly as it was read: same lamports, same owner, same length, same executable.

    THE READ-BACK IS THE WHOLE STEP. ``surfnet_setAccount`` answers ``{"value": null}`` to
    every successful write and silently ignores fields it does not recognise, so "the RPC
    returned 200" is not evidence of anything. Three things are therefore checked against
    the account as it reads AFTERWARDS: that the authority is now the ephemeral key, that
    every other byte is untouched, and that the whole account still decodes as a storefront.

    :raises CheatcodeError: if the write did not take, or took more than it was asked to.
    """
    account = _account(call, proof.rpc_url, receipts)
    raw = _raw_data(account)
    if account is None or raw is None:
        raise DeliveryError(
            f"the store account {receipts} could not be read from {proof.rpc_url}, so "
            "there is nothing to take"
        )
    try:
        start, end = authority_span(raw)
        before = decode_store(raw, address=receipts)
    except StoreDecodeError as exc:
        raise DeliveryError(
            f"the account at {receipts} does not decode as a storefront ({exc}) — "
            "refusing to overwrite 32 bytes of something whose layout is unknown"
        ) from exc

    from solders.pubkey import Pubkey

    rewritten = raw[:start] + bytes(Pubkey.from_string(new_authority)) + raw[end:]
    surfnet_overlay(proof.rpc_url, call)(
        {
            receipts: AccountState(
                address=receipts,
                lamports=int(account.get("lamports") or 0),
                owner=str(account.get("owner")),
                data_base64=base64.b64encode(rewritten).decode(),
                executable=bool(account.get("executable")),
            )
        }
    )

    observed = _raw_data(_account(call, proof.rpc_url, receipts))
    if observed is None or len(observed) != len(raw):
        raise CheatcodeError(
            f"surfnet_setAccount reported success but {receipts} now reads "
            f"{len(observed) if observed is not None else 'nothing'} bytes where it held "
            f"{len(raw)} — on this RPC the write did not take effect as asked"
        )
    collateral = sum(
        1
        for index, (was, now) in enumerate(zip(raw, observed))
        if was != now and not start <= index < end
    )
    try:
        after = decode_store(observed, address=receipts)
    except StoreDecodeError as exc:
        raise CheatcodeError(
            f"after the authority rewrite {receipts} no longer decodes as a storefront "
            f"({exc}) — every reading taken from it below would be fiction"
        ) from exc
    if after.authority != new_authority or collateral:
        raise CheatcodeError(
            f"surfnet_setAccount reported success but {receipts} names "
            f"{after.authority} as its authority, not {new_authority}"
            + (
                f", and {collateral} byte(s) outside the authority changed"
                if collateral
                else ""
            )
            + " — on this RPC the write did not take effect"
        )
    return TakenStore(
        address=receipts,
        authority_before=before.authority,
        authority_after=after.authority,
        collateral_changes=collateral,
        data_len=len(observed),
    )


# ---------------------------------------------------------------------------
# 3. the build
# ---------------------------------------------------------------------------


def _build_mark_as_delivered(
    *, store_name: str, receipt_id: int, authority: str, blockhash: str
) -> str:
    """The unsigned transaction, base64 — built HERE, locally, from the store name alone.

    WHY A DIRECT BUILDER AND NOT THE OTHER TWO. ``prepare_purchase`` builds through
    Orquestra's hosted ``/build`` endpoint, and the FDL route
    (:mod:`gecko.project.fdl` + ``scripts/orquestra_bridge``) runs a partner's compiler
    under ``bun`` against a local checkout of his repo. Both are the right answer for the
    round trip, whose question is "does his engine agree with our graph". This module's
    question is different — "what does the PROGRAM do once the transaction lands" — and
    putting a third-party service or a bun subprocess inside a loop whose entire value is a
    local, offline-falsifiable read-back buys nothing and costs the fork sandbox its
    independence. The instruction is two accounts, two arguments and a fixed discriminator;
    the honest thing at that size is to encode it.

    THE JOIN THE IDL LOSES, BOUND STRUCTURALLY. ``mark_as_delivered``'s receipts account
    declares the seed ``store_name`` while the instruction's own arguments are
    ``_store_name`` and ``receipt_id`` — Rust's unused-parameter convention copied into the
    IDL. So the seed names an argument that does not exist, and a deriver that matches
    seeds to args by name gets nothing for the one seed that selects the store. Here there
    is exactly ONE ``store_name`` parameter: it is Borsh-encoded as the wire argument
    ``_store_name`` AND it derives the receipts PDA. They cannot disagree, because there is
    nothing for them to disagree with.

    The account order and flags are the IDL's own: ``[receipts(w), authority(w, signer)]``.
    """
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    encoded_name = store_name.encode("utf-8")
    data = (
        MARK_AS_DELIVERED_DISCRIMINATOR
        + len(encoded_name).to_bytes(4, "little")
        + encoded_name
        + receipt_id.to_bytes(8, "little")
    )
    receipts = receipts_pda(store_name)
    instruction = Instruction(
        Pubkey.from_string(LET_ME_BUY_PROGRAM_ID),
        data,
        [
            AccountMeta(Pubkey.from_string(receipts), False, True),
            AccountMeta(Pubkey.from_string(authority), True, True),
        ],
    )
    message = Message.new_with_blockhash(
        [instruction], Pubkey.from_string(authority), Hash.from_string(blockhash)
    )
    return base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()


# ---------------------------------------------------------------------------
# 6. the read-back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    """One reading of the vec, reduced to the row the caller asked about.

    The walk sees every row — the vec is sequential — and this is what survives it: a
    presence bit, a delivery flag, and two counts. No buyer, no price, no product.
    """

    present: bool
    was_delivered: bool | None
    rows: int | None
    account_bytes: int | None
    unreadable: str | None = None


def _row(call: RpcCall, rpc_url: str, receipts: str, receipt_id: int) -> _Row:
    """Read the store account and reduce it to :class:`_Row` for ``receipt_id``."""
    raw = _raw_data(_account(call, rpc_url, receipts))
    if raw is None:
        return _Row(False, None, None, None, "the store account could not be read")
    try:
        rows, _total = _walk_receipts(raw)
    except StoreDecodeError as exc:
        return _Row(
            False, None, None, len(raw), f"the receipts vec did not decode: {exc}"
        )
    for row in rows:
        if int(row["receipt_id"]) == receipt_id:
            return _Row(True, bool(row["delivered"]), len(rows), len(raw))
    return _Row(False, None, len(rows), len(raw))


def _judge(
    partial: Delivery,
    *,
    taken: TakenStore | None,
    signature: str,
    before: _Row,
    after: _Row,
    meta: Mapping[str, Any] | None,
) -> Delivery:
    """Turn two readings into the answer, including the one that says "nothing happened".

    Each objection is its own sentence. "There is no such row" and "the row is still false"
    are different defects with different remedies — the first is an eviction the venue
    cannot see, the second would be a program that accepted the call and ignored it — and
    collapsing them into one boolean throws away which happened.
    """
    problems: list[str] = []
    for reading, when in ((before, "before"), (after, "after")):
        if reading.unreadable:
            problems.append(f"{when} the transaction, {reading.unreadable}")
    if (
        taken is None
        and after.was_delivered is True
        and before.was_delivered is not True
    ):
        # The store was never made to name this signer, and the row flipped anyway.
        problems.append(
            f"the store does not name {partial.authority} as its authority and row "
            f"{partial.receipt_id} was delivered anyway — mark_as_delivered enforces no "
            "require! against receipts.authority, so the IDL's `signer` flag means only "
            "that SOMEBODY signed. Anyone can clear anyone's delivery queue for a fee"
        )

    silent = False
    if after.unreadable is not None:
        # A READ FAILURE IS NOT THE FINDING, and this branch exists because an adversarial
        # pass produced the finding out of a transient miss. When the post-transaction
        # getAccountInfo comes back empty, `present` is False for a reason that has nothing
        # to do with the program: the node was behind, the account paged out, the RPC
        # hiccuped. Reporting that as "the program scanned N rows, found nothing and
        # returned Ok" asserts an observation nobody made — and it would let an RPC blip
        # manufacture the headline of this whole demonstration. Unknown is its own answer.
        problems.append(
            f"the store account could not be read after the transaction, so whether row "
            f"{partial.receipt_id} changed is UNKNOWN. This is not the eviction finding: "
            "re-run it rather than reporting either outcome"
        )
    elif not after.present:
        silent = True
        problems.append(
            f"the transaction confirmed and the receipts vec carries NO row with id "
            f"{partial.receipt_id} — the program scanned {after.rows} resident row(s), "
            "found nothing and returned Ok. The vec is capped at 20 and make_purchase "
            "evicts the oldest without an error, so an evicted order is marked delivered "
            "successfully, forever, and stays undelivered"
        )
    elif before.was_delivered is True and after.was_delivered is True:
        silent = True
        problems.append(
            f"row {partial.receipt_id} was already was_delivered=true before this ran, so "
            "the transaction changed nothing — a second delivery is indistinguishable "
            "from the first from the outside"
        )
    elif after.was_delivered is not True:
        problems.append(
            f"row {partial.receipt_id} is present and was_delivered is "
            f"{after.was_delivered!r} after a CONFIRMED transaction — the program "
            "accepted the call and did not record the delivery"
        )
    return replace(
        partial,
        landed=True,
        took_the_store=taken,
        signature=signature,
        units_consumed=_int_or_none(meta, "computeUnitsConsumed"),
        fee_lamports=_int_or_none(meta, "fee"),
        receipt_found=after.present,
        was_delivered_before=before.was_delivered,
        was_delivered_after=after.was_delivered,
        silently_did_nothing=silent,
        store_readable=after.unreadable is None,
        resident_receipts=after.rows,
        resident_bytes=after.account_bytes,
        discrepancies=tuple(problems),
    )

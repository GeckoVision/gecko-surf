"""Surfpool cheatcodes, with the parameter shapes MEASURED against a running fork.

WHY THIS MODULE TAKES A PROOF AND NEVER A URL. These are the calls that rewrite chain
state: they hand an address money, mint a token balance out of nothing, and move the
clock. Aimed at mainnet they are harmless only because mainnet does not implement them —
which is a fact about the *other* server, not a guarantee this repo makes. So the guard
is the same one :mod:`gecko.sandbox.surfnet` uses for key material: every function here
takes a :class:`~gecko.sandbox.surfnet.SurfnetProof` and reads the endpoint off it. There
is no ``rpc_url`` parameter to get wrong, and a proof cannot exist unless a validator
already answered ``surfnet_getSurfnetInfo``.

EVERY SHAPE BELOW WAS DISCOVERED, NOT GUESSED. Recorded 2026-08-17 against
``surfpool start --no-tui --no-deploy --rpc-url <mainnet> --port 8933``, surfpool 1.1.1
(``solana-core`` 3.1.6). Each function's docstring carries the exact request that was
sent and the exact error that a wrong one came back with. Where a cheatcode does
something other than what its name suggests, the docstring says so in capitals rather
than smoothing it over — the point of a discovered wrapper is that the surprises survive
into the call site.

THE ONE HAZARD THAT SPANS ALL OF THEM: **unknown fields are silently ignored.**
``surfnet_setAccount [addr, {"bogusField": 1}]`` returns success and changes nothing;
so do ``{"rentEpoch": …}``, ``{"space": …}``, and — the one that will actually bite —
``{"delegated_amount": …}`` where the struct spells it ``delegatedAmount``. A typo is
therefore a SILENT no-op, not an error. That is precisely why these wrappers build the
update objects themselves from typed arguments instead of forwarding a caller's dict,
and why each one READS THE EFFECT BACK and raises if the chain does not agree. "The RPC
returned 200" is not evidence here and is never treated as any.

Control-plane invariant #1 holds: nothing is persisted, and everything read back
(lamports, token amount, slot) is public validator state on a local fork.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..landing import RPC_COMMITMENT, TOKEN_PROGRAM_ID
from ..rpc import RpcCall, default_rpc_call
from ..store_accounts import derive_ata
from .surfnet import SandboxError, SurfnetProof

__all__ = [
    "ClockJump",
    "CheatcodeError",
    "FundedSol",
    "FundedToken",
    "ResetAccount",
    "TimeTravelError",
    "fund_sol",
    "fund_token",
    "reset_account",
    "time_travel",
]


class CheatcodeError(SandboxError):
    """A cheatcode was refused, or it returned success and the effect was not there.

    The second half matters more than the first. Surfpool answers ``{"value": null}`` to
    every successful state write, so the response body carries no evidence at all — a
    misspelled field, an ignored argument and a real mutation are indistinguishable from
    the wire. Every function in this module therefore reads the state back, and this is
    what it raises when the read disagrees with what was asked for.
    """


class TimeTravelError(CheatcodeError):
    """The clock was asked to move somewhere it cannot go — almost always BACKWARDS.

    Measured: ``surfnet_timeTravel`` is forward-only and says so in a ``-32603`` internal
    error, e.g. ``Cannot travel to past slot: target=439000000, current=439900001``. A
    fork is therefore not rewindable: to get an earlier clock, restart the fork.
    """


def _cheat(
    proof: SurfnetProof,
    method: str,
    params: list[Any],
    rpc_call: RpcCall | None = None,
) -> Any:
    """POST a cheatcode at the PROVEN endpoint and return its ``result``.

    ``proof.rpc_url`` is the only endpoint any of this can reach — that binding is the
    whole reason the proof, and not a URL, is the parameter.
    """
    call = rpc_call or default_rpc_call
    response = call(proof.rpc_url, method, params)
    if not isinstance(response, Mapping):
        raise CheatcodeError(
            f"{method} returned a non-object JSON-RPC response from {proof.rpc_url}"
        )
    return response.get("result")


def _value(result: Any) -> Any:
    """Unwrap the ``{context, value}`` envelope surfpool wraps most answers in."""
    if isinstance(result, Mapping) and "value" in result:
        return result["value"]
    return result


# ---------------------------------------------------------------------------
# SOL
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundedSol:
    """An address's lamport balance after :func:`fund_sol`, as READ BACK from the fork."""

    address: str
    requested_lamports: int
    observed_lamports: int
    #: True when the address held no account at all before the call. Measured:
    #: ``surfnet_setAccount`` CREATES a missing account (owner ``11111…``, ``space`` 0).
    created: bool


def fund_sol(
    proof: SurfnetProof,
    address: str,
    lamports: int,
    *,
    rpc_call: RpcCall | None = None,
) -> FundedSol:
    """Set ``address``'s lamport balance to exactly ``lamports``. Measured shape::

        --> surfnet_setAccount ["<pubkey>", {"lamports": 5000000000}]
        <-- {"context": {...}, "value": null}

    THE PARAMS ARE A FIXED 2-TUPLE. ``[]`` answers ``-32602 'Invalid params: invalid
    length 0, expected a tuple of size 2.'`` and a third element answers ``'invalid
    length 3, expected fewer elements in array.'`` A malformed pubkey is rejected up
    front (``Invalid pubkey 'notapubkey'``), so the address is validated by the server.

    SET, NOT ADD. The value is absolute — calling twice with the same number leaves the
    same balance, not double it. That is what the rehearsal wants: "the buyer holds
    exactly X" is a statement you can make in one call and re-make idempotently.

    A PARTIAL PATCH, NOT A REPLACE. The update struct's measured fields are ``lamports``
    (u64), ``owner`` (string), ``data`` (string), ``executable`` (bool); anything omitted
    is LEFT ALONE. Verified on a live SPL token account: setting only ``lamports`` kept
    its 165 bytes of data and its ``Tokenkeg…`` owner intact. Only ``lamports`` is
    exposed here — see the module docstring on why a caller's raw dict is not forwarded.

    ``data`` IS HEX, NOT BASE64, AND THE ERROR MESSAGE LIES ABOUT IT. Recorded for
    whoever needs it next: serde advertises ``expected a list of bytes or a string``, but
    a base64 string fails with ``Invalid hex data provided: Invalid character 'Q' at
    position 1`` and a real byte list fails with ``Invalid character '\\t' at position 0``
    — the list is stringified and then hex-decoded, so that branch is unusable. Only a
    hex string works (``"deadbeef"`` → 4 bytes). Not exposed, because a helper whose
    encoding contradicts its own error text is worse than no helper — and because
    :func:`gecko.fork_preflight.surfnet_overlay` already owns the one place in this repo
    that writes account ``data`` through this cheatcode, having measured the same thing
    independently. A second implementation of a transcode is how the two drift.

    :raises CheatcodeError: if the balance read back is not ``lamports``.
    """
    if lamports < 0:
        raise CheatcodeError(
            f"refusing to fund {address} with {lamports} lamports — the update field is "
            "a u64 and a negative value is rejected by the server as 'expected u64'"
        )
    before = _value(
        _cheat(proof, "getAccountInfo", [address, {"encoding": "base64"}], rpc_call)
    )
    _cheat(proof, "surfnet_setAccount", [address, {"lamports": lamports}], rpc_call)
    observed = _value(_cheat(proof, "getBalance", [address], rpc_call))
    if observed != lamports:
        raise CheatcodeError(
            f"surfnet_setAccount reported success but {address} holds {observed!r} "
            f"lamports, not {lamports} — on this RPC the write did not take effect"
        )
    return FundedSol(
        address=address,
        requested_lamports=lamports,
        observed_lamports=int(observed),
        created=before is None,
    )


# ---------------------------------------------------------------------------
# SPL tokens
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundedToken:
    """A token balance after :func:`fund_token`, with the ATA it actually landed on."""

    owner: str
    mint: str
    #: The associated token account, derived HERE with :func:`gecko.store_accounts.derive_ata`
    #: and then confirmed by reading the balance back off that exact address.
    token_account: str
    token_program: str
    requested_amount: int
    observed_amount: int
    #: True when the ATA did not exist before the call. **Measured: it does not need to.**
    created: bool


def fund_token(
    proof: SurfnetProof,
    owner: str,
    mint: str,
    amount: int,
    *,
    token_program: str = TOKEN_PROGRAM_ID,
    rpc_call: RpcCall | None = None,
) -> FundedToken:
    """Give ``owner`` exactly ``amount`` base units of ``mint``. Measured shape::

        --> surfnet_setTokenAccount ["<owner>", "<mint>", {"amount": 4200000}]
        --> surfnet_setTokenAccount ["<owner>", "<mint>", {"amount": 42}, "<tokenProgram>"]
        <-- {"context": {...}, "value": null}

    THREE REQUIRED ARGS, AN OPTIONAL FOURTH. ``[]`` answers ``-32602 '`params` should
    have at least 3 argument(s)'``; a bare integer in slot 3 answers ``'invalid type:
    integer `1000000`, expected struct TokenAccountUpdate'`` — so the amount must be
    wrapped in an object. The fourth argument is the TOKEN PROGRAM, and it moves the
    address written: passing ``TokenzQd…`` writes the Token-2022 ATA and leaves the
    legacy ATA absent (verified: legacy ``getAccountInfo`` → ``null``, the 2022 ATA →
    ``owner: TokenzQd…``). It is NOT validated — passing the System Program id still
    returns success, and simply writes to whatever address that derives.

    **YES, IT CREATES A MISSING ATA — this was the question worth answering.** Measured
    against a keypair generated seconds earlier, with no account anywhere on chain::

        surfnet_setTokenAccount [<fresh owner>, <USDC>, {"amount": 4200000}]
        getAccountInfo(ATA) -> lamports 2039280, space 165, owner Tokenkeg…,
                               state "initialized", tokenAmount.amount "4200000"

    The account is written at the canonical ATA address, rent-exempt at the standard
    2,039,280 lamports, already ``initialized``. No ``create_associated_token_account``
    instruction, no payer, no signature. So the rehearsal does NOT need to pre-create the
    buyer's ATA — but a real purchase on mainnet WOULD, and that asymmetry is the fork's
    biggest lie about the world. A flow rehearsed here can silently omit ATA creation and
    fail live.

    **IT DOES NOT TOUCH THE MINT.** Also measured: an owner and a mint that BOTH exist
    nowhere still produce a happy 165-byte token account claiming 123 units of a mint
    with no mint account behind it. Nothing validates that the mint exists, that its
    decimals match, or that supply adds up (``surfnet_setSupply`` is the separate
    cheatcode for that). So a balance from here can be arithmetically impossible, and
    only an actual transfer instruction will notice.

    SET, NOT ADD, AND ``{}`` IS A NO-OP. Following 4,200,000 with 1 leaves 1, not
    4,200,001; an empty update leaves the existing balance untouched rather than zeroing
    it. The other measured ``TokenAccountUpdate`` fields are ``delegate`` (string),
    ``state`` (string; ``"frozen"`` accepted, ``"bogus"`` → ``Invalid token account
    state bogus``), ``delegatedAmount`` (u64) and ``closeAuthority`` (string) — all
    camelCase, and their snake_case spellings are among the silently-ignored unknowns.

    THE READ-BACK CANNOT USE ``getTokenAccountBalance``, and finding that out cost a red
    test. That method needs the MINT account in order to report ``decimals``, so on an
    ATA this cheatcode created for a mint that does not exist it answers
    ``-32602 'Account <ata> not found'`` — about the mint, while pointing at the token
    account. The same happens for a Token-2022 ATA whose mint was never created. Since
    "the mint need not exist" is precisely one of the behaviours documented above, the
    verification here decodes the account's own bytes instead: SPL layout is
    ``mint(32) | owner(32) | amount(u64 LE)``, which also lets the owner and the mint be
    confirmed rather than assumed.

    :raises CheatcodeError: if the ATA's decoded balance/owner/mint are not what was asked.
    """
    if amount < 0:
        raise CheatcodeError(
            f"refusing to set a negative token balance ({amount}) for {owner} — the "
            "update field is a u64 and the server rejects it as 'expected u64'"
        )
    token_account = derive_ata(owner, mint, token_program=token_program)
    before = _value(
        _cheat(
            proof, "getAccountInfo", [token_account, {"encoding": "base64"}], rpc_call
        )
    )
    _cheat(
        proof,
        "surfnet_setTokenAccount",
        [owner, mint, {"amount": amount}, token_program],
        rpc_call,
    )
    after = _value(
        _cheat(
            proof, "getAccountInfo", [token_account, {"encoding": "base64"}], rpc_call
        )
    )
    observed = _decode_token_account(after)
    if observed != (mint, owner, amount):
        raise CheatcodeError(
            f"surfnet_setTokenAccount reported success but {token_account} "
            f"(ATA of {owner} for {mint}) decodes as {observed!r}, not "
            f"{(mint, owner, amount)!r} — on this RPC the write did not take effect"
        )
    return FundedToken(
        owner=owner,
        mint=mint,
        token_account=token_account,
        token_program=token_program,
        requested_amount=amount,
        observed_amount=observed[2],
        created=before is None,
    )


def _decode_token_account(account: Any) -> tuple[str | None, str | None, int | None]:
    """``(mint, owner, amount)`` from a raw SPL token account, or ``None``s if unreadable.

    Deliberately reads the first 72 bytes and nothing else. The layout
    ``mint(32) | owner(32) | amount(u64 LE)`` is fixed and shared by the legacy Token
    program and Token-2022 (whose extensions live PAST the 165-byte base), so the same
    decode answers for both — which the jsonParsed path does not, and neither does
    ``getTokenAccountBalance`` when the mint account is missing.

    Every byte here is treated as untrusted: a short, absent, or wrongly-shaped payload
    returns ``None``s and the caller turns that into a refusal, rather than an exception
    from deep in a decoder.
    """
    from base64 import b64decode
    from binascii import Error as BinasciiError

    from solders.pubkey import Pubkey

    if not isinstance(account, Mapping):
        return (None, None, None)
    data = account.get("data")
    encoded = data[0] if isinstance(data, list) and data else data
    if not isinstance(encoded, str):
        return (None, None, None)
    try:
        raw = b64decode(encoded, validate=True)
    except (BinasciiError, ValueError):
        return (None, None, None)
    if len(raw) < 72:
        return (None, None, None)
    return (
        str(Pubkey.from_bytes(raw[0:32])),
        str(Pubkey.from_bytes(raw[32:64])),
        int.from_bytes(raw[64:72], "little"),
    )


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResetAccount:
    """What an address looked like before and after :func:`reset_account`."""

    address: str
    lamports_before: int | None
    #: What the fork reports once the local override is gone. ``None`` means the address
    #: has no account on the FORKED chain either.
    lamports_after: int | None


def reset_account(
    proof: SurfnetProof,
    address: str,
    *,
    rpc_call: RpcCall | None = None,
) -> ResetAccount:
    """Drop the local override on ``address``. Measured shape::

        --> surfnet_resetAccount ["<pubkey>"]
        <-- {"context": {...}, "value": null}

    **THE NAME OVERPROMISES: THIS DOES NOT ZERO OR DELETE THE ACCOUNT.** It discards
    what the fork has written locally, after which the next read falls back to the
    FORKED chain — i.e. to mainnet's real state. Measured end to end: an address funded
    here to 5,000,000,000 lamports read back as 9,925,000 after the reset, which is its
    genuine mainnet balance. Treat this as "undo my cheatcodes", never as "empty this
    account"; to model an empty wallet, ``fund_sol(proof, addr, 0)``.

    ONE OR TWO PARAMS. Three answers ``-32602 'Invalid parameters: Expected from 1 to 2
    parameters.'``, and a bad pubkey is rejected server-side. The optional second
    parameter is a ``ResetAccountConfig`` — a sequence in that slot answers ``'invalid
    length 0, expected struct ResetAccountConfig with 1 element'``, so it holds exactly
    one field, **and we could not discover its name**: every candidate tried
    (``commitment``, ``slot``, ``recursive``, ``encoding``, ``minContextSlot``,
    ``dataSlice``, ``keepData``, ``preserve``) returned success while doing nothing,
    which is indistinguishable from the silent unknown-field behaviour described in the
    module docstring. Rather than ship a guess, it is not exposed. If you need it, read
    surfpool's source for ``ResetAccountConfig``.

    NO read-back assertion is possible here — "whatever mainnet says" is not a value this
    function knows in advance — so both sides of the change are RETURNED for the caller
    to judge instead of an effect being claimed.
    """
    before = _value(
        _cheat(proof, "getAccountInfo", [address, {"encoding": "base64"}], rpc_call)
    )
    _cheat(proof, "surfnet_resetAccount", [address], rpc_call)
    after = _value(
        _cheat(proof, "getAccountInfo", [address, {"encoding": "base64"}], rpc_call)
    )
    return ResetAccount(
        address=address,
        lamports_before=_lamports(before),
        lamports_after=_lamports(after),
    )


def _lamports(account: Any) -> int | None:
    if isinstance(account, Mapping) and isinstance(account.get("lamports"), int):
        lamports = account["lamports"]
        assert isinstance(lamports, int)
        return lamports
    return None


# ---------------------------------------------------------------------------
# the clock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClockJump:
    """Where the clock landed, read back from the fork rather than taken on trust."""

    epoch: int
    absolute_slot: int
    #: ``getBlockHeight`` at ``confirmed`` AFTER the jump. Deliberately its own field and
    #: deliberately NOT the ``blockHeight`` inside the ``getEpochInfo`` answer: the two
    #: disagree on a live fork (measured 415 vs 384 microseconds apart), and the one that
    #: governs transaction expiry — what ``gecko.landing.block_height`` reads — is this one.
    block_height: int | None


def time_travel(
    proof: SurfnetProof,
    *,
    epoch: int | None = None,
    slot: int | None = None,
    timestamp_ms: int | None = None,
    rpc_call: RpcCall | None = None,
) -> ClockJump:
    """Move the fork's clock FORWARD. Exactly one destination, measured shape::

        --> surfnet_timeTravel [{"absoluteEpoch": 1020}]
        --> surfnet_timeTravel [{"absoluteSlot": 439900000}]
        --> surfnet_timeTravel [{"absoluteTimestamp": 1788235466000}]
        <-- {"epoch": 1020, "slotIndex": 0, "slotsInEpoch": 432000,
             "absoluteSlot": 440640000, "blockHeight": 352, "transactionCount": 1}

    AN EXTERNALLY-TAGGED ENUM, NOT A CONFIG OBJECT. The server names its own variants:
    ``"x"`` in that slot answers ``-32602 'unknown variant `x`, expected one of
    `absoluteEpoch`, `absoluteSlot`, `absoluteTimestamp`'``, and ``{}`` or a two-key map
    answers ``'expected map with a single key'``. Hence the keyword-only arguments here
    and the refusal below when the count is not exactly one.

    **``absoluteTimestamp`` IS IN MILLISECONDS**, which is the trap this signature exists
    to defuse. The refusal quotes its own clock as ``current=1788149066000`` while
    ``getBlockTime`` answers in seconds — so a caller passing a normal unix timestamp is
    ~1,788 million ms in the past and gets ``Cannot travel to past timestamp``. The
    parameter is therefore named ``timestamp_ms`` and nothing else. Conversion is
    400ms/slot: +86,400,000 ms advanced ``slotIndex`` by exactly 216,000.

    **CALLING IT WITH NO PARAMS IS NOT A NO-OP.** ``params: []`` is accepted (0 or 1
    arguments) and performs *some* jump — the first such call returned an epoch-info body
    and the second failed with ``Cannot travel to past timestamp: target=1786950536942,
    current=1786950538400``. Whatever it does, it is not "tell me the time", and this
    wrapper never sends it.

    **FORWARD ONLY, AND THE REFUSAL IS RAISED HERE RATHER THAN READ OFF THE WIRE.**
    Surfpool rejects a backwards jump with ``-32603 Internal error`` and puts the
    explanation in ``error.data`` (``Cannot travel to past slot: target=439000000,
    current=439900001``) — which :func:`gecko.rpc.default_rpc_call` deliberately does not
    surface. So a "did it go backwards?" test that string-matched the transport's message
    would be matching text that is not there. Instead the destination is compared against
    the fork's OWN reported clock first, which is both precise and offline-testable; the
    remaining ``-32603`` is still mapped to :class:`TimeTravelError`, naming the fact that
    the reason was dropped rather than pretending to know it. There is no rewind: a fork
    that has jumped stays jumped until it is restarted.

    **AND THE ONE THAT MATTERS FOR EXPIRY: THIS DOES NOT AGE A BLOCKHASH.** Jumping five
    whole epochs (2,160,000 slots) moved ``getBlockHeight`` from 345 to 346 and
    ``lastValidBlockHeight`` from 526 to 527. Block height tracks the fork's OWN produced
    blocks, not the slot number it reports. Also measured: the fork hands out a
    placeholder blockhash — ``SURFNETxSAFEHASHxxxxxxxxxxxxxxxxxxxxxxxx177``, with a
    counter suffix that increments — so it is not a real hash either. A rehearsal that
    wants to prove an expiry deadline is honest CANNOT do it by time-travelling; it has
    to reason about ``lastValidBlockHeight`` arithmetic directly.

    :raises TimeTravelError: on a backwards jump, or when the destination count is not 1.
    """
    destinations = {
        "absoluteEpoch": epoch,
        "absoluteSlot": slot,
        "absoluteTimestamp": timestamp_ms,
    }
    given = {key: value for key, value in destinations.items() if value is not None}
    if len(given) != 1:
        raise TimeTravelError(
            "surfnet_timeTravel takes exactly one destination — the server's own words "
            "are 'expected map with a single key', one of absoluteEpoch / absoluteSlot / "
            f"absoluteTimestamp. Got {sorted(given) or 'nothing'}. (timestamp_ms is "
            "MILLISECONDS; passing unix seconds is a jump into the distant past.)"
        )
    ((key, value),) = given.items()
    if value < 0:
        raise TimeTravelError(f"{key} is a u64; {value} is not a destination")
    _refuse_if_not_forward(proof, key, value, rpc_call)
    try:
        result = _cheat(proof, "surfnet_timeTravel", [{key: value}], rpc_call)
    except Exception as exc:
        raise TimeTravelError(
            f"the fork refused to travel to {key}={value} ({exc}). Measured: this method "
            "answers a rejected jump with -32603 and puts the reason in `error.data`, "
            "which the canonical transport drops — every -32603 we have ever seen from "
            "it was the forward-only refusal ('Cannot travel to past …'), so a clock "
            "that has already passed this destination is the first thing to check. "
            "The fork is forward-only; restart it to get an earlier clock."
        ) from None
    epoch_info = result if isinstance(result, Mapping) else {}
    landed_epoch = epoch_info.get("epoch")
    landed_slot = epoch_info.get("absoluteSlot")
    if not isinstance(landed_epoch, int) or not isinstance(landed_slot, int):
        raise CheatcodeError(
            f"surfnet_timeTravel did not answer with an epoch-info body: {result!r}"
        )
    height = _cheat(proof, "getBlockHeight", [{"commitment": RPC_COMMITMENT}], rpc_call)
    return ClockJump(
        epoch=landed_epoch,
        absolute_slot=landed_slot,
        block_height=height if isinstance(height, int) else None,
    )


#: Any millisecond timestamp after 2001-09-09 exceeds this; every unix-SECONDS value
#: between 1970 and the year 33658 falls below it. That gap is what makes the wrong-unit
#: check exact rather than a heuristic — and the wrong unit is the mistake this API is
#: most likely to be handed, since every other Solana timestamp in the stack is seconds.
_MIN_PLAUSIBLE_TIMESTAMP_MS = 1_000_000_000_000


def _refuse_if_not_forward(
    proof: SurfnetProof, key: str, value: int, rpc_call: RpcCall | None
) -> None:
    """Refuse a jump the fork's own clock has already passed, BEFORE sending it.

    Done here rather than by reading the server's complaint, because the complaint is in
    ``error.data`` and the canonical transport does not carry it (see :func:`time_travel`).
    ``absoluteEpoch`` and ``absoluteSlot`` are both readable from ``getEpochInfo``, so
    those are exact. ``absoluteTimestamp`` is not: the fork's internal millisecond clock
    was measured ~10 days ahead of what ``getBlockTime`` reports for the same slot, so
    there is no honest local comparison — what IS checkable is the unit, and that is the
    error people actually make.
    """
    if key == "absoluteTimestamp":
        if value < _MIN_PLAUSIBLE_TIMESTAMP_MS:
            raise TimeTravelError(
                f"timestamp_ms={value} looks like unix SECONDS. surfnet_timeTravel's "
                "absoluteTimestamp is MILLISECONDS — its own refusals quote a clock like "
                "current=1788149066000 — so seconds is a jump ~1.8 billion ms into the "
                "past and the fork is forward-only. Multiply by 1000."
            )
        return
    info = _cheat(proof, "getEpochInfo", [{"commitment": RPC_COMMITMENT}], rpc_call)
    field = "epoch" if key == "absoluteEpoch" else "absoluteSlot"
    current = info.get(field) if isinstance(info, Mapping) else None
    if isinstance(current, int) and value <= current:
        raise TimeTravelError(
            f"the fork's clock is forward-only: {key}={value} is not ahead of the "
            f"current {field}={current}. Restart the fork to get an earlier clock."
        )

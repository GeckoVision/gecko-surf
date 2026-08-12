"""The Receipt engine — close a built plan into a legible ``simulateTransaction`` result.

A **Receipt** answers one honest question: *would this transaction LAND against a
snapshot of on-chain state?* — plus the compute units it burns, a CATEGORICAL revert
class when it wouldn't, and best-effort SOL/token deltas. It does NOT predict price or
slippage, and a fork/RPC snapshot is NEVER labelled mainnet (``network_label`` carries
that caveat onto every Receipt).

Two seams, both injectable so the whole engine is falsifiable offline (Pattern B):
``BuildCall`` (plan → serialized unsigned tx) and ``RpcCall`` (the JSON-RPC transport).
We ONLY ``simulateTransaction`` — never a keypair, never ``sendTransaction``, never a
broadcast (``sigVerify:false, replaceRecentBlockhash:true, commitment:"processed"``,
mirroring ``scripts/subscribe.py``).

Control-plane invariant #1: the Receipt is RETURNED to the caller and stored NOWHERE.
No payload, pubkey, or log line is persisted by this module. ``revert_class`` strings are
a stable vocabulary (the future D2 corpus is CATEGORICAL-only — out of scope here).

KNOWN LIMITS of the token leg (:func:`parse_token_deltas`) — state them here so a caller
sizing a spend cap knows where the measurement stops:

* **An account absent from BOTH ``preTokenBalances`` and ``postTokenBalances`` is
  invisible.** The delta is computed from the rows the node sends; an account it never
  mentions produces no movement, and the report reads ``measured`` with no outflow for it.
  We refuse the asymmetric case (in pre, gone from post → ``post-balance-missing``)
  because the pre row is evidence the account exists; the symmetric case leaves no
  evidence at all, so there is nothing to notice. A caller whose cap must cover a
  specific mint has to check that mint APPEARS in the movements rather than trusting a
  clean report to mean "and nothing else moved either".
* **The three payload shapes are three different facts** and are never collapsed: key
  absent → ``None`` (NOT TRACKED); ``[]`` → measured, nothing moved; ``null`` → the
  ``balances-null`` refusal. The reasoning is at the guard in ``parse_token_deltas``.
* **Amounts are only as good as the declared decimals.** A mint declaring two different
  scales anywhere in one report is refused rather than reconciled.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence

from .networks import UNKNOWN_NETWORK, Network
from .rpc import RpcCall, _http_post_json, default_rpc_call, validate_rpc_url
from .txbind import LookupResolution

__all__ = [
    "BuildCall",
    "BuiltTx",
    "REVERT_FAMILIES",
    "Receipt",
    "SimulateError",
    "TOKEN_2022_PROGRAM_ID",
    "TOKEN_DELTA_REFUSALS",
    "TOKEN_PROGRAM_ID",
    "TokenDeltaRefusal",
    "TokenDeltaReport",
    "TokenDeltaUnmeasurable",
    "TokenMovement",
    "TokenOutflow",
    "classify_revert",
    "parse_token_deltas",
    "revert_family",
    "simulate",
]

# The CLOSED family set for a revert. Single source of truth: these are exactly the
# names ``classify_revert`` can return (``none`` for the no-revert case, plus the
# ``custom_program_error`` family that classify emits parametrically as
# ``custom_program_error:<code>``). The D2 corpus imports this rather than redeclaring
# it, so the vocabulary can only ever change HERE (invariant: never two sources).
REVERT_FAMILIES: frozenset[str] = frozenset(
    {
        "none",
        "slippage",
        "account_error",
        "insufficient_funds",
        "custom_program_error",
        "other",
    }
)

_CUSTOM_FAMILY = "custom_program_error"


@dataclass(frozen=True)
class BuiltTx:
    """A serialized UNSIGNED transaction plus the encoding it is in.

    The builder (Orquestra ``/build``) returns the tx in **base58**; other builders may
    return base64. ``simulateTransaction`` supports both, so we carry the encoding rather
    than assume one — passing the wrong encoding is a silent decode failure.
    """

    tx: str
    encoding: str  # "base58" | "base64" — as reported by the builder


# plan -> the built UNSIGNED transaction (Orquestra /build, or an injected fake).
BuildCall = Callable[[Mapping[str, Any]], "BuiltTx"]

# The default honesty label: a simulation is a snapshot, not mainnet, not a price.
_DEFAULT_NETWORK_LABEL = "simulated (fork/RPC snapshot — not mainnet)"

# Log substrings (lower-cased match) that mean the revert was a slippage guard tripping,
# not a generic custom error — pump's buy uses 0x1772 / TooMuchSolRequired for this.
_SLIPPAGE_MARKERS = ("toomuchsolrequired", "slippage", "max_sol_cost", "0x1772")
_INSUFFICIENT_MARKERS = ("insufficient", "notenoughsol")
_ACCOUNT_MARKERS = (
    "accountnotfound",
    "could not find account",
    "accountownedbywrongprogram",
    "accountnotinitialized",
    "not initialized",
    "notinitialized",
)


# --- the token leg: a delta denominated in the mint, or an explicit refusal ------------
#
# WHY THIS EXISTS: ``Receipt.sol_delta`` answers "how many lamports left the payer". A
# USDC purchase moves ZERO lamports beyond fees, so a lamport-denominated cap reads a
# 25-USDC drain as ~0 outflow. The token leg has to be measured in the MINT'S OWN units,
# and where it cannot be measured honestly it must REFUSE — a zero standing in for
# "could not measure" is the recurring bug this module is explicitly closing.
#
# WHERE THE NUMBERS COME FROM: the ``pre``/``postTokenBalances`` arrays of the simulation
# value (the ``getTransaction`` meta shape, which fork RPCs mirror on
# ``simulateTransaction``). Stock mainnet ``simulateTransaction`` does NOT return them —
# against such an RPC the result is ``None`` (NOT TRACKED), never zero, and a caller that
# needs an amount must refuse for want of one.
#
# NOTHING HERE IS PERSISTED (invariant #1). The values live on the returned Receipt for
# the caller's own decision; only mint, decimals, owner and amounts are carried, never a
# token-account address, instruction data, or any other payload.

#: The two SPL token programs. Which one owns a mint is on-chain state that has to be
#: READ; deriving anything under the wrong one yields a valid-looking address for an
#: account that does not exist. A balance entry that names neither is FLAGGED, never
#: defaulted to classic.
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

#: The CLOSED vocabulary of reasons a token delta is UNMEASURABLE. Every member means
#: "the number we could compute would not be the truth", and each one is returned instead
#: of a number — never alongside a zero.
TOKEN_DELTA_REFUSALS: frozenset[str] = frozenset(
    {
        # Token-2022 extensions where the observed delta is not the debit, the transfer
        # has effects the balances cannot see, or the balance is not readable at all.
        "transfer-fee",
        "transfer-hook",
        "confidential-transfer",
        "permanent-delegate",
        "non-transferable",
        "interest-bearing",
        "ui-amount-scaled",
        # An extension we do not model. Fails CLOSED: an unknown extension may redefine
        # what a transfer does, and "we have not heard of it" is not "it is harmless".
        "extension-unrecognised",
        # Token-2022 mint whose extension set was never read. Unread is not "none" —
        # assuming none is exactly the assume-classic bug.
        "token-2022-extensions-unread",
        # The owning program is absent or unrecognised, or the evidence contradicts it.
        "token-program-unknown",
        "token-program-mismatch",
        # The payload disagrees with itself across pre/post, or is not the shape it
        # claims. All RPC output is untrusted input.
        "mint-inconsistent",
        "decimals-inconsistent",
        "owner-unresolved",
        "malformed-balance",
        # An account we saw BEFORE the transaction and were told nothing about after.
        # Absent-from-post is not zero.
        "post-balance-missing",
        # The key was PRESENT and its value was ``null`` — the node declining to provide
        # the data, which is not the node stating nothing moved. Distinct from
        # ``malformed-balance`` on purpose: see ``parse_token_deltas``.
        "balances-null",
    }
)

#: Token-2022 extensions that change what a transfer DEBITS, what it TOUCHES, or what a
#: balance MEANS — mapped to the refusal each one earns. Names are normalised (lowercase,
#: punctuation stripped) so ``transferFeeConfig``/``transfer_fee_config`` are one key.
_UNSOUND_EXTENSIONS: dict[str, str] = {
    # The recipient is credited less than the sender is debited: delta != debit.
    "transferfeeconfig": "transfer-fee",
    "transferfeeamount": "transfer-fee",
    # An extra program runs on transfer and may require accounts nobody declared; its
    # side effects are invisible in the two balances.
    "transferhook": "transfer-hook",
    "transferhookaccount": "transfer-hook",
    # The amounts are encrypted; pre/post carry no readable balance.
    "confidentialtransfermint": "confidential-transfer",
    "confidentialtransferaccount": "confidential-transfer",
    "confidentialtransferfeeconfig": "confidential-transfer",
    "confidentialtransferfeeamount": "confidential-transfer",
    # A third party can move the tokens: the authority is not the signer's, so a delta
    # here does not attribute the movement to the transaction we are gating.
    "permanentdelegate": "permanent-delegate",
    # Balances can change without a transfer having been possible at all.
    "nontransferable": "non-transferable",
    "nontransferableaccount": "non-transferable",
    # The ui amount is a function of TIME, so the same raw amount renders differently at
    # two moments; a ui number here would be a claim about when, not how much.
    "interestbearingconfig": "interest-bearing",
    "interestbearingmint": "interest-bearing",
    # A multiplier sits between raw and ui, so our locally rendered ui would be wrong.
    "scaleduiamountconfig": "ui-amount-scaled",
    "scaleduiamount": "ui-amount-scaled",
}

#: Extensions REVIEWED and found not to change the delta-equals-debit relation or the
#: raw→ui rendering. This list grows only by review: anything absent from BOTH tables is
#: refused as ``extension-unrecognised``.
_SOUND_EXTENSIONS: frozenset[str] = frozenset(
    {
        "metadatapointer",
        "tokenmetadata",
        "mintcloseauthority",
        "immutableowner",
        "memotransfer",
        "requiredmemoontransfer",
        "defaultaccountstate",
        "cpiguard",
        "grouppointer",
        "groupmemberpointer",
        "tokengroup",
        "tokengroupmember",
    }
)

_BASE58_ALPHABET = frozenset(
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
)

#: A mint's decimals are u8 on chain, but a mint claiming more than this is untrusted
#: input rather than a mint, and we will not render a scale we have never seen.
_MAX_DECIMALS = 18


class TokenDeltaUnmeasurable(Exception):
    """Raised when an amount is read off a report that REFUSED to measure one.

    The whole point of the type: an unmeasurable delta has no number, and the caller
    finds that out by being stopped, not by receiving a zero it will happily compare
    against a cap.
    """


@dataclass(frozen=True)
class TokenMovement:
    """One token account's balance change, denominated in its mint.

    ``pre_raw``/``post_raw``/``delta_raw`` are RAW base units (what the program moves);
    ``ui_delta`` is rendered HERE from ``delta_raw`` and ``decimals`` and is never taken
    from the RPC's ``uiAmount``/``uiAmountString`` — those are untrusted, and for an
    interest-bearing mint they are a function of the moment they were read.

    The field set is deliberately minimal: mint, owner, decimals, amounts. No token
    account address, no account index, no payload (invariant #1).
    """

    mint: str
    owner: str
    decimals: int
    pre_raw: int
    post_raw: int
    delta_raw: int
    ui_delta: str


@dataclass(frozen=True)
class TokenOutflow:
    """The NET amount of one mint that LEFT one owner, aggregated across their accounts.

    ``raw`` is a positive magnitude in the mint's base units — the shape a per-mint cap
    is written in. It is never lamports and must never be summed with lamports: a
    6-decimal amount is a thousandth of the lamport it would be compared against.
    """

    mint: str
    owner: str
    decimals: int
    raw: int
    ui: str


@dataclass(frozen=True)
class TokenDeltaRefusal:
    """WHY a token delta could not be measured — a closed-vocabulary reason plus the
    public mint it concerns. Carries no owner, no account, no payload."""

    reason: str
    mint: str | None
    detail: str

    def __post_init__(self) -> None:
        if self.reason not in TOKEN_DELTA_REFUSALS:
            raise ValueError(
                f"refusal reason outside the closed vocabulary: {self.reason!r}"
            )


@dataclass(frozen=True)
class TokenDeltaReport:
    """The token leg of a Receipt: what moved, or why we will not say.

    THREE STATES, kept distinct on purpose — collapsing any two of them is the bug:

    * ``Receipt.token_delta is None`` — NOT TRACKED. The simulation carried no token
      balances at all (stock ``simulateTransaction`` returns none).
    * ``status == "measured"`` with no movements — a real, observed zero: the balances
      were there and nothing moved.
    * ``status == "unmeasurable"`` — we saw something we cannot honestly reduce to a
      number. ``outflows()``/``net_raw()`` RAISE here rather than return 0.

    Fails closed as a whole: one refusal makes the entire report unmeasurable even beside
    a cleanly measured mint, because nothing proves the refused mint is not the drain.
    """

    status: Literal["measured", "unmeasurable"]
    movements: tuple[TokenMovement, ...]
    refusals: tuple[TokenDeltaRefusal, ...]

    def __post_init__(self) -> None:
        if self.refusals and self.status != "unmeasurable":
            raise ValueError("a report carrying refusals cannot be labelled measured")
        if not self.refusals and self.status != "measured":
            raise ValueError(
                "a report with no refusal has nothing to be unmeasurable about"
            )

    def _require_measured(self) -> None:
        if self.status != "measured":
            reasons = ", ".join(sorted({refusal.reason for refusal in self.refusals}))
            raise TokenDeltaUnmeasurable(
                f"the token delta could not be measured ({reasons}); there is no amount "
                "to read, and zero is not the answer"
            )

    def outflows(self) -> tuple[TokenOutflow, ...]:
        """Net token OUTFLOW per (owner, mint), positive magnitudes, deterministic order.

        Raises :class:`TokenDeltaUnmeasurable` unless the whole report is measured.
        """
        self._require_measured()
        nets: dict[tuple[str, str], list[int]] = {}
        decimals: dict[tuple[str, str], int] = {}
        for movement in self.movements:
            key = (movement.owner, movement.mint)
            nets.setdefault(key, []).append(movement.delta_raw)
            decimals[key] = movement.decimals
        out: list[TokenOutflow] = []
        for key in sorted(nets):
            net = sum(nets[key])
            if net >= 0:
                continue
            owner, mint = key
            out.append(
                TokenOutflow(
                    mint=mint,
                    owner=owner,
                    decimals=decimals[key],
                    raw=-net,
                    ui=_render_ui(-net, decimals[key]),
                )
            )
        return tuple(out)

    def net_raw(self, *, mint: str, owner: str) -> int:
        """The signed net raw delta of ``mint`` for ``owner`` (0 = observed no change).

        Raises :class:`TokenDeltaUnmeasurable` unless the whole report is measured — the
        zero this returns is only ever an OBSERVED zero.
        """
        self._require_measured()
        return sum(
            movement.delta_raw
            for movement in self.movements
            if movement.mint == mint and movement.owner == owner
        )


def _render_ui(raw: int, decimals: int) -> str:
    """Render a raw base-unit amount at ``decimals`` — locally, exactly, no float."""
    if decimals == 0:
        return str(raw)
    sign = "-" if raw < 0 else ""
    magnitude = abs(raw)
    scale = 10**decimals
    return f"{sign}{magnitude // scale}.{magnitude % scale:0{decimals}d}"


def _is_base58_pubkey(value: Any) -> bool:
    """A base58 string of pubkey length. NOT proof of a mint — the floor below which the
    string is certainly not one."""
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 44
        and all(char in _BASE58_ALPHABET for char in value)
    )


def _raw_amount(value: Any) -> int | None:
    """The RPC's ``uiTokenAmount.amount`` as a non-negative int, or ``None``.

    A balance is a decimal STRING of base units on the wire. A float is not truncated, a
    bool is not an int here (``isinstance(True, int)`` is True in Python), a negative is
    not a balance, and nothing is coerced — an unreadable amount refuses.

    ``.isdecimal()``, NOT ``.isdigit()``. ``str.isdigit()`` is True for characters that
    are digits in the Unicode sense but that ``int()`` then rejects — superscripts like
    "\u00b2", for instance. That combination turned untrusted RPC output into a
    ``ValueError`` escaping this module rather than the ``malformed-balance`` refusal the
    caller is documented to receive. ``.isdecimal()`` is exactly the set ``int()`` accepts
    (modulo the leading sign, which a balance may not carry anyway).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


@dataclass(frozen=True)
class _Entry:
    """One validated pre/post token-balance row. Internal; never leaves this module."""

    mint: str
    owner: str
    decimals: int
    raw: int
    program_id: str


def _validate_entry(entry: Any) -> _Entry | TokenDeltaRefusal:
    """Validate ONE untrusted balance row into an ``_Entry``, or the refusal it earns."""
    if not isinstance(entry, Mapping):
        return TokenDeltaRefusal(
            reason="malformed-balance",
            mint=None,
            detail="a token balance entry was not an object",
        )
    mint = entry.get("mint")
    if not _is_base58_pubkey(mint):
        return TokenDeltaRefusal(
            reason="malformed-balance",
            mint=None,
            detail="a token balance names no base58 mint; a string is not a mint",
        )
    assert isinstance(mint, str)  # narrowed by _is_base58_pubkey
    program_id = entry.get("programId")
    if program_id not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        return TokenDeltaRefusal(
            reason="token-program-unknown",
            mint=mint,
            detail=(
                "the balance does not name a known token program, and which program owns "
                "a mint is read, never assumed"
            ),
        )
    assert isinstance(program_id, str)
    owner = entry.get("owner")
    if not _is_base58_pubkey(owner):
        return TokenDeltaRefusal(
            reason="owner-unresolved",
            mint=mint,
            detail="the balance names no owner, so the delta belongs to nobody",
        )
    assert isinstance(owner, str)
    amount_obj = entry.get("uiTokenAmount")
    if not isinstance(amount_obj, Mapping):
        return TokenDeltaRefusal(
            reason="malformed-balance",
            mint=mint,
            detail="the balance carries no uiTokenAmount object",
        )
    decimals = amount_obj.get("decimals")
    if (
        isinstance(decimals, bool)
        or not isinstance(decimals, int)
        or not 0 <= decimals <= _MAX_DECIMALS
    ):
        return TokenDeltaRefusal(
            reason="malformed-balance",
            mint=mint,
            detail=(
                "the balance carries no usable decimals; an amount at the wrong scale is "
                "a well-formed number for the wrong quantity of money"
            ),
        )
    raw = _raw_amount(amount_obj.get("amount"))
    if raw is None:
        return TokenDeltaRefusal(
            reason="malformed-balance",
            mint=mint,
            detail="the balance amount is not a non-negative base-unit integer",
        )
    return _Entry(
        mint=mint, owner=owner, decimals=decimals, raw=raw, program_id=program_id
    )


def _normalise_extension(name: str) -> str:
    return "".join(char for char in name.lower() if char.isalnum())


def _extension_refusal(
    mint: str,
    program_id: str,
    mint_extensions: Mapping[str, Sequence[str]] | None,
) -> TokenDeltaRefusal | None:
    """Decide whether this mint's EXTENSION state makes the measurement unsound.

    Extensions are on-chain state and arrive here as evidence the caller READ (the
    ``extension`` names of a ``getAccountInfo`` ``jsonParsed`` mint). No evidence for a
    Token-2022 mint is a refusal, not an assumption of none.
    """
    declared = None if mint_extensions is None else mint_extensions.get(mint)
    if program_id == TOKEN_PROGRAM_ID:
        if declared:
            return TokenDeltaRefusal(
                reason="token-program-mismatch",
                mint=mint,
                detail=(
                    "extension evidence was supplied for a mint the balance says is owned "
                    "by the classic token program, which has no extensions; the two "
                    "readings contradict each other"
                ),
            )
        return None
    if declared is None:
        return TokenDeltaRefusal(
            reason="token-2022-extensions-unread",
            mint=mint,
            detail=(
                "a Token-2022 mint whose extension set was never read; unread is not "
                "none, and several extensions make the delta not the debit"
            ),
        )
    for name in declared:
        if not isinstance(name, str):
            return TokenDeltaRefusal(
                reason="extension-unrecognised",
                mint=mint,
                detail="an extension name was not a string",
            )
        key = _normalise_extension(name)
        reason = _UNSOUND_EXTENSIONS.get(key)
        if reason is not None:
            return TokenDeltaRefusal(
                reason=reason,
                mint=mint,
                detail=(
                    f"the mint carries the {name} extension, so the balance delta is not "
                    "the amount debited by the signer"
                ),
            )
        if key not in _SOUND_EXTENSIONS:
            return TokenDeltaRefusal(
                reason="extension-unrecognised",
                mint=mint,
                detail=(
                    f"the mint carries {name}, an extension this engine does not model; "
                    "an unmodelled extension may redefine what a transfer does"
                ),
            )
    return None


def _index_balances(
    rows: Any,
) -> tuple[dict[int, Any], TokenDeltaRefusal | None]:
    """Key an untrusted balance array by ``accountIndex``. A duplicate or unusable index
    is refused rather than resolved by last-write-wins."""
    indexed: dict[int, Any] = {}
    if not isinstance(rows, list):
        return indexed, TokenDeltaRefusal(
            reason="malformed-balance",
            mint=None,
            detail="a token balance array was not a list",
        )
    for row in rows:
        index = row.get("accountIndex") if isinstance(row, Mapping) else None
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return indexed, TokenDeltaRefusal(
                reason="malformed-balance",
                mint=None,
                detail="a token balance carries no usable accountIndex",
            )
        if index in indexed:
            return indexed, TokenDeltaRefusal(
                reason="malformed-balance",
                mint=None,
                detail=f"two token balances claim account index {index}",
            )
        indexed[index] = row
    return indexed, None


def parse_token_deltas(
    value: Mapping[str, Any],
    *,
    mint_extensions: Mapping[str, Sequence[str]] | None = None,
) -> TokenDeltaReport | None:
    """Turn a simulation ``value``'s pre/post token balances into a typed delta.

    Returns ``None`` — NOT TRACKED — when the value carries neither array: that is the
    stock ``simulateTransaction`` case, and it must not be confused with "nothing moved".

    ``mint_extensions`` is the caller's READING of each Token-2022 mint's extension set
    (the ``extension`` names from ``getAccountInfo`` ``jsonParsed``). It is an input, not
    a network call: this function makes none. A Token-2022 mint missing from it refuses.

    Everything in ``value`` is untrusted transport output: shapes are checked, numbers are
    never coerced, and any disagreement between the pre and post rows for one account is a
    refusal rather than a resolved conflict.
    """
    has_pre = "preTokenBalances" in value
    has_post = "postTokenBalances" in value
    if not has_pre and not has_post:
        return None

    refusals: list[TokenDeltaRefusal] = []

    # THREE SHAPES, THREE DIFFERENT FACTS — measured against api.mainnet-beta.solana.com
    # (a ``let_me_buy make_purchase`` that moves tokens returned two populated rows at 6
    # decimals; a ``let_me_buy initialize`` that touches no token account returned
    # ``[]``/``[]`` for both; ``null`` was produced by neither):
    #
    # * KEY ABSENT      -> NOT TRACKED. Handled above: ``None``, never zero.
    # * EMPTY ARRAY []  -> MEASURED, nothing moved. A POSITIVE fact from the node about a
    #   real transaction, and it must NOT refuse — ``initialize`` produces exactly this
    #   and has to stay signable. Falls through to the normal path below, where zero rows
    #   yield zero movements and no refusal.
    # * null            -> REFUSE, under its own reason. ``null`` is the node DECLINING to
    #   provide the data, which is not the node stating nothing moved. We cannot tell "no
    #   token movement" from "not computed", and an SPL spend reading as zero outflow
    #   defeats the cap — so the safe direction is to refuse. It is NOT reused as
    #   ``malformed-balance`` (that means "the payload is the wrong shape", a different
    #   operator action) and it is NOT silently read as ``[]``. Measurement says ``null``
    #   does not occur on the standard mainnet RPC for either the token or the non-token
    #   case, so this refusal costs nothing operationally while closing the dangerous
    #   direction.
    if (has_pre and value.get("preTokenBalances") is None) or (
        has_post and value.get("postTokenBalances") is None
    ):
        return TokenDeltaReport(
            status="unmeasurable",
            movements=(),
            refusals=(
                TokenDeltaRefusal(
                    reason="balances-null",
                    mint=None,
                    detail=(
                        "a token balance array was present but null; the node declined to "
                        "provide the data, which is not a statement that nothing moved"
                    ),
                ),
            ),
        )

    pre_rows, pre_refusal = _index_balances(value.get("preTokenBalances", []))
    if pre_refusal is not None:
        refusals.append(pre_refusal)
    post_rows, post_refusal = _index_balances(value.get("postTokenBalances", []))
    if post_refusal is not None:
        refusals.append(post_refusal)

    # Every decimals value declared for each mint, across BOTH arrays and ALL accounts —
    # checked once after the loop. See the disagreement guard below for why.
    declared_decimals: dict[str, set[int]] = {}

    movements: list[TokenMovement] = []
    for index in sorted(set(pre_rows) | set(post_rows)):
        pre_entry: _Entry | None = None
        if index in pre_rows:
            validated = _validate_entry(pre_rows[index])
            if isinstance(validated, TokenDeltaRefusal):
                refusals.append(validated)
                continue
            pre_entry = validated
            declared_decimals.setdefault(validated.mint, set()).add(validated.decimals)

        # An account absent from PRE did not hold the mint before this transaction — the
        # first-time-buyer ATA created in-flight. Its pre balance is 0, a fact, and
        # skipping it would be the zero-for-unmeasured bug wearing a different costume.
        if index not in post_rows:
            refusals.append(
                TokenDeltaRefusal(
                    reason="post-balance-missing",
                    mint=pre_entry.mint if pre_entry else None,
                    detail=(
                        "an account with a balance before the transaction is absent from "
                        "the post balances; absent is not zero"
                    ),
                )
            )
            continue

        validated_post = _validate_entry(post_rows[index])
        if isinstance(validated_post, TokenDeltaRefusal):
            refusals.append(validated_post)
            continue
        post_entry = validated_post
        declared_decimals.setdefault(post_entry.mint, set()).add(post_entry.decimals)

        pre_raw = 0
        if pre_entry is not None:
            if pre_entry.mint != post_entry.mint:
                refusals.append(
                    TokenDeltaRefusal(
                        reason="mint-inconsistent",
                        mint=post_entry.mint,
                        detail="the same account names two different mints across pre/post",
                    )
                )
                continue
            if pre_entry.program_id != post_entry.program_id:
                refusals.append(
                    TokenDeltaRefusal(
                        reason="token-program-mismatch",
                        mint=post_entry.mint,
                        detail="pre and post disagree on which token program owns the mint",
                    )
                )
                continue
            if pre_entry.owner != post_entry.owner:
                refusals.append(
                    TokenDeltaRefusal(
                        reason="owner-unresolved",
                        mint=post_entry.mint,
                        detail="pre and post disagree on the token account's owner",
                    )
                )
                continue
            pre_raw = pre_entry.raw

        extension_refusal = _extension_refusal(
            post_entry.mint, post_entry.program_id, mint_extensions
        )
        if extension_refusal is not None:
            refusals.append(extension_refusal)
            continue

        delta = post_entry.raw - pre_raw
        movements.append(
            TokenMovement(
                mint=post_entry.mint,
                owner=post_entry.owner,
                decimals=post_entry.decimals,
                pre_raw=pre_raw,
                post_raw=post_entry.raw,
                delta_raw=delta,
                ui_delta=_render_ui(delta, post_entry.decimals),
            )
        )

    # THE DECIMALS DISAGREEMENT GUARD — whole-report, not per-account.
    #
    # A mint's decimals are immutable on-chain state, so ONE mint means ONE scale. Checking
    # only the pre vs post rows of a SINGLE account left the dangerous case open: two
    # DIFFERENT accounts of the same (owner, mint) could each be internally consistent
    # while declaring 6 and 9. ``outflows()`` summed their raw deltas and stamped whichever
    # decimals it happened to see last onto the total — 50_000_000 raw rendered as
    # "0.050000000" instead of "50.000000", a silent 1000x UNDER-report against any
    # ui-denominated cap, with ``status="measured"`` and no refusal to warn anyone.
    #
    # So a disagreement ANYWHERE in the report is a refusal, never a resolved conflict: we
    # cannot know which reading is the true scale, and picking one is picking an amount.
    # This subsumes the old per-account pre/post check (a disagreement across one
    # account's two rows is a same-mint disagreement), which is why there is no longer a
    # separate one — two guards emitting for one fact would double-count the refusal.
    for mint in sorted(declared_decimals):
        if len(declared_decimals[mint]) < 2:
            continue
        scales = ", ".join(str(d) for d in sorted(declared_decimals[mint]))
        refusals.append(
            TokenDeltaRefusal(
                reason="decimals-inconsistent",
                mint=mint,
                detail=(
                    f"the report declares more than one decimals value ({scales}) for one "
                    "mint; a mint's scale is immutable, so at least one reading is wrong "
                    "and an amount rendered at the wrong scale is the wrong amount"
                ),
            )
        )
    inconsistent = {mint for mint, seen in declared_decimals.items() if len(seen) > 1}
    if inconsistent:
        # Drop the movements of a mint whose scale is in dispute. The report is already
        # unmeasurable, but a movement carrying one of two contradictory scales is a
        # half-truth sitting on the object for someone to read off it.
        movements = [
            movement for movement in movements if movement.mint not in inconsistent
        ]

    return TokenDeltaReport(
        status="unmeasurable" if refusals else "measured",
        movements=tuple(movements),
        refusals=tuple(refusals),
    )


class SimulateError(Exception):
    """A build or transport failure that is NOT a program revert — e.g. the builder
    returned no transaction. A program revert is not an error: it is a Receipt with
    ``status="fail"`` and a ``revert_class``. Messages never echo a raw request body."""


@dataclass(frozen=True)
class Receipt:
    """The legible outcome of simulating a built transaction against a state snapshot.

    ``status`` is the land/no-land verdict; ``revert_class`` is a CATEGORICAL string (the
    corpus vocabulary), never a fabricated number. ``sol_delta``/``tokens_received`` are
    best-effort and ``None`` unless the relevant account was tracked and decodable.
    ``network_label`` is the always-present honesty caveat (snapshot, not mainnet).

    THE FIELD SET, STATED ONCE (several branches reshape this dataclass in parallel, and
    two of them appending "at the end" is how a field gets dropped in a hand-resolved
    merge — this is the canonical order):

    1. ``status``, ``err``, ``revert_class``, ``units_consumed``, ``sol_delta``,
       ``tokens_received``, ``logs_tail``, ``network_label`` — the original eight,
       required, unchanged.
    2. ``message_binding``, ``binding_strength`` — the signing binding.
    3. ``lookup_resolution`` — whether the message loads accounts through an address
       lookup table. A gate input, categorical, outside the corpus projection.
    4. ``network`` — D2. Structured, closed vocabulary, the ONLY network input the gate
       reads. Defaults to ``unknown``, which approves nothing.
    5. ``observed_slot`` — D6. Recorded, never enforced.
    6. ``token_delta`` — G6. The TYPED token leg: what moved, in which mint, at which
       scale — or an explicit refusal. ``None`` means NOT TRACKED, never zero.

    ``tokens_received`` in group 1 is SUPERSEDED by ``token_delta`` and is now
    permanently ``None`` from :func:`simulate`. It stays on the dataclass only because
    other modules construct Receipts positionally; it must never become a second, weaker
    source an amount can be read from, because an ``int`` there carries no mint and no
    decimals — a bare 25000000 is 25 USDC or 0.025 of a 9-decimal mint, and nothing on
    the field says which.
    """

    status: Literal["pass", "fail", "unknown"]
    err: Any | None
    revert_class: str | None
    units_consumed: int | None
    sol_delta: int | None
    tokens_received: int | None
    logs_tail: tuple[str, ...]
    network_label: str
    #: sha256 over the transaction's MESSAGE — what makes this Receipt attest THIS
    #: transaction rather than "some plan like it". ``None`` when the tx could not be
    #: decoded locally; a caller that needs the binding must treat that as a refusal.
    message_binding: str | None = None
    #: How much of the message the binding covers. ``structural`` omits the blockhash,
    #: which is the honest ceiling while we simulate with ``replaceRecentBlockhash``.
    binding_strength: str | None = None
    #: Whether the simulated message loaded any account from an address lookup table —
    #: the one case where a binding over the bytes cannot see the accounts (a v0 message
    #: carries the table address and u8 indexes, never the addresses). ``unresolved``
    #: means no binding was computable and the signing gate refuses; ``none`` means every
    #: account is in the message and the binding covers all of them. ``None`` is a build
    #: we could not decode, or a Receipt older than this field — it claims nothing, and
    #: the gate still refuses it for want of a binding.
    #:
    #: CATEGORICAL and control-plane: this is a gate input, never an outcome. It does not
    #: enter the corpus projection (``corpus.simulated_outcome_from``), and no resolved
    #: ADDRESS is recorded here or anywhere else on the Receipt.
    lookup_resolution: LookupResolution | None = None
    #: WHICH NETWORK this simulation ran against — structured, closed vocabulary, and the
    #: ONLY network input the signing gate reads. ``network_label`` beside it is prose for
    #: a human and is never compared: the default label is literally
    #: "simulated (fork/RPC snapshot — not mainnet)", so a substring test for "mainnet"
    #: matches a FORK receipt and inverts the gate into an approval.
    #:
    #: The DATACLASS default is ``unknown`` — the member that claims nothing — so a
    #: Receipt built by older or third-party code can never silently assert a network it
    #: was never told. ``simulate`` itself has no default for it: this field is the one a
    #: gate reads, so the run that produces it must be asked. Nothing infers it from the
    #: RPC URL (a fork proxy answers at any hostname) or from the label.
    network: Network = UNKNOWN_NETWORK
    #: The slot ``result.context.slot`` reported for the snapshot this ran against, or
    #: ``None`` when the RPC sent nothing usable.
    #:
    #: RECORDED, NOT ENFORCED. Nothing reads this to decide anything — not the signing
    #: gate, not the handoff. It exists so a receipt-age bound BECOMES EXPRESSIBLE
    #: ("refuse a receipt more than N slots old"); that bound is NOT built, and a receipt
    #: still does not expire. Named ``observed_slot`` rather than anything ending in
    #: ``_at``/``_valid`` so it cannot be misread as a freshness guarantee.
    observed_slot: int | None = None
    #: The TOKEN leg of this simulation — G6. ``sol_delta`` beside it answers only "how
    #: many lamports left the payer", which for a USDC purchase is ~0 while 25 USDC
    #: leaves the account: a lamport-denominated cap reads that drain as nothing.
    #:
    #: THREE STATES, none of them interchangeable. ``None`` = NOT TRACKED (the simulation
    #: carried no token balances at all — the stock ``simulateTransaction`` case); a
    #: report with ``status="measured"`` and no movements = an OBSERVED zero; a report
    #: with ``status="unmeasurable"`` = we will not put a number on it, and reading one
    #: off it raises rather than returning 0.
    #:
    #: Control-plane: mint, decimals, owner and amounts only. No token-account address,
    #: no payload, and it is not projected into the corpus.
    token_delta: TokenDeltaReport | None = None


def _custom_code(err: Any) -> int | None:
    """The Anchor ``Custom`` error code from an ``InstructionError``, if present."""
    if isinstance(err, dict):
        ie = err.get("InstructionError")
        if isinstance(ie, list) and len(ie) == 2 and isinstance(ie[1], dict):
            code = ie[1].get("Custom")
            if isinstance(code, int):
                return code
    return None


def classify_revert(err: Any, logs: Sequence[str]) -> str | None:
    """Map a simulation ``err`` + logs to a STABLE categorical revert class.

    These keys are the corpus vocabulary later — do not rename casually. A dollar number
    is never fabricated; the class is a string. The SEMANTIC log-based classes (slippage,
    account_error, insufficient_funds) win over the raw ``custom_program_error:<code>``:
    the same Anchor code (e.g. 3012 = AccountNotInitialized) is far more actionable named
    than numbered, and the logs carry the name.
    """
    if err is None:
        return None
    log_text = " ".join(logs).lower()
    if any(marker in log_text for marker in _SLIPPAGE_MARKERS):
        return "slippage"
    if any(marker in log_text for marker in _ACCOUNT_MARKERS):
        return "account_error"
    if any(marker in log_text for marker in _INSUFFICIENT_MARKERS):
        return "insufficient_funds"
    custom = _custom_code(err)
    if custom is not None:
        return f"custom_program_error:{custom}"
    return "other"


def revert_family(revert_class: str | None) -> tuple[str, int | None]:
    """Split a ``classify_revert`` output into a CLOSED family + an optional public code.

    The corpus stores the family (a ``REVERT_FAMILIES`` member) and the numeric error
    code SEPARATELY — a code is a public program constant (like an HTTP status), never a
    value. ``None`` (no revert) → ``("none", None)``; ``"custom_program_error:3012"`` →
    ``("custom_program_error", 3012)``; every other class carries no code →
    ``(<class>, None)``. Fails CLOSED: an unrecognized family collapses to ``"other"`` so
    a drifted classifier can never smuggle a non-vocabulary string into the corpus.
    """
    if revert_class is None:
        return ("none", None)
    if revert_class.startswith(_CUSTOM_FAMILY + ":"):
        code_text = revert_class[len(_CUSTOM_FAMILY) + 1 :]
        try:
            return (_CUSTOM_FAMILY, int(code_text))
        except ValueError:
            return (_CUSTOM_FAMILY, None)
    if revert_class in REVERT_FAMILIES:
        return (revert_class, None)
    return ("other", None)


def _default_build_call(plan: Mapping[str, Any]) -> BuiltTx:
    """POST the plan to its ``build_url`` and extract the serialized tx + its encoding.

    The builder (Orquestra ``/build``) is a user-configured HTTP target, not ingested
    spec content — scheme is gated to http/https, same posture as the RPC endpoint.

    Prefers ``serializedTransaction`` — Orquestra returns TWO tx fields, and the plain
    ``transaction`` one is oversized (exceeds the 1232-byte tx limit, unusable for
    ``simulateTransaction``) while ``serializedTransaction`` is the real signable tx. The
    encoding is read from the response (``encoding``), defaulting to ``base64`` for
    builders that omit it. Raises :class:`SimulateError` if no tx field is present.
    """
    url = str(plan["build_url"])
    validate_rpc_url(url)
    body = json.dumps(
        {
            "accounts": plan.get("accounts"),
            "args": plan.get("args"),
            "feePayer": plan.get("feePayer"),
        }
    ).encode()
    try:
        resp = _http_post_json(url, body)
    except urllib.error.HTTPError as exc:
        # A build-transport failure (auth, bad payload) — NOT a program revert. Surface
        # the status + url only; never echo the request/response body (redaction posture).
        raise SimulateError(
            f"build POST to {url} failed: HTTP {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SimulateError(f"build POST to {url} failed: {exc.reason}") from exc
    encoding = (
        resp.get("encoding") if isinstance(resp.get("encoding"), str) else "base64"
    )
    for key in ("serializedTransaction", "transaction", "tx"):
        tx = resp.get(key)
        if isinstance(tx, str) and tx:
            return BuiltTx(tx=tx, encoding=str(encoding))
    raise SimulateError(
        f"build response from {url} carried no transaction "
        "(tried keys: serializedTransaction, transaction, tx)"
    )


def _context_slot(result: Any) -> int | None:
    """The slot from a JSON-RPC ``result.context``, or ``None`` — never a coerced value.

    This arrives over untrusted transport, so it is TYPE-CHECKED rather than trusted:
    only a plain non-negative ``int`` is a slot. A numeric string is not coerced (that
    would let ``"318492001"`` and ``318492001`` be the same receipt), a float is not
    truncated, and ``bool`` is excluded explicitly — ``isinstance(True, int)`` is True in
    Python, so a naive check admits ``True`` and records slot 1.

    Absence is ``None``, never ``0``: zero is a CLAIM ("slot zero"), and a false one.
    """
    if not isinstance(result, Mapping):
        return None
    context = result.get("context")
    if not isinstance(context, Mapping):
        return None
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
        return None
    return slot


def _tracked_lamports(value: Any) -> int | None:
    """Pull ``lamports`` out of a getAccountInfo/simulate account object, if present."""
    if isinstance(value, dict):
        lamports = value.get("lamports")
        if isinstance(lamports, int):
            return lamports
    return None


def simulate(
    plan: Mapping[str, Any],
    *,
    rpc_url: str,
    rpc_call: RpcCall | None = None,
    build_call: BuildCall | None = None,
    track: Sequence[str] = (),
    network_label: str = _DEFAULT_NETWORK_LABEL,
    network: Network,
    replace_blockhash: bool = True,
    mint_extensions: Mapping[str, Sequence[str]] | None = None,
) -> Receipt:
    """Build ``plan`` into a tx and simulate it → a :class:`Receipt`.

    Never signs or broadcasts — ``simulateTransaction`` only. ``rpc_call`` and
    ``build_call`` are injectable so this is fully falsifiable offline. ``track`` is an
    ordered list of accounts to snapshot (``track[0]`` powers ``sol_delta``). The Receipt
    is returned, never stored.

    ``network`` is ASSERTED by the caller and never derived here — not from ``rpc_url``
    (a fork proxy answers at any hostname) and not from ``network_label`` (prose; the
    default one names mainnet while meaning the opposite). The two travel side by side
    WITHOUT a consistency check on purpose: a check that refused a "contradiction" would
    make the prose load-bearing again, which is the D2 bug wearing a different hat. The
    field is the fact; the label is a sentence about it.

    It is keyword-only with **NO DEFAULT**, and that is a deliberate reversal. The earlier
    draft defaulted it to :data:`~gecko.networks.UNKNOWN_NETWORK` and argued the default
    was safe because unknown approves nothing. It is safe and it is still wrong: a default
    lets a run that stated nothing travel to the signing gate looking exactly like a run
    somebody thought about, and the caller who DID know — the operator with the flag in
    their hand — never gets asked. So mypy asks instead, at every call site. A caller that
    genuinely cannot know passes ``UNKNOWN_NETWORK`` explicitly; that is a sentence, not
    a silence, and it still approves nothing.
    """
    validate_rpc_url(rpc_url)
    call = rpc_call or default_rpc_call
    builder = build_call or _default_build_call

    built = builder(plan)
    tracked = list(track)

    pre_lamports: int | None = None
    if tracked:
        pre = call(rpc_url, "getAccountInfo", [tracked[0], {"encoding": "base64"}])
        pre_lamports = _tracked_lamports((pre.get("result") or {}).get("value"))

    # The tx carries its own encoding (Orquestra returns base58); getAccountInfo is a
    # separate read and stays base64. Passing the tx's own encoding avoids a silent
    # simulateTransaction decode failure.
    sim_config: dict[str, Any] = {
        "encoding": built.encoding,
        "sigVerify": False,
        # Replacing the blockhash is right for a plan-shaped check and wrong for a
        # pre-signature one: the simulation then ran against a DIFFERENT message than the
        # one that would be signed, so the receipt can only bind structurally. Pass
        # replace_blockhash=False with a real, fresh blockhash to earn an `exact` binding
        # — and inherit its ~150-slot expiry along with it.
        "replaceRecentBlockhash": replace_blockhash,
        "commitment": "processed",
    }
    if tracked:
        sim_config["accounts"] = {"encoding": "base64", "addresses": tracked}

    # Bind the Receipt to the exact message being simulated, so a signer can later prove
    # the transaction in front of it is this one. Best-effort: a builder we cannot decode
    # yields no binding, and `evaluate_tx` refuses on a missing binding rather than
    # assuming — never the reverse.
    #
    # A message that loads accounts from an address lookup table is the one case that is
    # not "we could not compute it" but "no honest binding exists": the bytes commit to
    # the table and the indexes, not to the addresses. `_bind` raises there, and the
    # resolution recorded a line earlier survives to say so on the Receipt.
    binding: str | None = None
    strength: str | None = None
    resolution: LookupResolution | None = None
    try:
        from .txbind import message_binding as _bind

        from .txbind import BindingStrength, lookup_resolution_of

        resolution = lookup_resolution_of(built.tx, encoding=built.encoding)
        chosen: BindingStrength = "structural" if replace_blockhash else "exact"
        binding = _bind(built.tx, encoding=built.encoding, strength=chosen)
        strength = chosen
    except Exception:  # noqa: BLE001 - a binding we cannot compute is absent, not fatal
        binding = None
        strength = None

    sim = call(rpc_url, "simulateTransaction", [built.tx, sim_config])
    result = sim.get("result") or {}
    value = result.get("value") or {}
    # ``result.context.slot`` used to be dropped on the floor here, which left nothing on
    # the Receipt saying WHEN the snapshot was — an age bound was not merely unenforced,
    # it was inexpressible.
    observed_slot = _context_slot(result)

    err = value.get("err")
    logs = value.get("logs") or []
    status: Literal["pass", "fail", "unknown"] = "pass" if err is None else "fail"
    revert_class = classify_revert(err, logs)
    units = value.get("unitsConsumed")
    units_consumed = units if isinstance(units, int) else None

    sol_delta: int | None = None
    post_accounts = value.get("accounts")
    if tracked and isinstance(post_accounts, list) and post_accounts:
        post_lamports = _tracked_lamports(post_accounts[0])
        if pre_lamports is not None and post_lamports is not None:
            sol_delta = post_lamports - pre_lamports

    # The token leg, denominated in each mint rather than in lamports. ``None`` here means
    # the simulation carried no token balances (stock simulateTransaction returns none) —
    # NOT TRACKED, never zero. A Token-2022 mint whose extensions were not read, or whose
    # extensions make the delta not the debit, comes back REFUSED rather than numbered.
    token_delta = parse_token_deltas(value, mint_extensions=mint_extensions)

    # SUPERSEDED by token_delta and permanently None. A bare int carries no mint and no
    # decimals, so it cannot be compared against anything safely; leaving it fillable
    # would recreate the weaker parallel source token_delta exists to replace.
    tokens_received: int | None = None

    return Receipt(
        status=status,
        err=err,
        revert_class=revert_class,
        units_consumed=units_consumed,
        sol_delta=sol_delta,
        tokens_received=tokens_received,
        logs_tail=tuple(logs[-12:]),
        network_label=network_label,
        message_binding=binding,
        binding_strength=strength,
        lookup_resolution=resolution,
        network=network,
        observed_slot=observed_slot,
        token_delta=token_delta,
    )

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
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence

from .rpc import RpcCall, _http_post_json, default_rpc_call, validate_rpc_url
from .txbind import LookupResolution

__all__ = [
    "BuildCall",
    "BuiltTx",
    "REVERT_FAMILIES",
    "Receipt",
    "SimulateError",
    "classify_revert",
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
    replace_blockhash: bool = True,
) -> Receipt:
    """Build ``plan`` into a tx and simulate it → a :class:`Receipt`.

    Never signs or broadcasts — ``simulateTransaction`` only. ``rpc_call`` and
    ``build_call`` are injectable so this is fully falsifiable offline. ``track`` is an
    ordered list of accounts to snapshot (``track[0]`` powers ``sol_delta``). The Receipt
    is returned, never stored.
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
    value = (sim.get("result") or {}).get("value") or {}

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

    # tokens_received stays best-effort: only a decoded tracked token account would fill
    # it, which is out of scope until a token-account decode is wired — never fabricated.
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
    )

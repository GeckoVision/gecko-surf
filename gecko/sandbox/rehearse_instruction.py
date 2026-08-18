"""Rehearse ANY instruction on a proven local fork — and report what moved.

`rehearse_purchase` does this for one instruction of one store. This is the general form,
and it differs in one honest respect that is worth stating before the code:

**IT REPORTS, IT DOES NOT JUDGE.** A purchase can be judged because the price is knowable
from the store's own account, so the run has an oracle to check the ledger against. A
general instruction has no such oracle: nobody can say what `settle_success` *should* move
without re-implementing the program. Inventing an expectation here and then checking
against it would be the internal-consistency trap this repo has already been caught by
once — a run agreeing with itself proves nothing. So the result carries the deltas, the
logs and the compute, and leaves the verdict to whoever knows what they intended.

That is still the thing a developer cannot get anywhere else: what this call actually does
to real mainnet state, before doing it for real, at no cost.

THE THREE BINDINGS, inherited from the purchase rehearsal and non-negotiable:
  * the signer's `rpc_url` must be the proven surfnet's — the key that signs and the chain
    it signs for cannot drift apart;
  * the transaction is built against the FORK, never mainnet, and a URL guard admits
    exactly one URL;
  * nothing here can reach mainnet: the key is born inside the call and discarded with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..prepare_instruction import prepare_instruction_result
from ..rpc import RpcCall, default_rpc_call
from .cheatcodes import _decode_token_account, fund_sol, fund_token
from .rehearse import (
    CONFIRM_TIMEOUT_SECONDS,
    DEFAULT_FEE_LAMPORTS,
    LamportDelta,
    Refusal,
    RehearsalError,
    TokenDelta,
    _account,
    _confirm,
    _lamports,
    _only_this_surfnet,
    _sign,
    _transaction_meta,
)
from .surfnet import EphemeralSigner, SurfnetProof

__all__ = [
    "InstructionRehearsal",
    "rehearse_instruction",
]


@dataclass(frozen=True)
class InstructionRehearsal:
    """One instruction, rehearsed. Every field is an observation, none is a verdict."""

    program_id: str
    instruction: str
    #: the fork this ran on, and the evidence it IS a fork
    rpc_url: str
    signer: str
    landed: bool = False
    signature: str | None = None
    compute_units: int | None = None
    error: Any = None
    accounts: Mapping[str, str] = field(default_factory=dict)
    account_origins: Sequence[Mapping[str, str]] = field(default_factory=tuple)
    lamport_deltas: Sequence[LamportDelta] = field(default_factory=tuple)
    token_deltas: Sequence[TokenDelta] = field(default_factory=tuple)
    logs: Sequence[str] = field(default_factory=tuple)
    refusals: Sequence[Refusal] = field(default_factory=tuple)

    @property
    def moved_anything(self) -> bool:
        """True iff some token account's balance changed.

        Deliberately NOT called `succeeded`. A call can land, cost compute and move
        nothing — `mark_as_delivered` is exactly that — and a run that reported those as
        failure would teach a developer the wrong thing about their own program.
        """
        return any(d.moved for d in self.token_deltas if d.moved)


def _token_snapshot(
    call: RpcCall, rpc_url: str, accounts: Sequence[str]
) -> dict[str, tuple[str | None, str | None, int | None]]:
    """``{address: (mint, owner, amount)}`` for those accounts that decode as SPL tokens.

    The account mapping goes to the decoder untouched: it reads the RPC's own base64
    payload, and re-wrapping the bytes here would mean two decoders that can disagree.
    An address that is not a token account is simply absent — not an error, because most
    accounts in an instruction are not token accounts.
    """
    out: dict[str, tuple[str | None, str | None, int | None]] = {}
    for address in accounts:
        decoded = _decode_token_account(_account(call, rpc_url, address))
        if decoded[0] is None:
            continue
        out[address] = decoded
    return out


def rehearse_instruction(
    proof: SurfnetProof,
    *,
    signer: EphemeralSigner,
    program_id: str,
    instruction: str,
    values: Mapping[str, Any],
    idl_fetch: Any,
    build_call: Any,
    fund_tokens: Sequence[tuple[str, int]] = (),
    fee_lamports: int = DEFAULT_FEE_LAMPORTS,
    rpc_call: RpcCall | None = None,
) -> InstructionRehearsal:
    """Fund, prepare, sign, land and observe — one instruction, on a proven surfnet.

    ``signer`` is an :class:`~gecko.sandbox.surfnet.EphemeralSigner` rather than a pubkey,
    for the same reason the purchase rehearsal insists on one: the address that pays and
    the key that signs must be the same object, and a string would let them drift.

    ``fund_tokens`` is ``[(mint, raw_amount), …]`` — the balances to place on the signer
    before the call, by cheatcode. Stated by the caller because only they know what the
    instruction needs; this function will not guess a funding plan from an IDL.

    Raises :class:`RehearsalError` only for a binding that does not hold. Everything
    else — a refused preparation, a transaction that reverts — comes back in the result.
    """
    if signer.rpc_url != proof.rpc_url:
        raise RehearsalError(
            f"the signer is bound to {signer.rpc_url!r} and the proven surfnet is "
            f"{proof.rpc_url!r}; a key that signs for one chain must not be used on another"
        )
    call = rpc_call or default_rpc_call
    refusals: list[Refusal] = []

    fund_sol(proof, signer.pubkey, fee_lamports, rpc_call=call)
    watched: list[str] = []
    for mint, amount in fund_tokens:
        funded = fund_token(proof, signer.pubkey, mint, amount, rpc_call=call)
        watched.append(funded.token_account)

    prepared = prepare_instruction_result(
        {
            "program_id": program_id,
            "instruction": instruction,
            "payer": signer.pubkey,
            "values": dict(values),
        },
        idl_fetch=idl_fetch,
        build_call=build_call,
        # No simulation here: this run LANDS the transaction, and simulating first would
        # only tell us about a state the send then changes.
    )
    if prepared["refused"]:
        refusals.append(Refusal("prepare", f"{prepared['code']}: {prepared['reason']}"))
        return InstructionRehearsal(
            program_id=program_id,
            instruction=instruction,
            rpc_url=proof.rpc_url,
            signer=signer.pubkey,
            refusals=tuple(refusals),
        )

    accounts: dict[str, str] = dict(prepared["accounts"])
    watched = list(dict.fromkeys(watched + list(accounts.values())))
    before_tokens = _token_snapshot(call, proof.rpc_url, watched)
    before_sol = {
        a: _lamports(_account(call, proof.rpc_url, a)) for a in accounts.values()
    }

    # The guard admits exactly ONE url — the surfnet that proved itself — so a build or a
    # send cannot be redirected at anything else, mainnet included.
    _only_this_surfnet(proof)

    # `_sign` answers (signed base64, signature base58) — in that order.
    signed_base64, signature = _sign(prepared["transaction_base64"], signer)
    sent = call(
        proof.rpc_url,
        "sendTransaction",
        [signed_base64, {"encoding": "base64", "skipPreflight": False}],
    )
    if "error" in sent:
        refusals.append(Refusal("send", str(sent["error"])[:200]))
        return InstructionRehearsal(
            program_id=program_id,
            instruction=instruction,
            rpc_url=proof.rpc_url,
            signer=signer.pubkey,
            accounts=accounts,
            account_origins=tuple(prepared["account_origins"]),
            refusals=tuple(refusals),
        )

    # `_confirm` polls to its OWN deadline; wrapping it in a second loop would compound
    # one 30-second wait into minutes on the failure path.
    landed = _confirm(call, proof.rpc_url, signature)
    if not landed:
        refusals.append(
            Refusal("confirm", f"not confirmed within {CONFIRM_TIMEOUT_SECONDS:.0f}s")
        )

    meta = _transaction_meta(call, proof.rpc_url, signature)
    after_tokens = _token_snapshot(call, proof.rpc_url, watched)
    after_sol = {
        a: _lamports(_account(call, proof.rpc_url, a)) for a in accounts.values()
    }

    token_deltas = []
    for address in watched:
        pre, post = before_tokens.get(address), after_tokens.get(address)
        if pre is None and post is None:
            continue
        decoded = post or pre or (None, None, None)
        token_deltas.append(
            TokenDelta(
                account=address,
                mint=decoded[0] or "",
                owner=decoded[1] or "",
                before=pre[2] if pre else None,
                after=post[2] if post else None,
            )
        )
    lamport_deltas = [
        LamportDelta(address=a, before=before_sol.get(a), after=after_sol.get(a))
        for a in accounts.values()
        if before_sol.get(a) != after_sol.get(a)
    ]

    return InstructionRehearsal(
        program_id=program_id,
        instruction=instruction,
        rpc_url=proof.rpc_url,
        signer=signer.pubkey,
        landed=landed,
        signature=signature,
        compute_units=(meta or {}).get("computeUnitsConsumed"),
        error=(meta or {}).get("err"),
        accounts=accounts,
        account_origins=tuple(prepared["account_origins"]),
        lamport_deltas=tuple(lamport_deltas),
        token_deltas=tuple(token_deltas),
        logs=tuple((meta or {}).get("logMessages") or ())[-15:],
        refusals=tuple(refusals),
    )

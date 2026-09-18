"""A Meteora DLMM swap, landed on a proven surfnet and judged by what moved.

The DLMM landing orchestrator (:func:`gecko.providers.meteora_landing.assemble_swap_landing`)
assembles the bundle the swap needs (idempotent ATAs, the wSOL wrap when the input is
SOL, the swap with its recovered bin arrays, the unwrap) and simulates it. This lane takes
the same bundle, sizes the compute budget from the simulation, signs it with an ephemeral
key that exists only because the endpoint proved it is a fork, lands it, and reads the
ledger: the output token account rose by at least the state-quoted floor, the input side
fell, the signer paid the fee. Same bindings the orchestrator takes; same three fork
bindings the purchase rehearsal keeps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

from ..landing import (
    NATIVE_SOL_MINT,
    assemble_unsigned_tx,
    compute_budget_ixs,
    latest_blockhash,
    simulate_landing_bundle,
)
from ..providers.meteora_landing import (
    FetchSwapInstruction,
    SwapLandingBundle,
    assemble_swap_landing,
)
from ..rpc import RpcCall, default_rpc_call
from .cheatcodes import _decode_token_account, fund_sol, fund_token, reset_account
from .rehearse import (
    DEFAULT_FEE_LAMPORTS,
    LamportDelta,
    Refusal,
    RehearsalError,
    TokenDelta,
    _account,
    _confirm,
    _lamports,
    _sign,
    _transaction_meta,
)
from .surfnet import EphemeralSigner, SurfnetProof

__all__ = ["ReceivedWitness", "SwapRehearsal", "rehearse_dlmm_swap"]

#: Where the received amount was read. A SOL output does not stay in a token account:
#: the bundle's postlude closes the wSOL account, so the output arrives as native
#: lamports and the only honest witness is the signer's balance, fee added back.
#: MEASURED (fork, 2026-09-18): 1 USDC -> SOL landed with the wSOL account gone and the
#: signer up 4,273,542 = 4,278,542 received - 5,000 fee; the token-account read said None.
ReceivedWitness = Literal["token-account", "native-sol"]


@dataclass(frozen=True)
class SwapRehearsal:
    """One DLMM swap, rehearsed. Observations, not verdicts, except ``discrepancies``."""

    pool: str
    input_mint: str
    output_mint: str
    amount_in: int
    min_amount_out: int
    expected_out_snapshot: int
    bin_array_indexes: tuple[int, ...]
    signer: str
    landed: bool = False
    signature: str | None = None
    simulated_units: int | None = None
    units_consumed: int | None = None
    unit_limit: int | None = None
    fee_lamports: int | None = None
    token_out: TokenDelta | None = None
    token_in: TokenDelta | None = None
    signer_sol: LamportDelta | None = None
    received_amount: int | None = None
    received_witness: ReceivedWitness = "token-account"
    refusals: tuple[Refusal, ...] = ()
    discrepancies: tuple[str, ...] = ()
    reset: tuple[str, ...] = field(default_factory=tuple)

    @property
    def received(self) -> int | None:
        """What the signer got out of the swap, by the witness ``received_witness`` names."""
        return self.received_amount


def rehearse_dlmm_swap(
    proof: SurfnetProof,
    *,
    signer: EphemeralSigner,
    bindings: Mapping[str, Any],
    rpc_call: RpcCall | None = None,
    fetch_swap_instruction: FetchSwapInstruction | None = None,
    fee_lamports: int = DEFAULT_FEE_LAMPORTS,
    slippage_bps: int | None = None,
) -> SwapRehearsal:
    """Fund, assemble, size, sign, land, judge and reset one DLMM swap.

    ``bindings`` are the orchestrator's (``input_mint``, ``output_mint``, ``bin_step``,
    ``base_factor``, ``amount_in``); ``user`` is the ephemeral signer and is not taken
    from the caller, for the reason every rehearsal gives: the address that pays and the
    key that signs must be one object.
    """
    if signer.rpc_url != proof.rpc_url:
        raise RehearsalError(
            f"this signer was proven against {signer.rpc_url} and the proof names "
            f"{proof.rpc_url}; a key is bound to the one fork that proved itself"
        )
    call = rpc_call or default_rpc_call
    bound = {**dict(bindings), "user": signer.pubkey}
    input_mint = str(bound["input_mint"])
    output_mint = str(bound["output_mint"])
    amount_in = int(bound["amount_in"])
    refusals: list[Refusal] = []
    touched: list[str] = [signer.pubkey]

    # 1. FUND. SOL always (the fee, and the input when the input is SOL: the bundle's
    #    wrap prelude moves it into the wSOL account). A token input is placed by
    #    cheatcode on the signer's ATA.
    lamports = fee_lamports + (amount_in if input_mint == NATIVE_SOL_MINT else 0)
    fund_sol(proof, signer.pubkey, lamports, rpc_call=call)
    if input_mint != NATIVE_SOL_MINT:
        funded = fund_token(proof, signer.pubkey, input_mint, amount_in, rpc_call=call)
        touched.append(funded.token_account)

    def _blank(**kw: Any) -> SwapRehearsal:
        return SwapRehearsal(
            pool=kw.pop("pool", "?"),
            input_mint=input_mint,
            output_mint=output_mint,
            amount_in=amount_in,
            min_amount_out=kw.pop("min_amount_out", 0),
            expected_out_snapshot=kw.pop("expected_out_snapshot", 0),
            bin_array_indexes=tuple(kw.pop("bin_array_indexes", ())),
            signer=signer.pubkey,
            **kw,
        )

    try:
        return _run(
            proof,
            signer=signer,
            bound=bound,
            call=call,
            fetch=fetch_swap_instruction,
            slippage_bps=slippage_bps,
            touched=touched,
            blank=_blank,
            refusals=refusals,
        )
    finally:
        pass


def _run(
    proof: SurfnetProof,
    *,
    signer: EphemeralSigner,
    bound: Mapping[str, Any],
    call: RpcCall,
    fetch: FetchSwapInstruction | None,
    slippage_bps: int | None,
    touched: list[str],
    blank: Any,
    refusals: list[Refusal],
) -> SwapRehearsal:
    input_mint = str(bound["input_mint"])
    output_mint = str(bound["output_mint"])

    # 2. ASSEMBLE: the orchestrator's bundle, unchanged.
    kwargs: dict[str, Any] = {"rpc_url": proof.rpc_url, "rpc_call": call}
    if fetch is not None:
        kwargs["fetch_swap_instruction"] = fetch
    if slippage_bps is not None:
        kwargs["slippage_bps"] = slippage_bps
    try:
        bundle: SwapLandingBundle = assemble_swap_landing(bound, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a refused plan is an answer
        refusals.append(Refusal("assemble", f"{type(exc).__name__}: {exc}"))
        return _finish(blank(refusals=tuple(refusals)), proof, touched, call)
    plan = bundle.plan
    accounts = bundle.accounts
    min_amount_out = int(plan["args"]["min_amount_out"])
    facts: dict[str, Any] = {
        "pool": accounts["lb_pair"],
        "min_amount_out": int(plan["args"]["min_amount_out"]),
        "expected_out_snapshot": int(plan["quote"]["expected_out_snapshot"]),
        "bin_array_indexes": tuple(bundle.bin_array_indexes),
    }
    touched.extend([accounts["user_token_in"], accounts["user_token_out"]])

    # 3. SIZE the compute budget by simulating the exact bundle.
    receipt, unit_limit = simulate_landing_bundle(
        bundle.swap_ix_complete,
        bundle.prelude_ixs,
        signer.pubkey,
        rpc_url=proof.rpc_url,
        rpc_call=call,
        track=[signer.pubkey],
        network="fork",
        postlude_ixs=bundle.postlude_ixs,
    )
    if receipt.status != "pass":
        refusals.append(
            Refusal(
                "simulate",
                f"status={receipt.status} class={receipt.revert_class}",
            )
        )
        return _finish(
            blank(
                simulated_units=receipt.units_consumed,
                refusals=tuple(refusals),
                **facts,
            ),
            proof,
            touched,
            call,
        )

    # 4. BUILD the bytes that will be signed, with a fresh blockhash from the fork.
    blockhash, _ = latest_blockhash(proof.rpc_url, call)
    unsigned = assemble_unsigned_tx(
        [*compute_budget_ixs(unit_limit), *bundle.instructions],
        signer.pubkey,
        blockhash=blockhash,
    ).tx

    before_out = _decode_token_account(
        _account(call, proof.rpc_url, accounts["user_token_out"])
    )
    before_in = _decode_token_account(
        _account(call, proof.rpc_url, accounts["user_token_in"])
    )
    sol_before = _lamports(_account(call, proof.rpc_url, signer.pubkey))

    # 5. SIGN and LAND at the proven endpoint.
    signed, signature = _sign(unsigned, signer)
    sent = call(
        proof.rpc_url,
        "sendTransaction",
        [
            signed,
            {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
            },
        ],
    )
    landed_signature = (sent or {}).get("result")
    if not isinstance(landed_signature, str):
        refusals.append(
            Refusal("land", f"the node returned no signature: {str(sent)[:160]}")
        )
        return _finish(
            blank(
                simulated_units=receipt.units_consumed,
                unit_limit=unit_limit,
                refusals=tuple(refusals),
                **facts,
            ),
            proof,
            touched,
            call,
        )
    confirmed = _confirm(call, proof.rpc_url, landed_signature)
    if not confirmed:
        refusals.append(Refusal("confirm", "not confirmed; nothing below is evidence"))
        return _finish(
            blank(
                signature=landed_signature,
                simulated_units=receipt.units_consumed,
                unit_limit=unit_limit,
                refusals=tuple(refusals),
                **facts,
            ),
            proof,
            touched,
            call,
        )

    # 6. JUDGE by what moved.
    meta = _transaction_meta(call, proof.rpc_url, landed_signature) or {}
    after_out = _decode_token_account(
        _account(call, proof.rpc_url, accounts["user_token_out"])
    )
    after_in = _decode_token_account(
        _account(call, proof.rpc_url, accounts["user_token_in"])
    )
    sol_after = _lamports(_account(call, proof.rpc_url, signer.pubkey))
    token_out = TokenDelta(
        account=accounts["user_token_out"],
        owner=signer.pubkey,
        mint=output_mint,
        before=before_out[2],
        after=after_out[2],
    )
    token_in = TokenDelta(
        account=accounts["user_token_in"],
        owner=signer.pubkey,
        mint=input_mint,
        before=before_in[2],
        after=after_in[2],
    )
    signer_sol = LamportDelta(address=signer.pubkey, before=sol_before, after=sol_after)
    fee = meta.get("fee") if isinstance(meta.get("fee"), int) else None
    received, witness = _received(
        output_mint, token_out=token_out, signer_sol=signer_sol, fee=fee
    )
    problems: list[str] = []
    if received is None or received < min_amount_out:
        problems.append(
            f"the {witness} witness says {received!r} received, below the state-quoted "
            f"floor {min_amount_out}"
        )
    if input_mint != NATIVE_SOL_MINT and token_in.moved != -int(bound["amount_in"]):
        problems.append(
            f"the input account moved {token_in.moved!r}, not -{bound['amount_in']}"
        )
    if input_mint == NATIVE_SOL_MINT and (
        signer_sol.moved is None or signer_sol.moved > -int(bound["amount_in"])
    ):
        problems.append(
            f"the signer's SOL moved {signer_sol.moved!r}; at least the input should have left"
        )
    units = meta.get("computeUnitsConsumed")
    return _finish(
        blank(
            landed=True,
            signature=landed_signature,
            simulated_units=receipt.units_consumed,
            units_consumed=units if isinstance(units, int) else None,
            unit_limit=unit_limit,
            fee_lamports=fee,
            token_out=token_out,
            token_in=token_in,
            signer_sol=signer_sol,
            received_amount=received,
            received_witness=witness,
            refusals=tuple(refusals),
            discrepancies=tuple(problems),
            **facts,
        ),
        proof,
        touched,
        call,
    )


def _received(
    output_mint: str,
    *,
    token_out: TokenDelta,
    signer_sol: LamportDelta,
    fee: int | None,
) -> tuple[int | None, ReceivedWitness]:
    """What arrived, read from the account that actually holds it afterwards.

    A token output sits in the user's ATA: an account that did not exist before started
    at zero, so its whole balance after is what it received. A SOL output does not stay
    anywhere readable as a token: the postlude closes the wSOL account (its rent comes
    back with it), so the signer's lamports rose by the output minus the fee. Without
    the fee the native witness cannot be computed, and None is the honest answer.
    """
    if output_mint == NATIVE_SOL_MINT:
        if signer_sol.moved is None or fee is None:
            return None, "native-sol"
        return signer_sol.moved + fee, "native-sol"
    if token_out.before is None:
        return token_out.after, "token-account"
    return token_out.moved, "token-account"


def _finish(
    result: SwapRehearsal, proof: SurfnetProof, touched: Sequence[str], call: RpcCall
) -> SwapRehearsal:
    """RESET every touched account, and report each as a line."""
    from dataclasses import replace

    lines = []
    for address in dict.fromkeys(touched):
        try:
            outcome = reset_account(proof, address, rpc_call=call)
            lines.append(
                f"{address}: {outcome.lamports_before} -> {outcome.lamports_after} lamports"
            )
        except Exception as exc:  # noqa: BLE001 - the run is over; record, never raise
            lines.append(f"{address}: reset FAILED ({type(exc).__name__})")
    return replace(result, reset=tuple(lines))

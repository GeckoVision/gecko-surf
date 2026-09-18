"""The DLMM swap rehearsal, offline: the orchestrator's bundle, landed on a fake fork
that remembers what the cheatcodes wrote and moves the output token when it lands."""

from __future__ import annotations

import base64
from typing import Any

from gecko.sandbox import ephemeral_signer, prove_surfnet
from gecko.sandbox.rehearse_swap import rehearse_dlmm_swap
from tests.test_meteora_swap_landing import (
    CURRENT_POOL,
    PASS_VALUE,
    TOKEN,
    TOKEN_PROGRAM,
    WSOL,
    _bindings,
    _canned_swap,
    _lb_pair_blob,
)
from tests.test_sandbox_rehearse import MEASURED_INFO_RESULT, token_account_bytes

FORK = "http://127.0.0.1:8899"


class SwapFork:
    """Answers like a surfnet for exactly the calls the swap rehearsal makes."""

    def __init__(
        self,
        *,
        received: int = 990,
        output_native: bool = False,
        liquidity_indexes: tuple[int, ...] = (-1, 0, 1),
    ) -> None:
        self.received = received
        self.output_native = output_native
        self.lamports: dict[str, int] = {}
        self.tokens: dict[str, tuple[str, str, int]] = {}
        self.sent: list[str] = []
        self.calls: list[str] = []
        self.blob = _lb_pair_blob(liquidity_indexes)

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.calls.append(method)
        if method == "surfnet_getSurfnetInfo":
            return {"result": MEASURED_INFO_RESULT}
        if method == "surfnet_setAccount":
            self.lamports[params[0]] = int(params[1]["lamports"])
            return {"result": {"value": None}}
        if method == "surfnet_setTokenAccount":
            from gecko.sandbox.cheatcodes import derive_ata

            ata = derive_ata(params[0], params[1], token_program=params[3])
            self.tokens[ata] = (params[1], params[0], int(params[2]["amount"]))
            return {"result": {"value": None}}
        if method == "surfnet_resetAccount":
            self.lamports.pop(params[0], None)
            self.tokens.pop(params[0], None)
            return {"result": {"value": None}}
        if method == "getBalance":
            return {"result": {"value": self.lamports.get(params[0])}}
        if method == "getLatestBlockhash":
            return {
                "result": {
                    "value": {
                        "blockhash": "6xCk4Xgb64QofLjfh5Q5sy47W5dURHagdcDWWhhoAqgo",
                        "lastValidBlockHeight": 526,
                    }
                }
            }
        if method == "getAccountInfo":
            addr = params[0]
            if addr == CURRENT_POOL:
                return {
                    "result": {"value": {"owner": "x", "data": [self.blob, "base64"]}}
                }
            if addr in (TOKEN, WSOL):
                return {"result": {"value": {"owner": TOKEN_PROGRAM}}}
            if addr in self.tokens:
                mint, owner, amount = self.tokens[addr]
                return {
                    "result": {
                        "value": {
                            "data": [
                                token_account_bytes(mint, owner, amount),
                                "base64",
                            ],
                            "lamports": 2_039_280,
                        }
                    }
                }
            if addr in self.lamports:
                return {
                    "result": {
                        "value": {
                            "data": ["", "base64"],
                            "lamports": self.lamports[addr],
                        }
                    }
                }
            return {"result": {"value": None}}
        if method == "simulateTransaction":
            return {"result": {"context": {"slot": 1}, "value": PASS_VALUE}}
        if method == "sendTransaction":
            from solders.transaction import Transaction

            raw = base64.b64decode(params[0])
            tx = Transaction.from_bytes(raw)
            assert all(tx.verify_with_results())
            self.sent.append(params[0])
            payer = str(tx.message.account_keys[0])
            from gecko.sandbox.cheatcodes import derive_ata

            if self.output_native:
                # TOKEN in, SOL out: the input leaves the token account, the output
                # arrives as lamports because the postlude closed the wSOL account.
                # The wSOL ATA is never in self.tokens, so it reads as absent after.
                in_ata = derive_ata(payer, TOKEN, token_program=TOKEN_PROGRAM)
                mint, owner, amount = self.tokens[in_ata]
                self.tokens[in_ata] = (mint, owner, amount - 10_000_000)
                self.lamports[payer] = (
                    self.lamports.get(payer, 0) - 5_000 + self.received
                )
                return {"result": "5" + "m" * 86}
            # the swap lands: the fee and the input leave the signer, the output arrives
            self.lamports[payer] = self.lamports.get(payer, 0) - 5_000 - 10_000_000
            out_ata = derive_ata(payer, TOKEN, token_program=TOKEN_PROGRAM)
            self.tokens[out_ata] = (TOKEN, payer, self.received)
            return {"result": "5" + "m" * 86}
        if method == "getSignatureStatuses":
            return {
                "result": {
                    "value": [
                        {"confirmationStatus": "confirmed", "err": None, "slot": 2}
                    ]
                }
            }
        if method == "getTransaction":
            return {
                "result": {
                    "slot": 2,
                    "meta": {
                        "err": None,
                        "computeUnitsConsumed": 118_000,
                        "fee": 5_000,
                    },
                }
            }
        raise AssertionError(f"unexpected method {method}")


def _fetch(accounts: Any, args: Any, fee_payer: Any) -> Any:
    return _canned_swap(dict(accounts))


def test_the_swap_lands_and_the_output_account_receives_at_least_the_floor() -> None:
    fork = SwapFork(received=10**9)
    proof = prove_surfnet(FORK, rpc_call=fork)
    signer = ephemeral_signer(proof)
    result = rehearse_dlmm_swap(
        proof,
        signer=signer,
        bindings={**_bindings(), "amount_in": 10_000_000},
        rpc_call=fork,
        fetch_swap_instruction=_fetch,
    )
    assert result.landed, result.refusals
    assert result.pool == CURRENT_POOL
    assert result.bin_array_indexes == (-1, 0, 1)
    assert result.received == 10**9 and result.received >= result.min_amount_out
    assert result.units_consumed == 118_000 and result.unit_limit == 144_000
    assert result.signer_sol is not None and result.signer_sol.moved is not None
    assert result.signer_sol.moved <= -10_000_000, "the SOL input left the signer"
    assert result.discrepancies == ()
    assert len(fork.sent) == 1
    # reset ran: nothing the run funded is left behind
    assert signer.pubkey not in fork.lamports


def test_an_output_below_the_floor_is_a_discrepancy_not_a_pass() -> None:
    fork = SwapFork(received=1)
    proof = prove_surfnet(FORK, rpc_call=fork)
    result = rehearse_dlmm_swap(
        proof,
        signer=ephemeral_signer(proof),
        bindings={**_bindings(), "amount_in": 10_000_000},
        rpc_call=fork,
        fetch_swap_instruction=_fetch,
    )
    assert result.landed
    assert any("below the state-quoted floor" in line for line in result.discrepancies)


def test_a_sol_output_is_witnessed_by_the_signer_balance_not_a_closed_account() -> None:
    """MEASURED on the fork (2026-09-18, pool DNkZFw1V…, 1 USDC -> SOL): the postlude
    closes the wSOL account, the token read says None, and the signer is up by the
    output minus the fee. Judging a SOL output by its token account is a false failure."""
    # selling token_x walks DOWN from the active array (-2): liquidity must sit there
    fork = SwapFork(received=4_278_542, output_native=True, liquidity_indexes=(-3, -2))
    proof = prove_surfnet(FORK, rpc_call=fork)
    result = rehearse_dlmm_swap(
        proof,
        signer=ephemeral_signer(proof),
        bindings={
            **_bindings(),
            "input_mint": TOKEN,
            "output_mint": WSOL,
            "amount_in": 10_000_000,
        },
        rpc_call=fork,
        fetch_swap_instruction=_fetch,
    )
    assert result.landed, result.refusals
    assert result.bin_array_indexes == (-2, -3), "nearest first, in the swap direction"
    assert result.token_out is not None and result.token_out.after is None
    assert result.received_witness == "native-sol"
    assert result.received == 4_278_542, "signer gain 4,273,542 + fee 5,000"
    assert result.token_in is not None and result.token_in.moved == -10_000_000
    assert result.discrepancies == ()

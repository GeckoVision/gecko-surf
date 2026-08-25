"""The general fork rehearsal, falsified offline.

PATTERN B. The RPC, the IDL and the builder are all injected, so every branch below runs
with no validator and no network. The live proof — jurassic_fi `contribute` landing on a
surfpool mainnet fork at 25,937 CU, moving 250,000 raw USDC from a throwaway signer into
the launch's vault — is the final check, never the debugger.

What these tests pin is the thing that separates a rehearsal from a demo: it REPORTS what
moved and refuses to invent a verdict, and it will not sign for a chain other than the one
that proved itself.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from gecko.sandbox import rehearse as rehearse_module
from gecko.sandbox.rehearse import RehearsalError
from gecko.sandbox.rehearse_instruction import rehearse_instruction
from gecko.sandbox.surfnet import ephemeral_signer, prove_surfnet

FORK = "http://127.0.0.1:8899"
PROGRAM = "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
VAULT = "Apz65kudrgZ2DxnPczbpFJnt6zS1XWuTQAXsqj38bYaq"

IDL: dict[str, Any] = {
    "address": PROGRAM,
    "metadata": {"spec": "0.1.0"},
    "instructions": [
        {
            "name": "contribute",
            "args": [{"name": "amount", "type": "u64"}],
            "accounts": [
                {"name": "contributor", "signer": True, "writable": True},
                {"name": "payment_vault", "writable": True},
            ],
        }
    ],
    "accounts": [],
    "types": [],
}


def token_account(mint: str, owner: str, amount: int) -> dict[str, Any]:
    """A raw SPL token account: mint(32) | owner(32) | amount(u64 LE)."""
    from solders.pubkey import Pubkey

    raw = (
        bytes(Pubkey.from_string(mint))
        + bytes(Pubkey.from_string(owner))
        + amount.to_bytes(8, "little")
        + bytes(93)
    )
    return {"data": [base64.b64encode(raw).decode(), "base64"], "lamports": 2_039_280}


class FakeChain:
    """A surfnet that REMEMBERS what the cheatcodes wrote.

    Remembering matters: `fund_sol` and `fund_token` both read the account back and refuse
    if the write did not take. A fake that answered a canned balance would let those
    guards pass without exercising them, which is the same as not having them.

    ``balances`` maps a token account to (before, after); the reads flip once the
    transaction is sent, which is how a delta exists at all.
    """

    def __init__(
        self,
        *,
        balances: dict[str, tuple[int, int]] | None = None,
        send_error: str | None = None,
        confirmed: bool = True,
        owner: str = "",
    ) -> None:
        self.balances = balances or {}
        self.send_error = send_error
        self.confirmed = confirmed
        self.owner = owner
        self.sent = False
        self.calls: list[str] = []
        self.lamports: dict[str, int] = {}
        self.tokens: dict[str, tuple[str, str, int]] = {}

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.calls.append(method)
        if method == "surfnet_getSurfnetInfo":
            # The two facts prove_surfnet demands: a live integer context.slot, and
            # value.runbookExecutions, a concept that exists only in surfpool.
            return {
                "result": {
                    "context": {"slot": 12345},
                    "value": {"version": "1.1.1", "runbookExecutions": []},
                }
            }
        if method == "surfnet_setAccount":
            self.lamports[params[0]] = int(params[1]["lamports"])
            return {"result": {"value": None}}
        if method == "surfnet_setTokenAccount":
            owner, mint, update = params[0], params[1], params[2]
            from gecko.sandbox.cheatcodes import derive_ata

            # params[3] is the token program the cheatcode funded under — deriving with
            # anything else would key this fake on an account the real code never touches.
            self.tokens[derive_ata(owner, mint, token_program=params[3])] = (
                mint,
                owner,
                int(update["amount"]),
            )
            return {"result": {"value": None}}
        if method == "getBalance":
            # fund_sol verifies its own write with getBalance, not getAccountInfo.
            return {"result": {"value": self.lamports.get(params[0])}}
        if method == "getLatestBlockhash":
            return {
                "result": {"value": {"blockhash": "11111111111111111111111111111111"}}
            }
        if method == "getAccountInfo":
            address = params[0]
            if address in self.balances:
                before, after = self.balances[address]
                return {
                    "result": {
                        "value": token_account(
                            MINT, self.owner, after if self.sent else before
                        )
                    }
                }
            if address in self.tokens:
                mint, owner, amount = self.tokens[address]
                return {"result": {"value": token_account(mint, owner, amount)}}
            if address in self.lamports:
                return {
                    "result": {
                        "value": {
                            "data": ["", "base64"],
                            "lamports": self.lamports[address],
                        }
                    }
                }
            return {"result": {"value": None}}
        if method == "sendTransaction":
            if self.send_error:
                return {"error": {"code": -32002, "message": self.send_error}}
            self.sent = True
            return {"result": "sig11111"}
        if method == "getSignatureStatuses":
            status = (
                {"err": None, "confirmationStatus": "confirmed"}
                if self.confirmed
                else None
            )
            return {"result": {"value": [status]}}
        if method == "getTransaction":
            return {
                "result": {
                    "meta": {
                        "err": None,
                        "computeUnitsConsumed": 25937,
                        "logMessages": ["Program log: Instruction: Contribute"],
                    }
                }
            }
        return {"result": {"value": None}}


def idl_fetch(_program_id: str) -> dict[str, Any]:
    return IDL


def make_builder(payer_holder: dict[str, str]) -> Any:
    """A builder that returns a real, parseable transaction paying from the signer.

    Real bytes rather than a placeholder because `_sign` decodes them and checks the fee
    payer against the signing key — a fake string would skip the one check that matters.
    """

    def build(**kwargs: Any) -> str:
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import Message
        from solders.pubkey import Pubkey
        from solders.transaction import Transaction

        payer = Pubkey.from_string(kwargs["payer"])
        payer_holder["accounts"] = kwargs["accounts"]
        instruction = Instruction(
            Pubkey.from_string(PROGRAM),
            b"\x00" * 8,
            [AccountMeta(payer, True, True)],
        )
        message = Message.new_with_blockhash([instruction], payer, Hash.default())
        return base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()

    return build


def signer_on(chain: FakeChain) -> Any:
    return ephemeral_signer(prove_surfnet(FORK, rpc_call=chain))


# ------------------------------------------------------------------- the binding


def test_a_signer_bound_to_another_chain_is_refused_before_anything_happens() -> None:
    """The key that signs and the chain it signs for cannot drift apart. This is raised,
    not recorded, because continuing past it would mean signing against something other
    than the fork that proved itself."""
    chain = FakeChain()
    proof = prove_surfnet(FORK, rpc_call=chain)
    other = ephemeral_signer(prove_surfnet("http://127.0.0.1:9999", rpc_call=chain))

    with pytest.raises(RehearsalError, match="must not be used on another"):
        rehearse_instruction(
            proof,
            signer=other,
            program_id=PROGRAM,
            instruction="contribute",
            values={"amount": 1},
            idl_fetch=idl_fetch,
            build_call=make_builder({}),
            rpc_call=chain,
        )


# ------------------------------------------------------------------ what moved


def test_it_reports_the_balances_that_changed() -> None:
    chain = FakeChain()
    signer = signer_on(chain)
    buyer_ata = "HiTsN8bdMz1TmvcvSjcvyoK1qRk4FMgYFPT4pxHkSY9V"
    chain.owner = signer.pubkey
    chain.balances = {buyer_ata: (1_000_000, 750_000), VAULT: (0, 250_000)}
    held: dict[str, str] = {}

    result = rehearse_instruction(
        prove_surfnet(FORK, rpc_call=chain),
        signer=signer,
        program_id=PROGRAM,
        instruction="contribute",
        values={"amount": 250_000, "payment_vault": VAULT},
        idl_fetch=idl_fetch,
        build_call=make_builder(held),
        fund_tokens=[(MINT, 1_000_000)],
        rpc_call=chain,
    )

    assert result.landed is True
    assert result.compute_units == 25937
    assert result.refusals == ()
    moved = {d.account: d.moved for d in result.token_deltas}
    assert moved[VAULT] == 250_000
    assert result.moved_anything is True


def test_a_call_that_lands_and_moves_nothing_is_not_a_failure() -> None:
    """`moved_anything` is deliberately not called `succeeded`. A call can land, cost
    compute and move no balance — marking an order delivered is exactly that — and
    reporting those as failure teaches a developer the wrong thing about their program."""
    chain = FakeChain()
    signer = signer_on(chain)
    chain.owner = signer.pubkey
    chain.balances = {VAULT: (500, 500)}

    result = rehearse_instruction(
        prove_surfnet(FORK, rpc_call=chain),
        signer=signer,
        program_id=PROGRAM,
        instruction="contribute",
        values={"amount": 0, "payment_vault": VAULT},
        idl_fetch=idl_fetch,
        build_call=make_builder({}),
        rpc_call=chain,
    )

    assert result.landed is True
    assert result.moved_anything is False
    assert result.refusals == ()


# -------------------------------------------------------------------- refusals


def test_a_refused_preparation_never_reaches_the_chain() -> None:
    chain = FakeChain()
    signer = signer_on(chain)

    result = rehearse_instruction(
        prove_surfnet(FORK, rpc_call=chain),
        signer=signer,
        program_id=PROGRAM,
        instruction="contribute",
        values={},  # `amount` is declared and not supplied
        idl_fetch=idl_fetch,
        build_call=make_builder({}),
        rpc_call=chain,
    )

    assert result.landed is False
    assert [r.step for r in result.refusals] == ["prepare"]
    assert "argument-missing" in result.refusals[0].reason
    assert "sendTransaction" not in chain.calls


def test_a_rejected_send_is_recorded_rather_than_raised() -> None:
    chain = FakeChain(send_error="Blockhash not found")
    signer = signer_on(chain)

    result = rehearse_instruction(
        prove_surfnet(FORK, rpc_call=chain),
        signer=signer,
        program_id=PROGRAM,
        instruction="contribute",
        values={"amount": 1, "payment_vault": VAULT},
        idl_fetch=idl_fetch,
        build_call=make_builder({}),
        rpc_call=chain,
    )

    assert result.landed is False
    assert [r.step for r in result.refusals] == ["send"]
    assert "Blockhash not found" in result.refusals[0].reason


def test_an_unconfirmed_send_never_claims_to_have_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `_confirm` reads this global at call time, so shortening it here keeps the guard
    # real while costing the suite a fraction of a second instead of half a minute.
    monkeypatch.setattr(rehearse_module, "CONFIRM_TIMEOUT_SECONDS", 0.2)
    chain = FakeChain(confirmed=False)
    signer = signer_on(chain)

    result = rehearse_instruction(
        prove_surfnet(FORK, rpc_call=chain),
        signer=signer,
        program_id=PROGRAM,
        instruction="contribute",
        values={"amount": 1, "payment_vault": VAULT},
        idl_fetch=idl_fetch,
        build_call=make_builder({}),
        rpc_call=chain,
    )

    assert result.landed is False
    assert any(r.step == "confirm" for r in result.refusals)

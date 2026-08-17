"""The rehearsal loop — the guards offline, the whole thing against a real fork.

Two kinds of test, and the split is the same one the rest of this package uses:

* ``fork``-marked legs start a local surfpool mainnet fork, generate a key, SIGN, LAND a
  real transaction and then judge it by the ledger. They are the only tests in this repo
  that produce a signature, and they can only run because the endpoint proved it is not
  mainnet. Deselect with ``-m "not fork"``.
* offline legs use a light fake fork — a dict of accounts behind the injected transport —
  to prove the three bindings hold, that RESET runs even when a step refuses, that a wrong
  delta is reported as a discrepancy rather than smoothed into a pass, and that no other
  buyer's receipt row can leave the walk.

Nothing here reaches mainnet. The offline legs never touch a validator, and the fork legs
send to ``127.0.0.1``.
"""

from __future__ import annotations

import dataclasses
import base64
import os
from typing import Any

import pytest

from gecko.pda_testkit import SurfpoolError, SurfpoolFork, surfpool_status
from gecko.sandbox import SurfnetProof, ephemeral_signer, prove_surfnet
from gecko.sandbox.rehearse import (
    LamportDelta,
    Rehearsal,
    RehearsalError,
    TokenDelta,
    WrittenReceipt,
    _judge,
    _Ledger,
    _only_this_surfnet,
    _sign,
    _walk_receipts,
    _written_receipt,
    rehearse_purchase,
)
from gecko.store_directory import LET_ME_BUY_PROGRAM_ID

FORK_PORT = 8937
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

#: The measured surfnet-info body, so an offline proof can be built without a validator.
MEASURED_INFO_RESULT: dict[str, Any] = {
    "context": {"slot": 439792934, "apiVersion": "3.1.6"},
    "value": {"runbookExecutions": []},
}

SKIP_REASON = (
    "surfpool is not on PATH (or GECKO_FORK_DEMO=0) — THE FORK LEG DID NOT RUN and "
    "NOTHING below was measured against a validator: " + surfpool_status().detail
)
needs_fork = pytest.mark.skipif(
    not surfpool_status().available or os.getenv("GECKO_FORK_DEMO") == "0",
    reason=SKIP_REASON,
)

#: The storefront the fork legs buy from. The same local file every other scorecard reads
#: — no store name, wallet or channel is committed here, and its absence SKIPS rather than
#: substituting a store nobody chose.
CONFIG_PATH = os.path.expanduser(
    os.environ.get("GECKO_CI_CONFIG", "~/.gecko/catalog-ci.json")
)


def local_store() -> dict[str, str]:
    import json

    if not os.path.exists(CONFIG_PATH):
        pytest.skip(f"no {CONFIG_PATH} — the fork leg needs a storefront to buy from")
    stored = json.loads(open(CONFIG_PATH).read())
    if not stored.get("store"):
        pytest.skip(f"{CONFIG_PATH} names no store")
    return dict(stored)


def offline_proof(rpc_url: str = "http://127.0.0.1:8899") -> SurfnetProof:
    """A proof minted the ONLY way one can be: through prove_surfnet.

    This used to construct `SurfnetProof(...)` directly, and an adversarial pass showed
    why that was load-bearing in the wrong direction — a proof you can build is a proof
    an attacker can re-point at mainnet, and the suite doing it routinely is what made
    the shortcut look normal. So the transport is faked and the minting is real: the
    attestation comes from prove_surfnet, no validator runs, no network is touched, and
    a test cannot demonstrate a path that production does not have.
    """
    return prove_surfnet(
        rpc_url,
        rpc_call=lambda url, method, params: {"result": MEASURED_INFO_RESULT},
    )


def fresh_pubkey() -> str:
    from solders.keypair import Keypair

    return str(Keypair().pubkey())


# ---------------------------------------------------------------------------
# a light fake fork: accounts in a dict, the same JSON-RPC shapes the real one answers
# ---------------------------------------------------------------------------


def _borsh_string(text: str) -> bytes:
    raw = text.encode()
    return len(raw).to_bytes(4, "little") + raw


def store_account_bytes(
    store_name: str, authority: str, product: str, price: int
) -> str:
    """A `Receipts` account with NO receipt rows, in the program's own layout.

    Synthesised rather than captured: a real store's account carries real customers'
    pubkeys in its receipts vec, and a fixture is a file. Zero rows is also the honest
    starting point for the receipt assertions below, which are about the row the PROGRAM
    would write.
    """
    from solders.pubkey import Pubkey

    raw = b"\x00" * 8  # anchor discriminator — decode_store skips it
    raw += (0).to_bytes(4, "little")  # receipts vec: empty
    raw += (0).to_bytes(8, "little")  # total_purchases
    raw += _borsh_string(store_name)
    raw += bytes(Pubkey.from_string(authority))
    raw += (1).to_bytes(4, "little")  # one product
    raw += price.to_bytes(8, "little")
    raw += (6).to_bytes(1, "little")  # decimals
    raw += bytes(Pubkey.from_string(USDC))
    raw += _borsh_string(product)
    raw += _borsh_string("")  # telegram channel
    return base64.b64encode(raw).decode()


def token_account_bytes(mint: str, owner: str, amount: int) -> str:
    from solders.pubkey import Pubkey

    raw = bytes(Pubkey.from_string(mint)) + bytes(Pubkey.from_string(owner))
    raw += amount.to_bytes(8, "little")
    return base64.b64encode(raw + b"\x00" * (165 - len(raw))).decode()


class FakeFork:
    """A dict of accounts that answers like a surfnet. Records every request it is sent.

    Deliberately NOT a mock framework: the cheatcode wrappers read their own writes back,
    so a fake that only records calls would never exercise the verification those wrappers
    exist for. This one actually stores what it is told.
    """

    def __init__(self, rpc_url: str) -> None:
        self.rpc_url = rpc_url
        self.seen: list[tuple[str, str]] = []
        self.accounts: dict[str, dict[str, Any]] = {}

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.seen.append((url, method))
        handler = getattr(self, f"_{method}", None)
        if handler is None:
            return {"result": None}
        return {"result": handler(params)}

    # reads
    def _getAccountInfo(self, params: list[Any]) -> dict[str, Any]:  # noqa: N802
        return {"context": {"slot": 1}, "value": self.accounts.get(params[0])}

    def _getBalance(self, params: list[Any]) -> dict[str, Any]:  # noqa: N802
        account = self.accounts.get(params[0]) or {}
        return {"context": {"slot": 1}, "value": account.get("lamports", 0)}

    def _getLatestBlockhash(self, _params: list[Any]) -> dict[str, Any]:  # noqa: N802
        return {
            "context": {"slot": 1},
            "value": {
                "blockhash": "SURFNETxSAFEHASHxxxxxxxxxxxxxxxxxxxxxxxx177",
                "lastValidBlockHeight": 526,
            },
        }

    def _getBlockHeight(self, _params: list[Any]) -> int:  # noqa: N802
        return 375

    # cheatcodes
    def _surfnet_setAccount(self, params: list[Any]) -> None:  # noqa: N802
        account = self.accounts.setdefault(
            params[0],
            {"lamports": 0, "data": ["", "base64"], "owner": "1" * 32, "space": 0},
        )
        account.update(params[1])

    def _surfnet_setTokenAccount(self, params: list[Any]) -> None:  # noqa: N802
        from gecko.store_accounts import derive_ata

        owner, mint, update = params[0], params[1], params[2]
        program = params[3] if len(params) > 3 else None
        ata = (
            derive_ata(owner, mint, token_program=program)
            if program
            else derive_ata(owner, mint)
        )
        self.accounts[ata] = {
            "lamports": 2_039_280,
            "data": [token_account_bytes(mint, owner, update["amount"]), "base64"],
            "owner": program or "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "space": 165,
        }

    def _surfnet_resetAccount(self, params: list[Any]) -> None:  # noqa: N802
        self.accounts.pop(params[0], None)


def wired_fake(store_name: str, authority: str, product: str, price: int) -> FakeFork:
    """A fake fork holding exactly one storefront, at the address its NAME derives."""
    from gecko.store_accounts import receipts_pda

    fork = FakeFork("http://127.0.0.1:9999")
    fork.accounts[receipts_pda(store_name)] = {
        "lamports": 26_510_640,
        "data": [store_account_bytes(store_name, authority, product, price), "base64"],
        "owner": LET_ME_BUY_PROGRAM_ID,
        "space": 3681,
    }
    return fork


# ---------------------------------------------------------------------------
# offline: the three bindings
# ---------------------------------------------------------------------------


def test_a_signer_proven_against_another_fork_is_refused_before_anything_happens() -> (
    None
):
    """A key is bound to the ONE endpoint that proved itself.

    The refusal has to land before the first RPC call, not merely before the send: a
    rehearsal that funded, prepared and then noticed has already written state onto a fork
    nobody proved.
    """
    elsewhere = ephemeral_signer(offline_proof("http://127.0.0.1:7777"))
    fork = wired_fake("teststore", fresh_pubkey(), "Water", 100_000)

    with pytest.raises(RehearsalError) as refused:
        rehearse_purchase(
            offline_proof(fork.rpc_url),
            buyer=elsewhere,
            store="teststore",
            product="Water",
            rpc_call=fork,
        )

    assert "7777" in str(refused.value)
    assert fork.seen == [], "the fork was contacted despite the binding not holding"


def test_the_url_guard_admits_the_proven_surfnet_and_nothing_else() -> None:
    """Strictly narrower than the SSRF guard it stands in for: exactly one string."""
    from gecko.netguard import UnsafeUrlError

    guard = _only_this_surfnet(offline_proof("http://127.0.0.1:8899"))
    assert guard("http://127.0.0.1:8899") is None
    for other in (
        "https://api.mainnet-beta.solana.com",
        "http://127.0.0.1:8900",
        "http://localhost:8899",
    ):
        with pytest.raises(UnsafeUrlError):
            guard(other)


def test_a_transaction_that_pays_from_another_account_is_never_signed() -> None:
    """The fee payer is checked BEFORE the signature exists, not after the node rejects it."""
    from solders.hash import Hash
    from solders.instruction import Instruction
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    stranger = Keypair()
    instruction = Instruction(
        Pubkey.from_string("11111111111111111111111111111111"), b"\x00", []
    )
    message = Message.new_with_blockhash(
        [instruction], stranger.pubkey(), Hash.default()
    )
    unsigned = base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()

    signer = ephemeral_signer(offline_proof())
    with pytest.raises(RehearsalError) as refused:
        _sign(unsigned, signer)
    assert str(stranger.pubkey()) in str(refused.value)
    assert signer.pubkey in str(refused.value)


def test_a_prepared_transaction_aimed_elsewhere_is_refused_before_the_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The submit URL binding — the mechanism that keeps a fork signature on the fork.

    This is the mutation worth catching: move the ``submit.rpc_url`` check to AFTER
    ``_sign`` and a signature over mainnet-bound bytes exists, if only for a moment. The
    spy on ``_sign`` is what makes the ordering observable rather than assumed.
    """
    import gecko.sandbox.rehearse as module

    signed_calls: list[str] = []
    monkeypatch.setattr(
        module,
        "_sign",
        lambda tx, buyer: (signed_calls.append(tx), ("", ""))[1],
    )
    monkeypatch.setattr(
        module,
        "prepare_purchase_result",
        lambda *_a, **_k: {
            "refused": False,
            "transaction": {"unsigned_transaction": "AAAA"},
            # the whole point: an approved plan that names a DIFFERENT node
            "submit": {"rpc_url": "https://api.mainnet-beta.solana.com"},
        },
    )
    fork = wired_fake("teststore", fresh_pubkey(), "Water", 100_000)
    signer = ephemeral_signer(offline_proof(fork.rpc_url))

    with pytest.raises(RehearsalError) as refused:
        rehearse_purchase(
            offline_proof(fork.rpc_url),
            buyer=signer,
            store="teststore",
            product="Water",
            rpc_call=fork,
        )

    assert "mainnet" in str(refused.value)
    assert signed_calls == [], "the bytes were signed before the endpoint was checked"
    assert "sendTransaction" not in {method for _url, method in fork.seen}


# ---------------------------------------------------------------------------
# offline: the loop's own promises
# ---------------------------------------------------------------------------


def test_the_rehearsal_reaches_only_the_proven_endpoint() -> None:
    """Every request, cheatcode and read alike, goes to the proof's URL and nowhere else."""
    fork = wired_fake("teststore", fresh_pubkey(), "Water", 100_000)
    proof = offline_proof(fork.rpc_url)
    signer = ephemeral_signer(proof)

    from gecko.simulate import BuiltTx

    result = rehearse_purchase(
        proof,
        buyer=signer,
        store="teststore",
        product="Water",
        rpc_call=fork,
        # the builder returns nothing, so the production path refuses at step 2 — which
        # is exactly the run this test wants: funded, refused, and cleaned up.
        build_call=lambda _plan: BuiltTx(tx="", encoding="base64"),
    )

    assert fork.seen, "no request was made at all"
    assert {url for url, _method in fork.seen} == {fork.rpc_url}
    assert result.landed is False
    assert result.balanced is None, "an unlanded run must not render as a failed ledger"
    assert [r.step for r in result.refusals] == ["prepare"]
    assert "build-returned-nothing" in result.refusals[0].reason


def test_reset_runs_even_when_a_step_refuses() -> None:
    """A refusal at step 2 has already funded. Leaving that behind dirties the next run."""
    from gecko.simulate import BuiltTx

    fork = wired_fake("teststore", fresh_pubkey(), "Water", 100_000)
    proof = offline_proof(fork.rpc_url)
    signer = ephemeral_signer(proof)

    result = rehearse_purchase(
        proof,
        buyer=signer,
        store="teststore",
        product="Water",
        rpc_call=fork,
        build_call=lambda _plan: BuiltTx(tx="", encoding="base64"),
    )

    assert len(result.reset) == 4, result.reset
    resets = [method for _url, method in fork.seen if method == "surfnet_resetAccount"]
    assert len(resets) == 4
    # and the fake fork agrees: the funded accounts are gone again
    assert signer.pubkey not in fork.accounts


def test_the_price_is_read_from_the_store_and_is_not_a_parameter() -> None:
    """Nothing supplies a price. A price this function was handed would be checked against
    itself, which is not a check."""
    import inspect

    assert "price" not in inspect.signature(rehearse_purchase).parameters
    fork = wired_fake("teststore", fresh_pubkey(), "Water", 123_456)
    proof = offline_proof(fork.rpc_url)
    from gecko.simulate import BuiltTx

    result = rehearse_purchase(
        proof,
        buyer=ephemeral_signer(proof),
        store="teststore",
        product="Water",
        rpc_call=fork,
        build_call=lambda _plan: BuiltTx(tx="", encoding="base64"),
    )
    assert result.price_raw == 123_456


# ---------------------------------------------------------------------------
# offline: the judgement itself
# ---------------------------------------------------------------------------


def _ledgers(buyer_before: int, buyer_after: int, store_before: int, store_after: int):
    return (
        _Ledger(
            buyer_lamports=50_000_000,
            buyer_token=buyer_before,
            store_token=store_before,
            total_purchases=127,
            receipts_raw=None,
        ),
        _Ledger(
            buyer_lamports=49_995_000,
            buyer_token=buyer_after,
            store_token=store_after,
            total_purchases=128,
            receipts_raw=None,
        ),
    )


def _blank(price: int = 100_000) -> Rehearsal:
    return Rehearsal(
        store="teststore",
        product="Water",
        price_raw=price,
        mint=USDC,
        buyer="Buyer",
        landed=False,
    )


def test_a_short_payment_is_a_discrepancy_not_a_pass() -> None:
    """The assertion the task turns on: exactly the price, on both sides.

    A transfer of 90,000 against a listed price of 100,000 is a WRONG purchase that
    simulates, lands and confirms. Only the ledger catches it, and only if the ledger is
    compared against the store's own declared price rather than against the transfer.
    """
    before, after = _ledgers(100_000, 10_000, 21_630_226, 21_720_226)
    judged = _judge(
        _blank(),
        signature="sig",
        before=before,
        after=after,
        meta={"computeUnitsConsumed": 42_494, "fee": 5_000},
        simulated_units=42_494,
        window_blocks=151,
        buyer_ata="ata",
        store_ata="store",
        buyer="Buyer",
        authority="Authority",
        mint=USDC,
    )
    assert judged.landed is True
    assert judged.balanced is False
    assert any("-100000" in problem for problem in judged.discrepancies)
    # the debit and the credit still cancel — that is a DIFFERENT question, and reporting
    # only the balance would call this purchase fine
    assert not any("do not cancel" in problem for problem in judged.discrepancies)


def test_a_credit_that_lands_elsewhere_is_its_own_finding() -> None:
    """The buyer paid the right amount and the store was not credited it."""
    before, after = _ledgers(100_000, 0, 21_630_226, 21_630_226)
    judged = _judge(
        _blank(),
        signature="sig",
        before=before,
        after=after,
        meta=None,
        simulated_units=None,
        window_blocks=None,
        buyer_ata="ata",
        store_ata="store",
        buyer="Buyer",
        authority="Authority",
        mint=USDC,
    )
    assert judged.balanced is False
    assert any("do not cancel" in problem for problem in judged.discrepancies)


def test_a_missing_receipt_row_is_reported_even_when_the_money_moved() -> None:
    """Money moving is not the program recording a sale. Both are checked."""
    before, after = _ledgers(100_000, 0, 21_630_226, 21_730_226)
    judged = _judge(
        _blank(),
        signature="sig",
        before=before,
        after=after,  # receipts_raw is None -> the store account could not be read
        meta=None,
        simulated_units=None,
        window_blocks=None,
        buyer_ata="ata",
        store_ata="store",
        buyer="Buyer",
        authority="Authority",
        mint=USDC,
    )
    assert judged.balanced is False
    assert any("store account" in problem for problem in judged.discrepancies)


# ---------------------------------------------------------------------------
# offline: no other buyer's row leaves the walk
# ---------------------------------------------------------------------------


def receipts_account_bytes(
    rows: list[tuple[int, str, int, int, str]], total: int
) -> bytes:
    """`Receipts` bytes carrying real rows — (id, buyer, price, table, product)."""
    from solders.pubkey import Pubkey

    raw = b"\x00" * 8 + len(rows).to_bytes(4, "little")
    for receipt_id, buyer, price, table, product in rows:
        raw += receipt_id.to_bytes(8, "little")
        raw += bytes(Pubkey.from_string(buyer))
        raw += (0).to_bytes(1, "little")  # was_delivered
        raw += price.to_bytes(8, "little")
        raw += (1_786_947_987).to_bytes(8, "little")  # timestamp
        raw += table.to_bytes(1, "little")
        raw += _borsh_string(product)
    return raw + total.to_bytes(8, "little")


def test_only_the_ephemeral_buyers_row_leaves_the_walk() -> None:
    """Every other row belongs to a real customer, and the fork mirrors them faithfully.

    The walk has to READ them — the vec is sequential and there is no index — so the
    guarantee is that they are compared and dropped. A `WrittenReceipt` has no buyer field
    at all, which is what makes that structural rather than a habit.
    """
    import dataclasses

    other = fresh_pubkey()
    mine = fresh_pubkey()
    raw = receipts_account_bytes(
        [
            (125, other, 100_000, 9, "Water"),
            (126, mine, 100_000, 7, "Water"),
            (127, other, 100_000, 4, "Water"),
        ],
        total=128,
    )
    before = _Ledger(None, None, None, 127, None)
    after = _Ledger(None, None, None, None, raw)

    receipt, refusal = _written_receipt(before, after, mine)
    assert refusal is None
    assert receipt is not None
    assert receipt.receipt_id == 126
    assert receipt.table_number == 7
    assert receipt.total_purchases_after == 128
    assert "buyer" not in {f.name for f in dataclasses.fields(WrittenReceipt)}

    # ...and a buyer with no row is a refusal, not an empty pass
    _, missing = _written_receipt(before, after, fresh_pubkey())
    assert missing is not None and "no row for this buyer" in missing


def test_the_newest_row_is_the_one_this_run_caused() -> None:
    """The vec is a rolling window; a returning buyer appears more than once."""
    mine = fresh_pubkey()
    raw = receipts_account_bytes(
        [(120, mine, 100_000, 1, "Water"), (127, mine, 100_000, 7, "Water")], total=128
    )
    receipt, _ = _written_receipt(
        _Ledger(None, None, None, 127, None), _Ledger(None, None, None, None, raw), mine
    )
    assert receipt is not None and receipt.receipt_id == 127


def test_a_truncated_receipts_vec_refuses_rather_than_guessing() -> None:
    """Hostile bytes, through the same bounds-checked cursor decode_store uses."""
    from gecko.store_directory import StoreDecodeError

    with pytest.raises(StoreDecodeError):
        _walk_receipts(b"\x00" * 8 + (5).to_bytes(4, "little") + b"\x01\x02")


def test_a_delta_with_an_absent_side_is_none_and_never_zero() -> None:
    """An account that did not exist is not an account holding nothing."""
    assert TokenDelta("a", "o", USDC, None, 100).moved is None
    assert LamportDelta("a", 100, None).moved is None
    assert TokenDelta("a", "o", USDC, 100, 100).moved == 0


# ---------------------------------------------------------------------------
# the fork legs — the only signatures this repo produces
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fork() -> Any:
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    try:
        with SurfpoolFork(mainnet, port=FORK_PORT, ready_timeout=180) as running:
            yield running
    except SurfpoolError as exc:
        pytest.skip(f"THE FORK LEG DID NOT RUN — surfpool never became ready: {exc}")


@pytest.mark.fork
@needs_fork
def test_the_whole_loop_lands_and_the_ledger_balances(fork: Any) -> None:
    """FUND, PREPARE, SIGN, LAND, JUDGE, RESET — and the two deltas must be exact.

    This is the claim the module exists to make: not "these bytes would land" but "the
    buyer's token account fell by exactly the price and the store's rose by exactly it".
    """
    config = local_store()
    proof = prove_surfnet(fork.rpc_url)
    signer = ephemeral_signer(proof)
    product = os.environ.get("GECKO_CI_PRODUCT", "Water")

    result = rehearse_purchase(
        proof, buyer=signer, store=config["store"], product=product, table_number=7
    )

    assert not result.refusals, result.refusals
    assert result.landed is True
    assert result.signature
    assert result.buyer_token is not None and result.store_token is not None
    assert result.buyer_token.moved == -result.price_raw
    assert result.store_token.moved == result.price_raw
    assert result.discrepancies == ()
    assert result.balanced is True
    # the program's own record, not the transfer's
    assert result.receipt is not None
    assert result.receipt.price_raw == result.price_raw
    assert result.receipt.table_number == 7
    assert result.receipt.total_purchases_after == (
        (result.receipt.total_purchases_before or 0) + 1
    )
    # the fee came out of SOL, and nothing else did
    assert result.buyer_sol is not None
    assert result.buyer_sol.moved == -(result.fee_lamports or 0)


@pytest.mark.fork
@needs_fork
def test_the_fork_charges_exactly_what_it_simulated(fork: Any) -> None:
    """The fork is SELF-consistent — which is why its disagreement with mainnet is real.

    Measured 2026-08-17: the fork simulated and then charged 42,494 CU for a purchase
    mainnet simulates at 36,399. If this test ever fails, the two halves of that claim
    have come apart and the module docstring's compute paragraph is wrong.
    """
    config = local_store()
    proof = prove_surfnet(fork.rpc_url)
    result = rehearse_purchase(
        proof,
        buyer=ephemeral_signer(proof),
        store=config["store"],
        product=os.environ.get("GECKO_CI_PRODUCT", "Water"),
    )
    assert result.landed is True
    assert result.simulated_units is not None
    assert result.units_consumed == result.simulated_units


@pytest.mark.fork
@needs_fork
def test_a_second_rehearsal_starts_from_the_state_the_first_one_found(
    fork: Any,
) -> None:
    """RESET undoes TRANSACTION writes, not just cheatcode overrides.

    Measured: after a landed purchase the store's receipts vec went back to its forked
    state and ``total_purchases`` fell 128 -> 127. Without that, every rehearsal after the
    first would be judging a store the previous run moved.
    """
    config = local_store()
    proof = prove_surfnet(fork.rpc_url)
    product = os.environ.get("GECKO_CI_PRODUCT", "Water")

    first = rehearse_purchase(
        proof, buyer=ephemeral_signer(proof), store=config["store"], product=product
    )
    second = rehearse_purchase(
        proof, buyer=ephemeral_signer(proof), store=config["store"], product=product
    )

    assert first.landed and second.landed
    assert first.store_token is not None and second.store_token is not None
    assert first.store_token.before == second.store_token.before
    assert first.receipt is not None and second.receipt is not None
    assert first.receipt.receipt_id == second.receipt.receipt_id
    assert first.receipt.total_purchases_before == second.receipt.total_purchases_before


@pytest.mark.fork
@needs_fork
def test_the_key_is_created_only_after_the_fork_proves_itself(fork: Any) -> None:
    """The whole guard, end to end: mainnet cannot produce a proof, so it gets no key.

    Deliberately points at the REAL mainnet endpoint. It is a read of one JSON-RPC method
    that mainnet does not implement; no key exists on this path, so there is nothing that
    could be signed with even if it did.
    """
    from gecko.sandbox import NotASurfnetError

    assert prove_surfnet(fork.rpc_url).slot > 0
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    with pytest.raises(NotASurfnetError):
        prove_surfnet(mainnet)


def test_a_receipt_recording_a_different_price_than_moved_is_a_discrepancy() -> None:
    """The check nobody was asserting — deleting it left all 97 offline tests green.

    An adversarial pass replaced this branch with `elif False:` and nothing went red: the
    only test touching it asserted the two were EQUAL on the happy path, which passes with
    the comparison gone. So a program that transfers the listed price and then writes a
    receipt recording something else went unreported — and that receipt is what a
    fulfiller reads. The ledger said the right amount moved; the store's own record
    disagreed; the rehearsal called it clean.

    Here the transfer is exactly right (100,000 out, 100,000 in) and only the recorded
    price is wrong, so nothing but this branch can produce a finding.
    """
    ephemeral = fresh_pubkey()
    before, after = _ledgers(100_000, 0, 21_630_226, 21_730_226)
    after = dataclasses.replace(
        after,
        receipts_raw=receipts_account_bytes(
            [(191, ephemeral, 99_000, 9, "Water")], total=128
        ),
    )
    judged = _judge(
        _blank(),
        signature="sig",
        before=before,
        after=after,
        meta={"computeUnitsConsumed": 42_494, "fee": 5_000},
        simulated_units=42_494,
        window_blocks=151,
        buyer_ata="ata",
        store_ata="store",
        buyer=ephemeral,
        authority="Authority",
        mint=USDC,
    )
    assert judged.landed is True
    assert judged.receipt is not None and judged.receipt.price_raw == 99_000
    assert any(
        "99000" in problem and "100000" in problem for problem in judged.discrepancies
    ), f"the receipt/price mismatch was not reported: {judged.discrepancies}"
    # the money itself moved correctly — this finding is about the RECORD, not the transfer
    assert not any("do not cancel" in p for p in judged.discrepancies)

"""The delivery loop — the take verified offline, the whole thing against a real fork.

The split is the one the rest of the sandbox uses:

* ``fork``-marked legs start a local surfpool mainnet fork, generate a key, take a real
  storefront, SIGN, LAND a real ``mark_as_delivered`` and then read the row back. Deselect
  with ``-m "not fork"``.
* offline legs use a light fake fork that MODELS THE PROGRAM: on ``sendTransaction`` it
  decodes the bytes it was sent, reads the receipt id out of the instruction data, scans
  the vec, and flips ``was_delivered`` if it finds a match — returning a happy signature
  either way, which is the measured behaviour this module exists to expose. A fake that
  only recorded calls could not tell the two outcomes apart, and neither could the test.

Nothing here reaches mainnet.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

import pytest

from gecko.pda_testkit import (
    start_failure_is_a_broken_gate,
    SurfpoolError,
    SurfpoolFork,
    surfpool_status,
)
from gecko.sandbox import ephemeral_signer, prove_surfnet
from gecko.sandbox.cheatcodes import CheatcodeError
from gecko.sandbox.deliver import (
    MARK_AS_DELIVERED_DISCRIMINATOR,
    Delivery,
    DeliveryError,
    _build_mark_as_delivered,
    _judge,
    _Row,
    _take_the_store,
    rehearse_delivery,
)
from gecko.store_accounts import receipts_pda
from gecko.store_directory import LET_ME_BUY_PROGRAM_ID, authority_span, decode_store

# The purchase rehearsal's fake fork and offline proof — imported rather than re-written so
# both loops are exercised against ONE model of what a surfnet answers.
from test_sandbox_rehearse import FakeFork, fresh_pubkey, offline_proof

FORK_PORT = 8939
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

SKIP_REASON = (
    "surfpool is not on PATH (or GECKO_FORK_DEMO=0) — THE FORK LEG DID NOT RUN and "
    "NOTHING below was measured against a validator: " + surfpool_status().detail
)
needs_fork = pytest.mark.skipif(
    not surfpool_status().available or os.getenv("GECKO_FORK_DEMO") == "0",
    reason=SKIP_REASON,
)

CONFIG_PATH = os.path.expanduser(
    os.environ.get("GECKO_CI_CONFIG", "~/.gecko/catalog-ci.json")
)


def local_store() -> dict[str, str]:
    """The storefront the fork legs deliver from. No store name is committed here."""
    if not os.path.exists(CONFIG_PATH):
        pytest.skip(f"no {CONFIG_PATH} — the fork leg needs a real storefront")
    stored = json.loads(open(CONFIG_PATH).read())
    if not stored.get("store"):
        pytest.skip(f"{CONFIG_PATH} names no store")
    return dict(stored)


# ---------------------------------------------------------------------------
# a store account with real receipt rows, in the program's own layout
# ---------------------------------------------------------------------------


def _borsh_string(text: str) -> bytes:
    raw = text.encode()
    return len(raw).to_bytes(4, "little") + raw


def store_bytes(
    *,
    store_name: str,
    authority: str,
    rows: list[tuple[int, str, bool]],
    total: int = 128,
) -> bytes:
    """A `Receipts` account carrying ``(receipt_id, buyer, was_delivered)`` rows."""
    from solders.pubkey import Pubkey

    raw = b"\x00" * 8 + len(rows).to_bytes(4, "little")
    for receipt_id, buyer, delivered in rows:
        raw += receipt_id.to_bytes(8, "little")
        raw += bytes(Pubkey.from_string(buyer))
        raw += (1 if delivered else 0).to_bytes(1, "little")
        raw += (100_000).to_bytes(8, "little")  # price
        raw += (1_786_947_987).to_bytes(8, "little")  # timestamp
        raw += (7).to_bytes(1, "little")  # table_number
        raw += _borsh_string("Water")
    raw += total.to_bytes(8, "little")
    raw += _borsh_string(store_name)
    raw += bytes(Pubkey.from_string(authority))
    raw += (1).to_bytes(4, "little")  # one product
    raw += (100_000).to_bytes(8, "little")
    raw += (6).to_bytes(1, "little")
    raw += bytes(Pubkey.from_string(USDC))
    raw += _borsh_string("Water")
    raw += _borsh_string("")  # telegram channel
    return raw


class StoreFork(FakeFork):
    """A fake fork that MODELS ``mark_as_delivered`` — including its silence.

    ``sendTransaction`` decodes the bytes it was handed, pulls ``receipt_id`` out of the
    instruction data, scans the vec, and flips the row if it is there. If it is not there
    it does nothing AND STILL SUCCEEDS, which is the program's measured behaviour and the
    only reason an offline test can tell the two apart.
    """

    def __init__(self, rpc_url: str, *, store_name: str, raw: bytes) -> None:
        super().__init__(rpc_url)
        self.store_name = store_name
        self.receipts = receipts_pda(store_name)
        self.landed: list[int] = []
        self.accounts[self.receipts] = {
            "lamports": 26_510_640,
            "data": [base64.b64encode(raw).decode(), "base64"],
            "owner": LET_ME_BUY_PROGRAM_ID,
            "space": len(raw),
            "executable": False,
        }

    def raw(self) -> bytes:
        return base64.b64decode(self.accounts[self.receipts]["data"][0])

    def _surfnet_setAccount(self, params: list[Any]) -> None:  # noqa: N802
        """The measured asymmetry: ``data`` goes IN as hex and comes OUT as base64.

        Modelled rather than smoothed over, because it is the one transcode in the path
        (:func:`gecko.fork_preflight.surfnet_overlay` owns it) and a fake that stored the
        string verbatim would let a wrong encoding pass every offline test. ``fromhex``
        raises on base64, exactly as the real fork answers ``Invalid hex data provided``.
        """
        account = self.accounts.setdefault(
            params[0],
            {"lamports": 0, "data": ["", "base64"], "owner": "1" * 32, "space": 0},
        )
        update = dict(params[1])
        data_hex = update.pop("data", None)
        if data_hex is not None:
            decoded = bytes.fromhex(data_hex)
            update["data"] = [base64.b64encode(decoded).decode(), "base64"]
            update["space"] = len(decoded)
        account.update(update)

    def _sendTransaction(self, params: list[Any]) -> str:  # noqa: N802
        from solders.transaction import Transaction

        transaction = Transaction.from_bytes(base64.b64decode(params[0]))
        instruction = transaction.message.instructions[0]
        data = bytes(instruction.data)
        assert data[:8] == MARK_AS_DELIVERED_DISCRIMINATOR
        name_len = int.from_bytes(data[8:12], "little")
        assert data[12 : 12 + name_len].decode() == self.store_name
        receipt_id = int.from_bytes(data[12 + name_len : 20 + name_len], "little")
        self.landed.append(receipt_id)
        self._flip(receipt_id)
        return "S" * 64

    def _flip(self, receipt_id: int) -> None:
        """Scan the vec; flip if found; return Ok either way. The program's own shape."""
        raw = bytearray(self.raw())
        cursor = 8
        rows = int.from_bytes(raw[cursor : cursor + 4], "little")
        cursor += 4
        for _ in range(rows):
            row_id = int.from_bytes(raw[cursor : cursor + 8], "little")
            delivered_at = cursor + 8 + 32
            name_len = int.from_bytes(
                raw[delivered_at + 18 : delivered_at + 22], "little"
            )
            if row_id == receipt_id:
                raw[delivered_at] = 1
                break
            cursor = delivered_at + 22 + name_len
        self.accounts[self.receipts]["data"][0] = base64.b64encode(bytes(raw)).decode()

    def _getSignatureStatuses(self, _params: list[Any]) -> dict[str, Any]:  # noqa: N802
        return {
            "context": {"slot": 1},
            "value": [{"err": None, "confirmationStatus": "confirmed"}],
        }

    def _getTransaction(self, _params: list[Any]) -> dict[str, Any]:  # noqa: N802
        return {"meta": {"computeUnitsConsumed": 34_858, "fee": 5_000}}


def wired(rows: list[tuple[int, str, bool]], *, store: str = "teststore") -> StoreFork:
    authority = fresh_pubkey()
    return StoreFork(
        "http://127.0.0.1:9998",
        store_name=store,
        raw=store_bytes(store_name=store, authority=authority, rows=rows),
    )


# ---------------------------------------------------------------------------
# offline: the discriminator, and the join the IDL loses
# ---------------------------------------------------------------------------


def test_the_discriminator_is_the_one_the_program_dispatches_on() -> None:
    """Two independent derivations, and the pinned literal has to equal both.

    A rename upstream must refuse here rather than encode a call no dispatcher matches.
    """
    derived = hashlib.sha256(b"global:mark_as_delivered").digest()[:8]
    idl = json.loads(open("tests/fixtures/let_me_buy_idl.json").read())
    declared = next(
        bytes(ix["discriminator"])
        for ix in idl["instructions"]
        if ix["name"] == "mark_as_delivered"
    )
    assert MARK_AS_DELIVERED_DISCRIMINATOR == derived == declared


def test_one_store_name_fills_both_the_wire_arg_and_the_seed() -> None:
    """The IDL's seed says ``store_name``; the instruction's args say ``_store_name``.

    The builder takes ONE name, so the account it passes and the string it encodes cannot
    disagree — which is the failure a name-matching deriver walks straight into.
    """
    from solders.transaction import Transaction

    authority = fresh_pubkey()
    unsigned = _build_mark_as_delivered(
        store_name="teststore",
        receipt_id=191,
        authority=authority,
        blockhash="SURFNETxSAFEHASHxxxxxxxxxxxxxxxxxxxxxxxx177",
    )
    message = Transaction.from_bytes(base64.b64decode(unsigned)).message
    instruction = message.instructions[0]
    data = bytes(instruction.data)

    assert data[:8] == MARK_AS_DELIVERED_DISCRIMINATOR
    assert data[8:12] == (9).to_bytes(4, "little")
    assert data[12:21] == b"teststore"
    assert int.from_bytes(data[21:29], "little") == 191
    accounts = [str(message.account_keys[i]) for i in instruction.accounts]
    assert accounts == [receipts_pda("teststore"), authority]
    # the IDL's own order and flags: receipts(w, not signer), authority(w, signer)
    assert str(message.account_keys[0]) == authority, "the authority must pay"
    assert message.header.num_required_signatures == 1


# ---------------------------------------------------------------------------
# offline: the take verifies its own write
# ---------------------------------------------------------------------------


def test_the_take_rewrites_the_authority_and_touches_nothing_else() -> None:
    """Step 1's promise: 32 bytes, verified by reading the account back."""
    buyer = fresh_pubkey()
    fork = wired([(126, buyer, False)])
    proof = offline_proof(fork.rpc_url)
    signer = ephemeral_signer(proof)
    before = fork.raw()

    taken = _take_the_store(
        proof, receipts=fork.receipts, new_authority=signer.pubkey, call=fork
    )

    assert taken.authority_after == signer.pubkey
    assert taken.authority_before != signer.pubkey
    assert taken.collateral_changes == 0
    after = fork.raw()
    start, end = authority_span(before)
    assert after[:start] == before[:start] and after[end:] == before[end:]
    assert decode_store(after, address=fork.receipts).authority == signer.pubkey
    # and the vec the delivery is about survived the rewrite intact
    assert decode_store(after, address=fork.receipts).store_name == "teststore"


def test_a_cheatcode_that_silently_no_ops_is_caught_by_the_read_back() -> None:
    """``surfnet_setAccount`` ignores unknown fields and answers ``{"value": null}``.

    So "the RPC returned 200" is never evidence. A fork that accepts the write and keeps
    the old bytes must fail HERE — an unverified take would make every later reading a
    confident lie about a store we never took.
    """
    fork = wired([(126, fresh_pubkey(), False)])
    proof = offline_proof(fork.rpc_url)
    signer = ephemeral_signer(proof)
    fork._surfnet_setAccount = lambda _params: None  # type: ignore[method-assign]

    with pytest.raises(CheatcodeError) as refused:
        _take_the_store(
            proof, receipts=fork.receipts, new_authority=signer.pubkey, call=fork
        )
    assert signer.pubkey in str(refused.value)
    assert "did not take effect" in str(refused.value)


# ---------------------------------------------------------------------------
# offline: the two outcomes
# ---------------------------------------------------------------------------


def test_a_present_receipt_is_actually_flipped() -> None:
    """The control. A row that IS in the vec goes false -> true, and nothing is silent."""
    fork = wired([(125, fresh_pubkey(), False), (126, fresh_pubkey(), False)])
    proof = offline_proof(fork.rpc_url)

    result = rehearse_delivery(
        proof,
        authority=ephemeral_signer(proof),
        store="teststore",
        receipt_id=126,
        rpc_call=fork,
    )

    assert result.refusals == (), result.refusals
    assert result.landed is True
    assert result.signature
    assert result.receipt_found is True
    assert result.was_delivered_before is False
    assert result.was_delivered_after is True
    assert result.silently_did_nothing is False
    assert result.delivered is True
    assert result.discrepancies == ()
    assert result.units_consumed == 34_858
    assert fork.landed == [126]


def test_an_absent_receipt_lands_successfully_and_does_nothing() -> None:
    """THE FINDING, as a field rather than a comment.

    The vec is capped at 20 and ``make_purchase`` evicts the oldest without an error, so an
    id can simply not be there. ``mark_as_delivered`` scans, finds nothing and returns Ok:
    the signature confirms, the fee is charged, and the order stays undelivered. Every
    other instrument in this repo reads that as a clean success.
    """
    fork = wired([(125, fresh_pubkey(), False), (126, fresh_pubkey(), False)])
    proof = offline_proof(fork.rpc_url)

    result = rehearse_delivery(
        proof,
        authority=ephemeral_signer(proof),
        store="teststore",
        receipt_id=7,  # evicted long ago
        rpc_call=fork,
    )

    assert result.landed is True, "the transaction must SUCCEED — that is the point"
    assert result.signature
    assert result.receipt_found is False
    assert result.was_delivered_before is None, "absent is not the same fact as False"
    assert result.was_delivered_after is None
    assert result.silently_did_nothing is True
    assert result.delivered is False
    assert any("NO row with id 7" in problem for problem in result.discrepancies), (
        result.discrepancies
    )
    assert fork.landed == [7], "the program was asked, and it answered Ok"


def test_a_second_delivery_of_the_same_row_is_also_silent() -> None:
    """Already-true -> still-true is a transaction that changed nothing, and says so."""
    fork = wired([(126, fresh_pubkey(), True)])
    proof = offline_proof(fork.rpc_url)

    result = rehearse_delivery(
        proof,
        authority=ephemeral_signer(proof),
        store="teststore",
        receipt_id=126,
        rpc_call=fork,
    )

    assert result.landed is True
    assert result.receipt_found is True
    assert result.was_delivered_before is True and result.was_delivered_after is True
    assert result.silently_did_nothing is True
    assert result.delivered is False
    assert any("already was_delivered=true" in p for p in result.discrepancies)


# ---------------------------------------------------------------------------
# offline: the bindings and the cleanup
# ---------------------------------------------------------------------------


def test_a_signer_proven_against_another_fork_is_refused_before_anything_happens() -> (
    None
):
    """A key is bound to the ONE endpoint that proved itself — and the refusal is FIRST.

    A run that took the store and then noticed has already rewritten a storefront on a fork
    nobody proved.
    """
    elsewhere = ephemeral_signer(offline_proof("http://127.0.0.1:7777"))
    fork = wired([(126, fresh_pubkey(), False)])

    with pytest.raises(DeliveryError) as refused:
        rehearse_delivery(
            offline_proof(fork.rpc_url),
            authority=elsewhere,
            store="teststore",
            receipt_id=126,
            rpc_call=fork,
        )

    assert "7777" in str(refused.value)
    assert fork.seen == [], "the fork was contacted despite the binding not holding"


def test_the_store_is_given_back_however_the_run_ends() -> None:
    """The authority rewrite MUST be undone: the next run must not inherit a storefront
    owned by a key that no longer exists in any process."""
    fork = wired([(126, fresh_pubkey(), False)])
    proof = offline_proof(fork.rpc_url)

    result = rehearse_delivery(
        proof,
        authority=ephemeral_signer(proof),
        store="teststore",
        receipt_id=126,
        rpc_call=fork,
    )

    assert len(result.reset) == 2, result.reset
    resets = [m for _url, m in fork.seen if m == "surfnet_resetAccount"]
    assert len(resets) == 2
    assert fork.receipts not in fork.accounts, "the store kept the ephemeral authority"
    assert {url for url, _m in fork.seen} == {fork.rpc_url}


def test_skipping_the_take_leaves_the_store_untouched() -> None:
    """``take_the_store=False`` must not rewrite anything — the whole point is that the
    signer is a stranger to the storefront."""
    fork = wired([(126, fresh_pubkey(), False)])
    proof = offline_proof(fork.rpc_url)
    before = fork.raw()

    result = rehearse_delivery(
        proof,
        authority=ephemeral_signer(proof),
        store="teststore",
        receipt_id=126,
        take_the_store=False,
        rpc_call=fork,
    )

    assert result.took_the_store is None
    writes = [m for _url, m in fork.seen if m == "surfnet_setAccount"]
    assert writes == ["surfnet_setAccount"], "only the signer's lamports may be written"
    # the store still names the merchant it named before — the flip below came from a
    # signature the storefront has never heard of
    assert decode_store(before, address=fork.receipts).authority != result.authority
    assert result.was_delivered_after is True
    assert any("enforces no require!" in p for p in result.discrepancies)


def test_an_unlanded_run_does_not_render_as_a_delivery() -> None:
    """``delivered`` is None when nothing landed — an unanswered question, not a No."""
    assert (
        Delivery(store="s", receipt_id=1, authority="a", landed=False).delivered is None
    )


# ---------------------------------------------------------------------------
# the fork legs — a real storefront, a real signature, a real read-back
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fork() -> Any:
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    try:
        with SurfpoolFork(mainnet, port=FORK_PORT, ready_timeout=180) as running:
            yield running
    except SurfpoolError as exc:
        # Installed and would not start = a BROKEN gate, not an absent one.
        if start_failure_is_a_broken_gate():
            pytest.fail(
                "surfpool IS installed and the fork did not start, so this "
                "gate is broken rather than absent — a skip here would claim "
                f"the environment cannot do what it demonstrably can: {exc}"
            )
        pytest.skip(f"THE FORK LEG DID NOT RUN — surfpool never became ready: {exc}")


def _resident(fork_rpc: str, store: str) -> list[dict[str, Any]]:
    """The rows the real store holds right now, read off the fork."""
    from gecko.rpc import default_rpc_call
    from gecko.sandbox.rehearse import _account, _raw_data, _walk_receipts

    raw = _raw_data(_account(default_rpc_call, fork_rpc, receipts_pda(store)))
    assert raw is not None
    rows, _total = _walk_receipts(raw)
    return rows


@pytest.mark.fork
@needs_fork
def test_a_resident_receipt_is_delivered_on_a_real_fork(fork: Any) -> None:
    """TAKE, FUND, BUILD, SIGN, LAND, READ BACK — against a mainnet storefront's own bytes."""
    config = local_store()
    proof = prove_surfnet(fork.rpc_url)
    rows = [
        row for row in _resident(fork.rpc_url, config["store"]) if not row["delivered"]
    ]
    if not rows:
        pytest.skip("every resident receipt is already delivered on this store")

    result = rehearse_delivery(
        proof,
        authority=ephemeral_signer(proof),
        store=config["store"],
        receipt_id=int(rows[-1]["receipt_id"]),
    )

    assert not result.refusals, result.refusals
    assert result.landed is True
    assert result.took_the_store is not None
    assert result.took_the_store.collateral_changes == 0
    assert result.receipt_found is True
    assert result.was_delivered_before is False
    assert result.was_delivered_after is True
    assert result.silently_did_nothing is False
    assert result.discrepancies == ()
    # compute is not constant here — it scans the vec — so it is reported, never asserted
    assert result.units_consumed and result.resident_bytes


@pytest.mark.fork
@needs_fork
def test_any_key_at_all_can_mark_a_receipt_delivered(fork: Any) -> None:
    """THE SECOND FINDING, and it is a live authorization hole rather than a quirk.

    The run does NOT take the store: the signer is a key generated seconds earlier with no
    relationship to the storefront, and the IDL says ``authority`` is a ``signer``. Anchor
    checks that somebody signed, never who, and ``mark_as_delivered`` carries no
    ``require!`` against ``receipts.authority`` — the guard the same program does apply in
    five of its other instructions. Measured 2026-08-17: 34,870 CU, ``err: null``, row
    flipped.

    If this ever goes RED the program has been fixed and the finding is retired. That is
    the only reading of a failure here, and it is worth having.
    """
    config = local_store()
    proof = prove_surfnet(fork.rpc_url)
    rows = [
        row for row in _resident(fork.rpc_url, config["store"]) if not row["delivered"]
    ]
    if not rows:
        pytest.skip("every resident receipt is already delivered on this store")
    stranger = ephemeral_signer(proof)

    result = rehearse_delivery(
        proof,
        authority=stranger,
        store=config["store"],
        receipt_id=int(rows[-1]["receipt_id"]),
        take_the_store=False,
    )

    assert result.took_the_store is None, "the store was taken; this proves nothing"
    assert result.landed is True
    assert result.was_delivered_before is False
    assert result.was_delivered_after is True, (
        "the program refused a stranger — the authority hole is CLOSED and this test, the "
        "module docstring and the config notes must all be corrected"
    )
    assert any("enforces no require!" in p for p in result.discrepancies)


@pytest.mark.fork
@needs_fork
def test_an_evicted_receipt_id_succeeds_and_changes_nothing_on_a_real_fork(
    fork: Any,
) -> None:
    """The finding, on the real program: a confirmed transaction that did nothing.

    ``receipt_id=1`` is long gone from any store that has taken more than 20 orders — the
    vec is capped and ``make_purchase`` evicts the oldest silently. The program scans,
    finds nothing, and returns Ok.
    """
    config = local_store()
    proof = prove_surfnet(fork.rpc_url)
    resident = {
        int(row["receipt_id"]) for row in _resident(fork.rpc_url, config["store"])
    }
    evicted = next(i for i in range(1, 10_000) if i not in resident)

    result = rehearse_delivery(
        proof,
        authority=ephemeral_signer(proof),
        store=config["store"],
        receipt_id=evicted,
    )

    assert not result.refusals, result.refusals
    assert result.landed is True, "the transaction must SUCCEED"
    assert result.signature
    assert result.receipt_found is False
    assert result.silently_did_nothing is True
    assert any(f"NO row with id {evicted}" in p for p in result.discrepancies)


def _blank_delivery(receipt_id: int = 126) -> Delivery:
    """The half-built Delivery `_judge` is handed, with nothing decided yet."""
    return Delivery(
        store="teststore",
        receipt_id=receipt_id,
        authority=fresh_pubkey(),
        landed=True,
    )


def test_a_read_failure_after_the_transaction_is_not_the_eviction_finding() -> None:
    """An adversarial pass produced the headline finding out of a transient RPC miss.

    When the post-transaction `getAccountInfo` comes back empty, `present` is False for a
    reason that has nothing to do with the program — the node was behind, the account paged
    out, the RPC hiccuped. The judge used to set `silently_did_nothing` on `not present`
    unconditionally, so a read blip rendered as "the program scanned N rows, found nothing
    and returned Ok": an observation nobody made, and the headline of the whole
    demonstration, manufactured by infrastructure.

    Unknown is its own answer, and this pins it.
    """
    before = _Row(present=True, was_delivered=False, rows=20, account_bytes=3681)
    after = _Row(
        present=False,
        was_delivered=None,
        rows=None,
        account_bytes=None,
        unreadable="the store account could not be read",
    )

    judged = _judge(
        _blank_delivery(),
        taken=None,
        before=before,
        after=after,
        signature="sig",
        meta={"computeUnitsConsumed": 34_870, "fee": 5_000},
    )

    assert judged.silently_did_nothing is False, "a read failure is not the finding"
    assert judged.delivered is None, "an unanswered question is not a No"
    assert any("UNKNOWN" in d for d in judged.discrepancies)
    assert not any("returned Ok" in d for d in judged.discrepancies), (
        "must not assert what the program did when we could not look"
    )


def test_a_genuine_absence_is_still_the_finding() -> None:
    """The other branch, or the fix above just deleted the thing it was narrowing."""
    before = _Row(present=True, was_delivered=False, rows=20, account_bytes=3681)
    after = _Row(present=False, was_delivered=None, rows=20, account_bytes=3681)
    judged = _judge(
        _blank_delivery(),
        taken=None,
        before=before,
        after=after,
        signature="sig",
        meta={"computeUnitsConsumed": 34_137, "fee": 5_000},
    )
    assert judged.silently_did_nothing is True
    assert any("returned Ok" in d for d in judged.discrepancies)

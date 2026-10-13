"""The global rebate index and the relay balance check, offline.

Review finding 5: a rebate signature was unique only inside one customer's database, so
two customers with a payment in the same slot could both be credited the same rebate.
The index sits outside every per-customer database and holds only public chain data.
"""

from __future__ import annotations

import logging

import pytest

from gecko.anyquery.config import AnyQueryConfig
from gecko.anyquery.rebates import (
    REBATE_INDEX_DB,
    MemoryRebateIndex,
    MongoRebateIndex,
    RebateIndex,
    RebateIndexError,
    RebateRow,
    check_relay_balance,
)
from tests.anyquery_fake_mongo import FakeMongoClient, FakeUnavailableError

SIG = "4" + "R" * 86
PAY_A = "5" + "A" * 86
PAY_B = "5" + "B" * 86


def _indexes() -> list[RebateIndex]:
    return [
        MemoryRebateIndex(),
        MongoRebateIndex(FakeMongoClient(), unavailable=(FakeUnavailableError,)),
    ]


@pytest.mark.parametrize("index", _indexes(), ids=["memory", "mongo"])
def test_a_signature_is_claimed_by_one_payment_only(index: RebateIndex) -> None:
    assert index.claim(SIG, slot=7, lamports=100, payment_signature=PAY_A) is True
    assert index.claim(SIG, slot=7, lamports=100, payment_signature=PAY_B) is False
    assert index.row(SIG) == RebateRow(SIG, 7, 100, PAY_A)


@pytest.mark.parametrize("index", _indexes(), ids=["memory", "mongo"])
def test_a_claim_by_the_same_payment_is_idempotent(index: RebateIndex) -> None:
    """A crash between the claim and the ledger write is recoverable: the retry claims
    again for the same payment and writes the credit; no other payment ever can."""
    assert index.claim(SIG, slot=7, lamports=100, payment_signature=PAY_A)
    assert index.claim(SIG, slot=7, lamports=100, payment_signature=PAY_A)


@pytest.mark.parametrize("index", _indexes(), ids=["memory", "mongo"])
def test_an_unattributed_inflow_can_be_claimed_once_later(index: RebateIndex) -> None:
    index.record_unattributed(SIG, slot=7, lamports=100)
    assert index.row(SIG) == RebateRow(SIG, 7, 100, None)
    index.record_unattributed(SIG, slot=7, lamports=100)  # idempotent

    assert index.claim(SIG, slot=7, lamports=100, payment_signature=PAY_A)
    assert not index.claim(SIG, slot=7, lamports=100, payment_signature=PAY_B)
    # Recording it unattributed again never takes the attribution away.
    index.record_unattributed(SIG, slot=7, lamports=100)
    assert index.row(SIG) == RebateRow(SIG, 7, 100, PAY_A)


@pytest.mark.parametrize("index", _indexes(), ids=["memory", "mongo"])
def test_the_index_refuses_a_claim_whose_facts_changed(index: RebateIndex) -> None:
    index.record_unattributed(SIG, slot=7, lamports=100)
    with pytest.raises(RebateIndexError):
        index.claim(SIG, slot=8, lamports=100, payment_signature=PAY_A)
    with pytest.raises(RebateIndexError):
        index.claim(SIG, slot=7, lamports=0, payment_signature=PAY_A)


def test_the_mongo_index_lives_outside_every_customer_database() -> None:
    client = FakeMongoClient()
    index = MongoRebateIndex(client)
    index.claim(SIG, slot=7, lamports=100, payment_signature=PAY_A)

    assert client.database_names() == {REBATE_INDEX_DB}
    assert not REBATE_INDEX_DB.startswith("gq_")
    [doc] = client[REBATE_INDEX_DB]["rebates"].docs
    # Only public chain data: no customer, run, quote or amount owed.
    assert set(doc) == {"_id", "signature", "slot", "lamports", "payment_signature"}


def test_the_mongo_index_fails_closed_without_the_uri() -> None:
    client = FakeMongoClient(down=True)
    index = MongoRebateIndex(client, unavailable=(FakeUnavailableError,))
    with pytest.raises(RebateIndexError) as caught:
        index.claim(SIG, slot=7, lamports=100, payment_signature=PAY_A)
    assert "hunter2" not in str(caught.value)


# -- the relay's hot balance ----------------------------------------------------------

CONFIG = AnyQueryConfig()


@pytest.mark.parametrize(
    ("lamports", "status", "word"),
    [
        (5_000_000, "low", "top up"),
        (50_000_000, "ok", None),
        (100_000_000, "ok", None),
        (100_000_001, "sweep", "sweep"),
    ],
)
def test_the_relay_balance_check_logs_a_reminder_for_the_founder(
    lamports: int, status: str, word: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="gecko.anyquery.rebates")

    verdict = check_relay_balance(lamports, CONFIG)

    assert verdict.status == status and verdict.lamports == lamports
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    if word is None:
        assert warnings == []
    else:
        [record] = warnings
        assert word in record.getMessage() and word in verdict.reason


def test_the_balance_check_never_moves_money() -> None:
    """It is a reminder only: the founder sweeps by hand. Nothing here can sign."""
    import gecko.anyquery.rebates as module

    source = open(module.__file__, encoding="utf-8").read()
    for forbidden in ("sendTransaction", "sign_transaction", "Keypair"):
        assert forbidden not in source

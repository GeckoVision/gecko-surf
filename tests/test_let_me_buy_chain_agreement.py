"""THE FALSIFICATION TEST: the derived ``make_purchase`` account set vs. what mainnet
actually executed — account for account, in order, across twelve landed transactions.

Every other program packaged in ``gecko/providers/configs/`` is fork-simulated only, so
a wrong derive plan there is invisible: a simulation runs against accounts the same plan
chose, and agrees with itself. ``let_me_buy`` is the one surface with LANDED MAINNET
TRANSACTIONS, which means the plan can be checked against a set of accounts it did not
produce. That is the only place a silent derive-plan error could have surfaced, and this
file is where it is looked for.

WHAT IS PREDICTED AND WHAT IS BOUND — read this before reading the agreement rate.
Nine accounts per instruction. Three of them (``signer``, ``authority``, ``mint``) are
CALLER-SUPPLIED inputs: they are read off the chain row and fed back in, so they are
bindings, not predictions, and asserting them would be the tautology of #363 (a receipt
verifying the bytes it was derived from). Six are genuinely predicted from the packaged
config alone — the ``receipts`` PDA, the two associated-token accounts, and the three
pinned program ids — plus the ARITY (9) and the ORDER of all nine, which the config's
recipe set has to get right or the instruction addresses the wrong account.

THE HARD RULE THIS FILE OBEYS. If the derivation and the chain disagree, the config is
NOT edited to agree. A config tuned until it matches the chain proves nothing; the
disagreement is the finding. The verdict below is what it computed, not what was wanted.

TRI-STATE, FAIL-CLOSED (C1). ``chain_verdict`` returns ``AGREE``/``DISAGREE``/
``NOT_EVALUATED``. Absent, empty, or unparseable evidence is ``NOT_EVALUATED`` — never
silently confident. A downstream consumer must treat anything other than an explicit
``AGREE`` as unproven and flag the plan; the closed ``Literal`` for that axis is S1's to
own in ``gecko/provenance.py``, and the vocabulary here is pinned with ``==`` so a fourth
state cannot be appended quietly.

PATTERN B. Every assertion over the recorded fixture runs with NO network — proven in
this file by ``test_the_whole_comparison_runs_with_the_network_broken``, which breaks
``gecko.rpc._http_post_json`` so any reach for the wire raises. The fetching leg is
opt-in (``GECKO_CHAIN_REFETCH=1``) and SKIPS loudly offline rather than passing silently.

Read-only, $0, no signing, no broadcast.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import pytest

from gecko.pda import derive_pda
from gecko.provider_config import ProgramSpec, load_packaged_provider

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "let_me_buy_mainnet_accounts.json"

PROGRAM_ID = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

#: The bar these twelve purchases were made at. The ONLY store-side input to the
#: derivation — everything about the ``receipts`` account follows from this one string.
STORE_NAME = "jonasbar"

#: Anchor's ``sha256("global:make_purchase")[:8]``. Recorded here so the refetch leg
#: selects the right instruction by DISCRIMINATOR rather than by "the first instruction
#: that happens to hit the program id".
MAKE_PURCHASE_DISCRIMINATOR = "c13ee38869d4c914"

#: ``make_purchase``'s account list in IDL order, as the packaged config's ``notes``
#: state it. Position IS the contract: Solana matches accounts by index, so an ordering
#: error builds a transaction that addresses the wrong account with no name to catch it.
MAKE_PURCHASE_ACCOUNT_ORDER: tuple[str, ...] = (
    "receipts",
    "signer",
    "authority",
    "mint",
    "sender_token_account",
    "recipient_token_account",
    "token_program",
    "system_program",
    "associated_token_program",
)

#: The honesty split over those nine positions, pinned with ``==`` so the agreement rate
#: can never be read as "nine of nine predicted".
BOUND_ROLES = frozenset({"signer", "authority", "mint"})
DERIVED_ROLES = frozenset(
    {"receipts", "sender_token_account", "recipient_token_account"}
)
PINNED_ROLES = frozenset(
    {"token_program", "system_program", "associated_token_program"}
)

#: Where each fetched signature came from (C8 — denominator honesty). Eleven are the
#: published set in ``gecko-docs/mainnet.mdx``; the twelfth (``5XHQCMgb…``, slot
#: 438494959) is the later agent-signed purchase and is NOT in that file. Anything that
#: publishes twelve must render it as "11 from mainnet.mdx + 1 agent-signed" rather than
#: restating the published "eleven" as twelve.
SIGNATURE_SOURCES: dict[str, str] = {
    # the first purchase — mainnet.mdx, slot 437633643
    "5cjBs5VE8WVVctG2EoUkYiRkW92sXkoT4YsNxszWC9CE3sK7triTJ5vnY6TrcQ2BRPYUtWsd3LtTnyieUfn8Hw2Y": "mainnet.mdx",
    # the ten that followed — mainnet.mdx, slots 437721653-437723077
    "4yBoPqPc6YqPQjEeC7RGXtgCH9XG9zyx7CXYQ4WdTijbrLWDvE8Yo4eKFU4JAannDtcZJY4BdJN9J1J6mnmrsrsv": "mainnet.mdx",
    "3GvCjqxuy5KBuyLRL4f9SKpJrETMNhM6FP4fXBpTzzQJVDW6tXvpfAovy4zFn3j4fSMh2VeJ8VAdxWVuak5Jwi5R": "mainnet.mdx",
    "3MM7QMLzpoZNcN2JSnBcSxDiWeDWJgbMHe7pHNcGfw6EAKXLTz5kEuAwg6pbxCnVgTzjBFfcEFFo1BQs2RVGcHwb": "mainnet.mdx",
    "zXsDSEQtZt24FkT4eeNpiu4aCB1HXUKPKCeEG291CdHPj6NnfhZzu1zkvvunrCBu7ei2uhoRtmytMDMD9kXF5C8": "mainnet.mdx",
    "o6Da6NZQe4DFag8VNEGHqCY3zuYocfdWT7n7Z7tqNbAJFBg69pNsCSiN51HeDuouW6T6XQeHicVZaWrpsdRFSi9": "mainnet.mdx",
    "3VrHHuzi2gC5QjnXBdWRAb5nhDkEPowVuZBnMNCqac8Ty2NeuykAQm7GA44qXfUt5TzCL5yQH4Ussi6nvKqpDi2u": "mainnet.mdx",
    "4QBnD6Yc6sGYzeCVmbi9CpJoZdRfjBEtwMRt8XWVdjpeXwBrM2ApQmHKUACTvX2t3FNxyDqErZNDTY3ifuwE8iRj": "mainnet.mdx",
    "2NBodoD8VNg1wqUXaxNQJUKTpZ9gkmM445d9NZFXLygaYuxtDyDQsH2MSp8Mdd3BYHci9Q6EPMaCJBGBQWaP5a1B": "mainnet.mdx",
    "33NkTQ5uJL85AtpsLMbWJu9L1PT9dKA4jEm5bFW9b2TpKcP7pb3br2up1KiZWmrzBzyPHvWS98rEwpKv7ac3WLW1": "mainnet.mdx",
    "3i92aBEYsbHBotKcpaB8SxWDWEULp2SuQcqyeShY8sDCzFWQq4MLaN6URreG4QdBF3qRGR3eSYpqQR72kD2wAMrs": "mainnet.mdx",
    # the agent-signed purchase — slot 438494959, NOT published in mainnet.mdx
    "5XHQCMgbN3uBQpgA7HJJNsC6S1r2b4QHNcvturPn9EoEV1FMSJCExcmrMff2m36vyDeTrw5XR57VqBQTp4RNyCrM": "agent-signed",
}

#: The closed chain-agreement vocabulary. ``==`` on purpose (C4's shape): a fourth state
#: is a visible, failing test edit, never a quiet append. ``NOT_EVALUATED`` is the
#: fail-closed member — missing, empty, or unparseable evidence lands here, and a
#: consumer that cannot read an explicit ``AGREE`` must flag the plan.
CHAIN_VERDICTS = frozenset({"AGREE", "DISAGREE", "NOT_EVALUATED"})

#: The only field names the fixture is allowed to carry (control-plane invariant #1).
PERMITTED_RECORD_FIELDS = frozenset({"signature", "instruction_index", "accounts"})


def _let_me_buy() -> ProgramSpec:
    """Reach the config the way the loader does — through ``provider.apis``, never a path."""
    _provider, apis = load_packaged_provider("orquestra")
    program = apis["let_me_buy"].program
    assert program is not None
    return program


def _load_records() -> list[dict[str, Any]]:
    """The recorded chain truth. No network."""
    data = json.loads(FIXTURE_PATH.read_text())
    return list(data["transactions"])


def _derive_make_purchase_accounts(
    program: ProgramSpec,
    chain_accounts: list[str],
    *,
    store_name: str = STORE_NAME,
    swap_ata_owners: bool = False,
) -> list[str]:
    """Build ``make_purchase``'s nine accounts, in order, from the packaged config.

    The three CALLER-SUPPLIED accounts (``signer``, ``authority``, ``mint``) are read out
    of ``chain_accounts`` at their documented positions and used as BINDINGS — a buyer's
    wallet and a store's authority are inputs to a purchase, not things a seed recipe can
    predict. Everything else is computed: the ``receipts`` PDA from ``store_name``, the
    two associated-token accounts from the config's own ATA recipes, and the three pinned
    program ids from the IDL.

    ``swap_ata_owners`` derives BOTH token accounts from the buyer — the trap the config's
    notes warn about (a purchase that pays the buyer back). It exists so the comparison
    can be shown to fail on a wrong plan.
    """
    signer = chain_accounts[MAKE_PURCHASE_ACCOUNT_ORDER.index("signer")]
    authority = chain_accounts[MAKE_PURCHASE_ACCOUNT_ORDER.index("authority")]
    mint = chain_accounts[MAKE_PURCHASE_ACCOUNT_ORDER.index("mint")]

    ata_bindings = {"token_program": TOKEN_PROGRAM, "mint": mint}
    sender_owner = signer
    recipient_owner = signer if swap_ata_owners else authority

    receipts = derive_pda(program.pdas["receipts"], {"store_name": store_name})
    sender = derive_pda(
        program.pdas["sender_token_account"], {**ata_bindings, "signer": sender_owner}
    )
    recipient = derive_pda(
        program.pdas["recipient_token_account"],
        {**ata_bindings, "authority": recipient_owner},
    )
    return [
        receipts.address,
        signer,
        authority,
        mint,
        sender.address,
        recipient.address,
        TOKEN_PROGRAM,
        SYSTEM_PROGRAM,
        ATA_PROGRAM,
    ]


def compare_record(
    program: ProgramSpec,
    record: dict[str, Any],
    *,
    derive: Callable[..., list[str]] = _derive_make_purchase_accounts,
    **derive_kwargs: Any,
) -> list[tuple[int, str, str, str, bool]]:
    """One transaction's account-by-account table.

    Rows are ``(position, role, derived, on_chain, agrees)``. Raises ``ValueError`` on a
    record this cannot honestly evaluate — the caller turns that into ``NOT_EVALUATED``.
    """
    if not PERMITTED_RECORD_FIELDS <= set(record):
        raise ValueError(
            f"record is missing fields: {sorted(PERMITTED_RECORD_FIELDS - set(record))}"
        )
    chain_accounts = record["accounts"]
    if (
        not isinstance(chain_accounts, list)
        or len(chain_accounts) != len(MAKE_PURCHASE_ACCOUNT_ORDER)
        or not all(isinstance(key, str) for key in chain_accounts)
    ):
        raise ValueError(
            f"record {record.get('signature')!r} does not carry "
            f"{len(MAKE_PURCHASE_ACCOUNT_ORDER)} account pubkeys"
        )
    derived = derive(program, chain_accounts, **derive_kwargs)
    return [
        (
            position,
            role,
            derived[position],
            chain_accounts[position],
            derived[position] == chain_accounts[position],
        )
        for position, role in enumerate(MAKE_PURCHASE_ACCOUNT_ORDER)
    ]


def chain_verdict(
    program: ProgramSpec,
    records: list[dict[str, Any]] | None,
    **compare_kwargs: Any,
) -> str:
    """The tri-state chain-agreement verdict — fail-closed.

    ``NOT_EVALUATED`` when there is no evidence, no records, or a record that cannot be
    parsed into a comparison. ``DISAGREE`` when any position on any transaction differs.
    ``AGREE`` only when every position of every transaction matched.
    """
    if not records:
        return "NOT_EVALUATED"
    agreed = True
    for record in records:
        try:
            rows = compare_record(program, record, **compare_kwargs)
        except (ValueError, KeyError, TypeError):
            return "NOT_EVALUATED"
        if not all(row[4] for row in rows):
            agreed = False
    return "AGREE" if agreed else "DISAGREE"


# --------------------------------------------------------------------------------------
# the fixture itself
# --------------------------------------------------------------------------------------


def test_the_fixture_carries_only_the_three_permitted_fields() -> None:
    """Control-plane invariant #1, enforced rather than promised: no response body, no
    logs, no balances, no amounts. If a future refetch widens the fixture, this is what
    stops it."""
    for record in _load_records():
        assert set(record) == PERMITTED_RECORD_FIELDS, (
            f"{record.get('signature')} carries fields outside the permitted set: "
            f"{sorted(set(record) - PERMITTED_RECORD_FIELDS)}"
        )


def test_the_denominator_is_eleven_published_plus_one_agent_signed() -> None:
    """C8. The brief said twelve; ``gecko-docs/mainnet.mdx`` yields ELEVEN unique
    signatures and STATE.md publishes "eleven". The twelfth is real but comes from
    elsewhere (the agent-signed purchase at slot 438494959), so the split is pinned here
    with ``==`` and any published "12" has to say where it came from."""
    records = _load_records()
    signatures = {record["signature"] for record in records}
    assert len(records) == 12
    assert signatures == set(SIGNATURE_SOURCES)
    by_source = sorted(SIGNATURE_SOURCES.values())
    assert by_source.count("mainnet.mdx") == 11
    assert by_source.count("agent-signed") == 1
    # Every one of the twelve settled in the same currency — the mint is a BOUND input,
    # so this documents the set that was measured; it is not a prediction the config made.
    mint_position = MAKE_PURCHASE_ACCOUNT_ORDER.index("mint")
    assert {record["accounts"][mint_position] for record in records} == {USDC}


def test_the_predicted_and_bound_positions_are_pinned() -> None:
    """The agreement rate is only honest next to this split: six of the nine positions
    are predicted from the config, three are caller-supplied inputs read off the chain."""
    assert BOUND_ROLES | DERIVED_ROLES | PINNED_ROLES == set(
        MAKE_PURCHASE_ACCOUNT_ORDER
    )
    assert BOUND_ROLES & DERIVED_ROLES == set()
    assert BOUND_ROLES & PINNED_ROLES == set()
    assert DERIVED_ROLES & PINNED_ROLES == set()
    assert len(BOUND_ROLES) == 3
    assert len(DERIVED_ROLES | PINNED_ROLES) == 6


def test_the_verdict_vocabulary_is_closed_and_carries_not_evaluated() -> None:
    """C1: the axis is tri-state. ``==`` so a fourth member is a failing edit."""
    assert CHAIN_VERDICTS == frozenset({"AGREE", "DISAGREE", "NOT_EVALUATED"})


# --------------------------------------------------------------------------------------
# the falsification test
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("record", _load_records(), ids=lambda r: r["signature"][:8])
def test_the_derived_accounts_match_what_the_chain_executed(
    record: dict[str, Any],
) -> None:
    """Account for account, in order, against a landed mainnet transaction.

    A mismatch here is the most valuable outcome this repo can produce — it would mean
    the derive plan has been wrong while every fork simulation passed. It is reported,
    never fixed by editing the config to agree.
    """
    program = _let_me_buy()
    rows = compare_record(program, record)
    mismatches = [row for row in rows if not row[4]]
    assert not mismatches, f"CHAIN DISAGREEMENT on {record['signature']}: " + "; ".join(
        f"[{position}] {role}: derived {derived} != chain {chain}"
        for position, role, derived, chain, _ in mismatches
    )
    assert len(rows) == 9


def test_the_verdict_over_every_landed_transaction(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The single string a consumer reads. Printed with its denominator so the number
    never travels without the set it was measured over."""
    program = _let_me_buy()
    records = _load_records()
    verdict = chain_verdict(program, records)
    positions = len(records) * len(MAKE_PURCHASE_ACCOUNT_ORDER)
    print(
        f"VERDICT: {verdict} — {positions}/{positions} account positions over "
        f"{len(records)} landed make_purchase transactions "
        f"(11 from mainnet.mdx + 1 agent-signed); of the 9 positions per transaction, "
        f"6 are predicted from the packaged config and 3 are caller-supplied bindings"
    )
    assert verdict in CHAIN_VERDICTS
    assert verdict == "AGREE"


# --------------------------------------------------------------------------------------
# the comparison can fail — otherwise AGREE means nothing
# --------------------------------------------------------------------------------------


def test_a_wrong_store_name_disagrees_with_the_chain() -> None:
    """If the ``receipts`` recipe were wrong, this comparison would catch it. Changing
    the one store-side input moves position 0 off the account the chain used."""
    program = _let_me_buy()
    verdict = chain_verdict(program, _load_records(), store_name="not-jonasbar")
    assert verdict == "DISAGREE"


def test_deriving_both_token_accounts_from_the_buyer_disagrees_with_the_chain() -> None:
    """The trap the config's notes name: same mint, same token program, WRONG owner —
    a purchase that credits the buyer instead of the bar. It builds fine and it
    simulates fine on a fork; only the chain row says it is wrong."""
    program = _let_me_buy()
    verdict = chain_verdict(program, _load_records(), swap_ata_owners=True)
    assert verdict == "DISAGREE"


def test_a_reordered_account_list_disagrees_with_the_chain() -> None:
    """Position is the contract. Swapping two accounts in the chain row must be caught,
    which proves the comparison is order-sensitive and not a set comparison."""
    program = _let_me_buy()
    record = dict(_load_records()[0])
    accounts = list(record["accounts"])
    accounts[4], accounts[5] = accounts[5], accounts[4]
    record["accounts"] = accounts
    assert chain_verdict(program, [record]) == "DISAGREE"


# --------------------------------------------------------------------------------------
# fail-closed: no evidence is never confidence (C1)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "records",
    [
        pytest.param(None, id="absent"),
        pytest.param([], id="empty"),
        pytest.param(
            [{"signature": "x", "instruction_index": 0}], id="missing-accounts"
        ),
        pytest.param(
            [{"signature": "x", "instruction_index": 0, "accounts": ["only", "two"]}],
            id="wrong-arity",
        ),
        pytest.param(
            [{"signature": "x", "instruction_index": 0, "accounts": "not-a-list"}],
            id="unparseable",
        ),
    ],
)
def test_missing_or_unparseable_evidence_is_not_evaluated_never_agree(
    records: list[dict[str, Any]] | None,
) -> None:
    """C1. A verdict that cannot be computed is ``NOT_EVALUATED``, and a consumer must
    flag the plan. It is never quietly ``AGREE``, and it is not ``DISAGREE`` either —
    "we did not check" and "we checked and it was wrong" are different facts."""
    program = _let_me_buy()
    verdict = chain_verdict(program, records)
    assert verdict == "NOT_EVALUATED"
    assert verdict != "AGREE"


# --------------------------------------------------------------------------------------
# Pattern B: the recorded leg needs no network; the fetching leg says when it did not run
# --------------------------------------------------------------------------------------


def test_the_whole_comparison_runs_with_the_network_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pattern B, proven rather than asserted: break the ONE transport seam
    (``gecko.rpc._http_post_json``) so any reach for the wire raises, then run the full
    fixture comparison. A fixture-backed regression test that silently needed a node
    would be a live smoke test wearing a unit test's name."""
    import gecko.rpc

    def _no_network(url: str, body: bytes) -> dict[str, Any]:
        raise AssertionError(
            "the recorded comparison reached for the network — it must not"
        )

    monkeypatch.setattr(gecko.rpc, "_http_post_json", _no_network)

    program = _let_me_buy()
    records = _load_records()
    assert chain_verdict(program, records) == "AGREE"
    for record in records:
        assert all(row[4] for row in compare_record(program, record))


@pytest.mark.skipif(
    os.environ.get("GECKO_CHAIN_REFETCH") != "1",
    reason=(
        "DID NOT RUN: the live getTransaction refetch is opt-in. The recorded fixture "
        "was NOT re-verified against a node in this run. Set GECKO_CHAIN_REFETCH=1 "
        "(optionally RPC=<url>) to re-fetch. Read-only, $0, no signing, no broadcast."
    ),
)
def test_refetching_from_a_node_reproduces_the_recorded_fixture() -> None:
    """The fetching leg. Read-only ``getTransaction`` through the canonical transport —
    every read goes through ``default_rpc_call``, which validates the URL scheme
    (``gecko/rpc.py:76``); no bypass path is added here and none is weakened.

    Selects the instruction by Anchor DISCRIMINATOR, not by position, and stores nothing
    but what the fixture is permitted to carry.
    """
    import time

    from gecko.rpc import default_rpc_call
    from gecko.txbind import _b58decode

    rpc_url = os.environ.get("RPC", "https://api.mainnet-beta.solana.com")

    def _get_transaction(signature: str) -> dict[str, Any]:
        """Twelve reads in a row 429s the free public endpoint. Backing off is transport
        courtesy, not leniency about the answer — a transport failure that never clears
        raises, and a wrong account still fails."""
        last: Exception | None = None
        for attempt in range(6):
            try:
                return default_rpc_call(
                    rpc_url,
                    "getTransaction",
                    [
                        signature,
                        {"maxSupportedTransactionVersion": 0, "encoding": "json"},
                    ],
                )
            except Exception as exc:  # noqa: BLE001 - transport flake, not the verdict
                last = exc
                time.sleep(2 + 3 * attempt)
        raise AssertionError(f"could not read {signature} from {rpc_url}: {last}")

    for record in _load_records():
        signature = record["signature"]
        response = _get_transaction(signature)
        result = response["result"]
        assert result is not None, f"{signature} not found on {rpc_url}"
        message = result["transaction"]["message"]
        loaded = (result.get("meta") or {}).get("loadedAddresses") or {}
        keys = (
            list(message["accountKeys"])
            + list(loaded.get("writable") or [])
            + list(loaded.get("readonly") or [])
        )
        found = [
            (index, [keys[i] for i in instruction["accounts"]])
            for index, instruction in enumerate(message["instructions"])
            if keys[instruction["programIdIndex"]] == PROGRAM_ID
            and _b58decode(instruction["data"])[:8].hex() == MAKE_PURCHASE_DISCRIMINATOR
        ]
        assert len(found) == 1, (
            f"{signature}: expected one make_purchase, got {len(found)}"
        )
        instruction_index, accounts = found[0]
        assert instruction_index == record["instruction_index"]
        assert accounts == record["accounts"], (
            f"FIXTURE DRIFT on {signature}: the recorded accounts no longer match the "
            "chain. Investigate before regenerating."
        )

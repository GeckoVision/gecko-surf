"""Paying Orthogonal per call over x402 from the settlement wallet, offline.

Founder ruling, 9 Oct. Every seam is a fake: the x402 host (answers 402 with a v2
``payment-required`` header shaped like the one measured on x402.orthogonal.com on 9 Oct,
then checks the retry the way the reference facilitator does), the RPC (mainnet-SHAPED,
synthesized), and Privy (the REAL :class:`gecko.privy_backend.PrivyBackend` over a fake
transport that signs with a throwaway key whose pubkey IS the settlement wallet). The
bytes are real (solders). This proves what the payer does GIVEN such answers; the live
check is a founder-run call, never part of this suite.
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass, field, replace
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("solders")

from solders.message import to_bytes_versioned  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from gecko.anyquery.config import ConfigError, load_config  # noqa: E402
from gecko.anyquery.payment_tx import usdc_ata  # noqa: E402
from gecko.anyquery.upstream_spend import MongoSpendLedger  # noqa: E402
from gecko.anyquery.upstream_x402 import (  # noqa: E402
    HttpReply,
    HttpRequest,
    OrthogonalX402,
)
from gecko.anyquery.x402_policy import (  # noqa: E402
    X402Expectation,
    check_x402_payment,
    x402_pay_policy_from_config,
)
from gecko.autonomous_purchase import USDC_MINT  # noqa: E402
from gecko.landing import TOKEN_PROGRAM_ID  # noqa: E402
from gecko.orthogonal.api import OrthogonalApi  # noqa: E402
from gecko.orthogonal.errors import (  # noqa: E402
    NotAllowedError,
    UpstreamError,
    X402RefusedError,
    charge_possible,
)
from gecko.orthogonal.extract import X402_RECEIPT_KEY  # noqa: E402
from gecko.orthogonal.paginate import PageRead, fetch_pages  # noqa: E402
from gecko.orthogonal.spec import COMMENTS, PL_COMPANY, RUN_OP, Target  # noqa: E402
from gecko.orthogonal.spend import SpendGuard  # noqa: E402
from gecko.privy_backend import PrivyBackend, PrivyRequest  # noqa: E402
from gecko.servicemap.pins import op_for_call, pay_to_resolver  # noqa: E402
from gecko.servicemap.registry import MemoryRegistry  # noqa: E402
from gecko.servicemap.types import Health, OfferObs  # noqa: E402
from gecko.x402_svm import (  # noqa: E402
    COMPUTE_BUDGET_PROGRAM,
    MEMO_PROGRAM,
    SOLANA_MAINNET,
    build_exact_payment,
)
from tests.anyquery_fake_mongo import FakeMongoClient, FakeUnavailableError  # noqa: E402
from tests.test_anyquery_solana_billing import (  # noqa: E402
    CONFIG,
    SETTLEMENT,
    SLOT,
    STRANGER,
    _key,
    _row,
)

WALLET_KEY = _key(40)
assert str(WALLET_KEY.pubkey()) == SETTLEMENT
FACILITATOR = str(_key(77).pubkey())
#: Measured 9 Oct: scrapecreators' payTo on Orthogonal's x402 host; fiber's differs.
PAY_TO = "4Bf6zpkrv4gtirAzM3kzqXrRRLXNmbKrpHLebTpAk3Wg"
FIBER_PAY_TO = "DHZJ3PpyVtaomjEiHVwzL982prM2LeENNuHiFMxJy4NC"
BLOCKHASH = "EETubP5AKHgjPAhzPAFcb8BAY1hMH639CWCFTqi3hq1k"
PRICE = 20_000  # 0.02 USDC: the comments endpoint's 2-cent catalog quote
SETTLED_SIG = "4" + "X" * 86
X402_CONFIG = replace(CONFIG, orthogonal_pay="x402")
POLICY = x402_pay_policy_from_config(X402_CONFIG)
PINNED_POLICY = POLICY.pinned_to(PAY_TO)
PROBED_AT = "2026-10-09T12:00:00+00:00"
COMMENTS_OP = op_for_call("GET", "scrapecreators", "/v2/instagram/post/comments")


def _probed(op: str = COMMENTS_OP, pay_to: str = PAY_TO) -> MemoryRegistry:
    """A registry holding what a tier 1 probe of ``op`` would have written."""
    registry = MemoryRegistry()
    registry.put_offer(
        OfferObs(
            offer_id=f"{op}:{pay_to}",
            op_id=op,
            scheme="exact",
            network=SOLANA_MAINNET,
            asset=USDC_MINT,
            amount=PRICE,
            pay_to=pay_to,
            max_timeout_seconds=60,
            fee_payer=None,
            evidence="EXTRACTED",
            first_seen=PROBED_AT,
            last_seen=PROBED_AT,
        )
    )
    return registry


POST_URL = "https://www.instagram.com/p/ABC123/"
BODY = {
    "api": "scrapecreators",
    "path": "/v2/instagram/post/comments",
    "query": {"url": POST_URL},
}


def _offer(**over: Any) -> dict[str, Any]:
    offer = {
        "scheme": "exact",
        "network": SOLANA_MAINNET,
        "asset": USDC_MINT,
        "amount": str(PRICE),
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 60,
        "extra": {"feePayer": FACILITATOR},
    }
    offer.update(over)
    return offer


def _challenge(*offers: dict[str, Any], version: int = 2) -> str:
    """A v2 challenge with the Base and Monad offers the live host also lists."""
    body = {
        "x402Version": version,
        "resource": {"url": "https://x402.orthogonal.com/scrapecreators/v2/x"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "amount": "20000",
                "payTo": "0x0000000000000000000000000000000000000001",
                "maxTimeoutSeconds": 60,
                "extra": {},
            },
            *(offers or (_offer(),)),
            {
                "scheme": "exact",
                "network": "eip155:143",
                "asset": "0x0000000000000000000000000000000000000002",
                "amount": "20000",
                "payTo": "0x0000000000000000000000000000000000000003",
                "maxTimeoutSeconds": 60,
            },
        ],
    }
    return base64.b64encode(json.dumps(body).encode()).decode()


def _b64json(value: str) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(base64.b64decode(value))
    return decoded


@dataclass
class FakeHost:
    """x402.orthogonal.com: 402 without payment, the data with a valid one."""

    challenge: str = field(default_factory=_challenge)
    paid_status: int = 200
    unpaid_status: int = 402
    settle_answer: bool = True
    requests: list[HttpRequest] = field(default_factory=list)

    def __call__(self, req: HttpRequest) -> HttpReply:
        self.requests.append(req)
        paid = next(
            (v for k, v in req.headers.items() if k.lower() == "payment-signature"),
            None,
        )
        if paid is None:
            if self.unpaid_status != 402:
                return HttpReply(self.unpaid_status, {}, {"comments": []})
            return HttpReply(402, {"payment-required": self.challenge}, {})
        headers: dict[str, str] = {}
        if self.settle_answer:
            answer = {
                "success": True,
                "transaction": SETTLED_SIG,
                "network": SOLANA_MAINNET,
                "payer": SETTLEMENT,
            }
            headers["payment-response"] = base64.b64encode(
                json.dumps(answer).encode()
            ).decode()
        body = {"comments": [{"user": {"username": "a"}}], "cursor": None}
        return HttpReply(self.paid_status, headers, body)

    def paid_payload(self) -> dict[str, Any]:
        [paid] = [
            r
            for r in self.requests
            if any(k.lower() == "payment-signature" for k in r.headers)
        ]
        return _b64json(paid.headers["PAYMENT-SIGNATURE"])


@dataclass
class FakeRpc:
    """A mainnet-shaped node: the wallet's USDC goes to payTo's account in simulation."""

    sim_err: Any = None
    calls: list[str] = field(default_factory=list)

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.calls.append(method)
        if method == "getLatestBlockhash":
            return {
                "result": {
                    "value": {"blockhash": BLOCKHASH, "lastValidBlockHeight": 900}
                }
            }
        if method == "getSlot":
            return {"result": SLOT + 2}
        if method == "getAccountInfo":
            return {
                "result": {"value": {"lamports": 3_000_000, "data": ["", "base64"]}}
            }
        if method == "simulateTransaction":
            amount = _transfer_amount(params[0])
            value = {
                "err": self.sim_err,
                "logs": ["Program log: Instruction: TransferChecked"],
                "unitsConsumed": 6_200,
                "accounts": [{"lamports": 3_000_000, "data": ["", "base64"]}],
                "innerInstructions": [],
                "preTokenBalances": [
                    _row(2, SETTLEMENT, USDC_MINT, 1_000_000),
                    _row(3, PAY_TO, USDC_MINT, 0),
                ],
                "postTokenBalances": [
                    _row(2, SETTLEMENT, USDC_MINT, 1_000_000 - amount),
                    _row(3, PAY_TO, USDC_MINT, amount),
                ],
            }
            return {"result": {"context": {"slot": SLOT}, "value": value}}
        raise AssertionError(f"unscripted RPC method {method}")


def _transfer_amount(tx_b64: str) -> int:
    message = VersionedTransaction.from_bytes(base64.b64decode(tx_b64)).message
    keys = [str(k) for k in message.account_keys]
    for ix in message.instructions:
        data = bytes(ix.data)
        if keys[ix.program_id_index] == TOKEN_PROGRAM_ID and data[:1] == b"\x0c":
            return int(struct.unpack_from("<Q", data, 1)[0])
    return 0


@dataclass
class FakePrivy:
    """Privy's wallet GET and ``signTransaction``: signs slot 1 with the throwaway key."""

    swap_message: bool = False
    requests: list[PrivyRequest] = field(default_factory=list)

    def __call__(self, request: PrivyRequest, headers: Mapping[str, str]) -> dict:
        self.requests.append(request)
        if request.method == "GET":
            return {"address": SETTLEMENT, "chain_type": "solana"}
        assert request.body is not None
        assert request.body["method"] == "signTransaction"
        raw = base64.b64decode(request.body["params"]["transaction"])
        tx = VersionedTransaction.from_bytes(raw)
        message = tx.message
        if self.swap_message:
            # The same payment, to a stranger: a substitution the merge must refuse.
            offer = _parsed_offer()
            message = VersionedTransaction.from_bytes(
                build_exact_payment(
                    offer,
                    payer=SETTLEMENT,
                    source=usdc_ata(SETTLEMENT),
                    destination=usdc_ata(STRANGER),
                    decimals=6,
                    token_program=TOKEN_PROGRAM_ID,
                    blockhash=BLOCKHASH,
                    nonce=b"\x00" * 16,
                )
            ).message
        ours = WALLET_KEY.sign_message(to_bytes_versioned(message))
        signed = VersionedTransaction.populate(message, [Signature.default(), ours])
        return {
            "method": "signTransaction",
            "data": {
                "signed_transaction": base64.b64encode(bytes(signed)).decode(),
                "encoding": "base64",
            },
        }

    @property
    def signs(self) -> int:
        return sum(1 for r in self.requests if r.method == "POST")


def _parsed_offer() -> Any:
    from gecko.x402_svm import decode_payment_required, solana_exact_offers

    required = decode_payment_required(_challenge())
    [offer] = solana_exact_offers(required, network=SOLANA_MAINNET, asset=USDC_MINT)
    return offer


@dataclass
class Rig:
    payer: OrthogonalX402
    host: FakeHost
    rpc: FakeRpc
    privy: FakePrivy


def _rig(
    host: FakeHost | None = None,
    privy: FakePrivy | None = None,
    rpc: FakeRpc | None = None,
    **over: Any,
) -> Rig:
    host = host or FakeHost()
    privy = privy or FakePrivy()
    rpc = rpc or FakeRpc()
    backend = PrivyBackend(
        wallet_id="testwallet00000000000002",
        app_id="app-public-id",
        app_secret="privy-secret-never-printed",
        network="mainnet",
        transport=privy,
    )
    payer = OrthogonalX402(
        policy=over.pop("policy", POLICY),
        network=over.pop("network", "mainnet"),
        rpc_url="https://rpc.example",
        backend=lambda: backend,
        http=host,
        rpc_call=rpc,
        nonce=lambda: bytes(range(16)),
        pay_to_for=over.pop("pay_to_for", pay_to_resolver(_probed())),
        **over,
    )
    return Rig(payer, host, rpc, privy)


# --------------------------------------------------------------------------------------
# The paid call
# --------------------------------------------------------------------------------------


def test_one_call_is_challenged_paid_from_the_settlement_wallet_and_returned() -> None:
    rig = _rig()

    reply = rig.payer.call(COMMENTS, dict(BODY))

    first, paid = rig.host.requests
    assert first.method == "GET" and not any(
        k.lower() == "payment-signature" for k in first.headers
    )
    url = urlsplit(first.url)
    assert (url.scheme, url.netloc, url.path) == (
        "https",
        "x402.orthogonal.com",
        "/scrapecreators/v2/instagram/post/comments",
    )
    assert parse_qs(url.query) == {"url": [POST_URL]}
    assert paid.url == first.url and paid.method == "GET"
    # The key path's envelope: the data, the price in cents, and Gecko's receipt.
    assert reply["data"]["comments"][0]["user"]["username"] == "a"
    assert reply["priceCents"] == 2 and reply["quotedPriceCents"] == 2
    assert reply[X402_RECEIPT_KEY] == {
        "signature": SETTLED_SIG,
        "amount_usdc_micro": PRICE,
        "pay_to": PAY_TO,
    }
    [receipt] = rig.payer.receipts
    assert receipt.endpoint == COMMENTS.label and receipt.settled
    assert rig.privy.signs == 1


def test_the_payment_is_the_reference_exact_shape_signed_in_slot_one_only() -> None:
    rig = _rig()
    rig.payer.call(COMMENTS, dict(BODY))

    payload = rig.host.paid_payload()
    assert payload["x402Version"] == 2
    # The server's own offer and resource, echoed back unchanged.
    assert payload["accepted"] == _offer()
    assert payload["resource"] == {
        "url": "https://x402.orthogonal.com/scrapecreators/v2/x"
    }
    tx = VersionedTransaction.from_bytes(
        base64.b64decode(payload["payload"]["transaction"])
    )
    keys = [str(k) for k in tx.message.account_keys]
    assert keys[0] == FACILITATOR and keys[1] == SETTLEMENT
    # Slot 0 is the facilitator's, left empty for it; slot 1 is ours, and it verifies.
    assert tx.verify_with_results() == [False, True]
    assert str(tx.message.recent_blockhash) == BLOCKHASH
    ixs = tx.message.instructions
    programs = [keys[ix.program_id_index] for ix in ixs]
    assert programs == [
        COMPUTE_BUDGET_PROGRAM,
        COMPUTE_BUDGET_PROGRAM,
        TOKEN_PROGRAM_ID,
        MEMO_PROGRAM,
    ]
    assert bytes(ixs[0].data) == b"\x02" + struct.pack("<I", 20_000)
    assert bytes(ixs[1].data) == b"\x03" + struct.pack("<Q", 1)
    assert bytes(ixs[2].data) == b"\x0c" + struct.pack("<QB", PRICE, 6)
    source, mint, destination, owner = (keys[i] for i in bytes(ixs[2].accounts))
    assert (source, mint, destination, owner) == (
        usdc_ata(SETTLEMENT),
        USDC_MINT,
        usdc_ata(PAY_TO),
        SETTLEMENT,
    )
    assert bytes(ixs[3].data) == bytes(range(16)).hex().encode()


def test_a_server_memo_is_written_verbatim() -> None:
    offer = _offer(extra={"feePayer": FACILITATOR, "memo": "orthogonal-req-42"})
    rig = _rig(host=FakeHost(challenge=_challenge(offer)))
    rig.payer.call(COMMENTS, dict(BODY))

    tx = VersionedTransaction.from_bytes(
        base64.b64decode(rig.host.paid_payload()["payload"]["transaction"])
    )
    assert bytes(tx.message.instructions[3].data) == b"orthogonal-req-42"


def test_the_simulation_tracks_the_wallet_and_keeps_the_real_blockhash() -> None:
    rig = _rig()
    rig.payer.call(COMMENTS, dict(BODY))

    assert rig.rpc.calls == [
        "getLatestBlockhash",
        "getAccountInfo",
        "simulateTransaction",
        "getSlot",
    ]


# --------------------------------------------------------------------------------------
# Refused before anything is signed: nothing paid, the hold is released
# --------------------------------------------------------------------------------------


def _refused(rig: Rig, target: Target = COMMENTS, body: Any = None) -> X402RefusedError:
    with pytest.raises(X402RefusedError) as caught:
        rig.payer.call(target, dict(body or BODY))
    assert rig.privy.signs == 0, "nothing reaches Privy"
    assert len(rig.host.requests) == 1, "no paid request is sent"
    assert rig.payer.receipts == []
    assert not charge_possible(caught.value)
    return caught.value


def test_a_pay_to_that_is_not_the_pinned_one_is_refused() -> None:
    rig = _rig(host=FakeHost(challenge=_challenge(_offer(payTo=STRANGER))))
    assert "payTo" in _refused(rig).message


def test_each_operation_is_paid_to_its_own_probed_pay_to() -> None:
    """Live, 9 Oct: fiber's payTo is not scrapecreators', and one pin refused it. The
    pin is now the registry's, per operation, and the gate binds to it."""
    offer = _offer(payTo=FIBER_PAY_TO)
    rig = _rig(
        host=FakeHost(challenge=_challenge(offer)),
        pay_to_for=pay_to_resolver(_probed(pay_to=FIBER_PAY_TO)),
    )
    reply = rig.payer.call(COMMENTS, dict(BODY))
    assert reply[X402_RECEIPT_KEY]["pay_to"] == FIBER_PAY_TO
    tx = VersionedTransaction.from_bytes(
        base64.b64decode(rig.host.paid_payload()["payload"]["transaction"])
    )
    keys = [str(k) for k in tx.message.account_keys]
    transfer = tx.message.instructions[2]
    assert keys[bytes(transfer.accounts)[2]] == usdc_ata(FIBER_PAY_TO)


def test_an_operation_the_registry_never_probed_is_refused() -> None:
    rig = _rig(pay_to_for=pay_to_resolver(MemoryRegistry()))
    assert "no probed offer" in _refused(rig).message


def test_a_quarantined_operation_is_refused_even_when_the_challenge_matches() -> None:
    registry = _probed()
    registry.put_health(
        Health(COMMENTS_OP, "quarantined", 1, PROBED_AT, None, 402, 0, PROBED_AT)
    )
    rig = _rig(pay_to_for=pay_to_resolver(registry))
    assert "quarantined" in _refused(rig).message


def test_the_probed_pin_of_another_operation_is_not_this_ones() -> None:
    other = op_for_call("POST", "fiber", "/v1/people-search")
    rig = _rig(pay_to_for=pay_to_resolver(_probed(op=other)))
    assert "no probed offer" in _refused(rig).message


def test_without_a_registry_or_an_override_nothing_is_paid() -> None:
    rig = _rig(pay_to_for=None)
    assert "no payTo can be pinned" in _refused(rig).message


def test_the_env_override_replaces_the_registry_pin() -> None:
    rig = _rig(
        policy=POLICY.pinned_to(FIBER_PAY_TO),
        host=FakeHost(challenge=_challenge(_offer(payTo=FIBER_PAY_TO))),
        pay_to_for=pay_to_resolver(_probed(pay_to=PAY_TO)),
    )
    reply = rig.payer.call(COMMENTS, dict(BODY))
    assert reply[X402_RECEIPT_KEY]["pay_to"] == FIBER_PAY_TO


def test_the_gate_refuses_a_payment_with_no_pinned_pay_to() -> None:
    unpinned = replace(EXPECT, policy=POLICY)
    verdict = check_x402_payment(_payment(), unpinned)
    assert verdict is not None and verdict.code == "destination-not-allowlisted"


def test_an_amount_above_the_catalog_quote_is_refused() -> None:
    rig = _rig(host=FakeHost(challenge=_challenge(_offer(amount="20001"))))
    assert "quoted" in _refused(rig).message


def test_an_amount_above_the_per_call_cap_is_refused() -> None:
    rig = _rig(policy=replace(POLICY, max_call_usdc_micro=PRICE - 1))
    assert "per-call cap" in _refused(rig).message


def test_an_offer_without_the_facilitator_fee_payer_is_refused() -> None:
    rig = _rig(host=FakeHost(challenge=_challenge(_offer(extra={}))))
    assert "fee payer" in _refused(rig).message


@pytest.mark.parametrize(
    "offer",
    [
        _offer(asset="Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"),  # USDT
        _offer(network="solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"),  # devnet
        _offer(scheme="upto"),
    ],
)
def test_only_the_solana_mainnet_usdc_exact_offer_is_ever_taken(
    offer: dict[str, Any],
) -> None:
    rig = _rig(host=FakeHost(challenge=_challenge(offer)))
    assert "0 Solana USDC" in _refused(rig).message


def test_two_solana_offers_are_ambiguous_and_refused() -> None:
    rig = _rig(host=FakeHost(challenge=_challenge(_offer(), _offer(amount="10000"))))
    assert "2 Solana USDC" in _refused(rig).message


def test_a_challenge_that_is_not_x402_v2_is_refused() -> None:
    rig = _rig(host=FakeHost(challenge=_challenge(version=1)))
    assert "version 2" in _refused(rig).message


def test_a_failing_simulation_is_refused_before_signing() -> None:
    rig = _rig(rpc=FakeRpc(sim_err={"InstructionError": [2, {"Custom": 1}]}))
    assert "simulation" in _refused(rig).message


def test_a_substituted_message_from_privy_is_refused_and_nothing_is_sent() -> None:
    rig = _rig(privy=FakePrivy(swap_message=True))
    with pytest.raises(X402RefusedError):
        rig.payer.call(COMMENTS, dict(BODY))
    assert len(rig.host.requests) == 1 and rig.payer.receipts == []


def test_an_input_the_target_does_not_name_is_refused() -> None:
    body = {**BODY, "query": {"url": POST_URL, "limit": "999"}}
    rig = _rig()
    with pytest.raises(X402RefusedError):
        rig.payer.call(COMMENTS, body)
    assert rig.host.requests == []


def test_only_mainnet_is_paid() -> None:
    rig = _rig(network="devnet")
    with pytest.raises(X402RefusedError):
        rig.payer.call(COMMENTS, dict(BODY))
    assert rig.host.requests == []


def test_an_unpaid_failure_is_not_a_charge() -> None:
    rig = _rig(host=FakeHost(unpaid_status=503))
    with pytest.raises(UpstreamError) as caught:
        rig.payer.call(COMMENTS, dict(BODY))
    assert caught.value.status == 503 and not charge_possible(caught.value)


# --------------------------------------------------------------------------------------
# After the paid request: it may have been charged
# --------------------------------------------------------------------------------------


def test_a_paid_call_that_fails_may_have_been_charged_and_keeps_its_receipt() -> None:
    rig = _rig(host=FakeHost(paid_status=500))
    with pytest.raises(UpstreamError) as caught:
        rig.payer.call(COMMENTS, dict(BODY))
    assert charge_possible(caught.value) and caught.value.status == 500
    [receipt] = rig.payer.receipts
    assert receipt.amount_usdc_micro == PRICE and receipt.signature == SETTLED_SIG


def test_a_paid_call_with_no_settlement_answer_has_no_signature_but_a_receipt() -> None:
    rig = _rig(host=FakeHost(settle_answer=False))
    reply = rig.payer.call(COMMENTS, dict(BODY))
    assert reply[X402_RECEIPT_KEY]["signature"] is None
    [receipt] = rig.payer.receipts
    assert not receipt.settled and receipt.amount_usdc_micro == PRICE


def _spend_ledger() -> MongoSpendLedger:
    return MongoSpendLedger(FakeMongoClient(), unavailable=(FakeUnavailableError,))


def _read(payload: Any) -> PageRead:
    data = payload.get("data") if isinstance(payload, dict) else None
    return PageRead(items=tuple((data or {}).get("comments") or ()))


def _fetch(rig: Rig, ledger: MongoSpendLedger) -> Any:
    api = OrthogonalApi(transport=_no_wire, session=_NoKey(), x402=rig.payer)
    return fetch_pages(
        api,
        lambda _cursor, _parent: dict(BODY),
        _read,
        SpendGuard(ledger, per_run_cap=100, total_cap=100),
        2,
        endpoint=COMMENTS.label,
    )


def _no_wire(_req: Any) -> tuple[int, Any]:
    raise AssertionError("the key transport is never used in x402 mode")


class _NoKey:
    def auth_headers(self) -> dict[str, str]:
        raise AssertionError("the key is never read in x402 mode")


def test_through_the_runner_loop_a_paid_call_is_counted_and_receipted() -> None:
    ledger = _spend_ledger()
    result = _fetch(_rig(), ledger)

    [line] = result.calls
    assert (line.charged_cents, line.payment_signature, line.paid_usdc_micro) == (
        2,
        SETTLED_SIG,
        PRICE,
    )
    assert ledger.total_cents() == 2
    # No requestId on the x402 host: the loop stops after page 1, never pays page 2.
    assert result.stop_reason == "no_request_id"


def test_a_refused_payment_releases_its_hold() -> None:
    ledger = _spend_ledger()
    rig = _rig(host=FakeHost(challenge=_challenge(_offer(payTo=STRANGER))))
    with pytest.raises(X402RefusedError) as caught:
        _fetch(rig, ledger)
    assert ledger.total_cents() == 0 and caught.value.counted_cents == 0


def test_a_paid_failure_keeps_its_hold_at_the_quote() -> None:
    ledger = _spend_ledger()
    rig = _rig(host=FakeHost(paid_status=502))
    with pytest.raises(UpstreamError) as caught:
        _fetch(rig, ledger)
    assert ledger.total_cents() == 2 and caught.value.counted_cents == 2


def test_the_allowlist_is_checked_before_anything_is_paid() -> None:
    rig = _rig()
    api = OrthogonalApi(transport=_no_wire, session=_NoKey(), x402=rig.payer)
    with pytest.raises(NotAllowedError):
        api.call(RUN_OP, {"api": "resend", "path": "/emails", "body": {"to": "x"}})
    assert rig.host.requests == []


def test_a_receipt_planted_in_a_key_path_reply_is_stripped() -> None:
    planted = {
        "success": True,
        "priceCents": 4,
        "data": {"x": 1},
        X402_RECEIPT_KEY: {"signature": "fake", "amount_usdc_micro": 1},
    }

    class _Key:
        def auth_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer orth_test_x"}

    api = OrthogonalApi(transport=lambda _req: (200, planted), session=_Key())
    reply = api.call(RUN_OP, {"api": PL_COMPANY.api, "path": "/v3/companies/a.com"})
    assert X402_RECEIPT_KEY not in reply and reply["data"] == {"x": 1}


# --------------------------------------------------------------------------------------
# The rehearsal: everything but the signature
# --------------------------------------------------------------------------------------


def test_a_rehearsal_reads_checks_builds_and_simulates_but_never_signs() -> None:
    rig = _rig()

    seen = rig.payer.rehearse(COMMENTS, dict(BODY))

    assert seen.simulation == "pass" and seen.gate is None
    assert (seen.amount_usdc_micro, seen.pay_to, seen.fee_payer) == (
        PRICE,
        PAY_TO,
        FACILITATOR,
    )
    assert rig.privy.requests == [] and len(rig.host.requests) == 1
    assert "simulateTransaction" in rig.rpc.calls
    assert "sendTransaction" not in rig.rpc.calls


# --------------------------------------------------------------------------------------
# The local gate over the bytes
# --------------------------------------------------------------------------------------


def _payment(**kw: Any) -> bytes:
    offer = _parsed_offer()
    args: dict[str, Any] = {
        "payer": SETTLEMENT,
        "source": usdc_ata(SETTLEMENT),
        "destination": usdc_ata(PAY_TO),
        "decimals": 6,
        "token_program": TOKEN_PROGRAM_ID,
        "blockhash": BLOCKHASH,
        "nonce": b"\x01" * 16,
    }
    args.update(kw)
    if "amount" in kw:
        offer = replace(offer, amount=args.pop("amount"))
    if "fee_payer" in kw:
        offer = replace(offer, fee_payer=args.pop("fee_payer"))
    return build_exact_payment(offer, **args)


EXPECT = X402Expectation(
    policy=PINNED_POLICY, amount_usdc_micro=PRICE, fee_payer=FACILITATOR
)


def test_the_gate_lets_exactly_the_expected_payment_through() -> None:
    assert check_x402_payment(_payment(), EXPECT) is None


@pytest.mark.parametrize(
    ("bytes_kw", "expect", "code"),
    [
        ({"destination": usdc_ata(STRANGER)}, EXPECT, "destination-not-allowlisted"),
        ({"amount": PRICE + 1}, EXPECT, "over-per-transaction-cap"),
        ({"source": usdc_ata(STRANGER)}, EXPECT, "instruction-not-allowlisted"),
        ({"fee_payer": STRANGER}, EXPECT, "instruction-not-allowlisted"),
        (
            {},
            replace(EXPECT, amount_usdc_micro=POLICY.max_call_usdc_micro + 1),
            "over-per-transaction-cap",
        ),
    ],
)
def test_the_gate_refuses_anything_else(
    bytes_kw: dict[str, Any], expect: X402Expectation, code: str
) -> None:
    verdict = check_x402_payment(_payment(**bytes_kw), expect)
    assert verdict is not None and not verdict.authorized and verdict.code == code


def test_the_gate_refuses_a_payment_the_wallet_pays_the_fee_for() -> None:
    verdict = check_x402_payment(
        _payment(fee_payer=STRANGER), replace(EXPECT, fee_payer=STRANGER)
    )
    assert verdict is None  # a stranger facilitator named by the challenge is fine
    with pytest.raises(Exception, match="fee payer"):
        _payment(fee_payer=SETTLEMENT)


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------

_ENV = {
    "GECKO_SETTLEMENT_WALLET_ID": "testwallet00000000000002",
    "GECKO_SETTLEMENT_WALLET": "FqQtW9mNLSbicvbhgdZHj7vpdxniJKLXsgmvDm3F46U4",
}


def test_the_key_path_is_the_default_and_no_single_pay_to_is_pinned() -> None:
    config = load_config({})
    assert config.orthogonal_pay == "key"
    assert config.orthogonal_x402_pay_to is None  # per operation, from the registry
    assert config.max_x402_call_usdc_micro == 100_000
    assert config.settlement_float_usdc_micro == 1_000_000


def test_x402_mode_reads_its_settings() -> None:
    config = load_config(
        {
            **_ENV,
            "GECKO_ORTHOGONAL_PAY": "x402",
            "GECKO_ORTHOGONAL_X402_MAX_CALL_USDC_MICRO": "50000",
            "GECKO_SETTLEMENT_FLOAT_USDC_MICRO": "2000000",
        }
    )
    assert config.orthogonal_pay == "x402"
    assert config.max_x402_call_usdc_micro == 50_000
    assert config.settlement_float_usdc_micro == 2_000_000


@pytest.mark.parametrize(
    ("env", "said"),
    [
        ({"GECKO_ORTHOGONAL_PAY": "x402"}, "settlement wallet"),
        ({"GECKO_ORTHOGONAL_PAY": "card"}, "GECKO_ORTHOGONAL_PAY"),
        ({"GECKO_ORTHOGONAL_X402_PAYTO": "not-an-address"}, "PAYTO"),
        (
            {**_ENV, "GECKO_ORTHOGONAL_X402_PAYTO": _ENV["GECKO_SETTLEMENT_WALLET"]},
            "PAYTO",
        ),
        ({"GECKO_ORTHOGONAL_X402_MAX_CALL_USDC_MICRO": "0"}, "MAX_CALL"),
        ({"GECKO_SETTLEMENT_FLOAT_USDC_MICRO": "-1"}, "FLOAT"),
    ],
)
def test_a_wrong_x402_setting_is_refused_by_name(
    env: dict[str, str], said: str
) -> None:
    with pytest.raises(ConfigError, match=said):
        load_config(env)


def test_bytes_the_gate_would_refuse_never_reach_privy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Privy rule set allows the SPL Token program program-wide on this wallet, so
    Privy alone would sign a transfer to anyone: our gate is the pin that binds."""
    import gecko.anyquery.upstream_x402 as module

    real = module.build_exact_payment

    def hijacked(offer: Any, **kw: Any) -> bytes:
        return real(offer, **{**kw, "destination": usdc_ata(STRANGER)})

    monkeypatch.setattr(module, "build_exact_payment", hijacked)
    rig = _rig()
    with pytest.raises(X402RefusedError, match="destination-not-allowlisted"):
        rig.payer.call(COMMENTS, dict(BODY))
    assert rig.privy.signs == 0 and len(rig.host.requests) == 1


# --------------------------------------------------------------------------------------
# Privy in the authority role (the facilitator pays the fee)
# --------------------------------------------------------------------------------------


def _attestation(**kw: Any) -> Any:
    from gecko.signer import AUTHORITY_ROLE, SigningAttestation

    base: dict[str, Any] = {
        "binding": "b",
        "strength": "exact",
        "network": "mainnet",
        "fee_payer": FACILITATOR,
        "receipt_slot": SLOT,
        "current_slot": SLOT,
        "units_consumed": 1,
        "profile": "external",
        "signing_as": AUTHORITY_ROLE,
        "required_signers": (FACILITATOR, SETTLEMENT),
    }
    base.update(kw)
    return SigningAttestation(**base)


def _backend(privy: Any) -> PrivyBackend:
    return PrivyBackend(
        wallet_id="testwallet00000000000002",
        app_id="app",
        app_secret="secret",
        network="mainnet",
        transport=privy,
    )


def test_privy_signs_as_the_authority_into_its_own_slot() -> None:
    from gecko.privy_backend import signature_at

    signed = _backend(FakePrivy()).sign_transaction(_payment(), _attestation())
    assert signature_at(signed, 0) == bytes(64)
    assert signature_at(signed, 1) != bytes(64)


@pytest.mark.parametrize(
    ("kw", "said"),
    [
        ({"fee_payer": SETTLEMENT}, "fee payer"),
        ({"required_signers": (FACILITATOR, STRANGER)}, "not a required signer"),
    ],
)
def test_privy_refuses_an_authority_role_the_bytes_do_not_support(
    kw: dict[str, Any], said: str
) -> None:
    from gecko.privy_backend import PrivyBackendError

    privy = FakePrivy()
    with pytest.raises(PrivyBackendError, match=said):
        _backend(privy).sign_transaction(_payment(), _attestation(**kw))
    assert privy.signs == 0


def test_privy_answering_with_its_slot_empty_is_refused() -> None:
    from gecko.privy_backend import PrivyBackendError

    def echo(request: PrivyRequest, headers: Mapping[str, str]) -> dict:
        if request.method == "GET":
            return {"address": SETTLEMENT, "chain_type": "solana"}
        assert request.body is not None
        return {"data": {"signed_transaction": request.body["params"]["transaction"]}}

    with pytest.raises(PrivyBackendError, match="EMPTY"):
        _backend(echo).sign_transaction(_payment(), _attestation())

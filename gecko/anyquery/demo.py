"""The any-query story end to end, in recorded mode: $0, offline, every lane's real code.

Onboard (a Privy wallet bound at creation) -> plan a pasted query (the model planner on a
recorded reply, BM25 over the committed catalog, ``validate_plan``) -> run it (``execute``
on the Orthogonal core, replies replayed) -> pay for the run in SOL. That is TWO
transactions (option B, founder ruling 9 Oct): the user's wallet sends one plain SOL
transfer to Gecko's settlement wallet (``SolanaBilling``, real bytes, final when it
lands); then Gecko's settlement wallet wraps it, swaps it to USDC on the one Whirlpool and
forwards the USDC to the cold treasury (``Settlement``), sent through the Helius sender
with the Kora relay's fee payer as rebate address -> observe the rebate in the
SETTLEMENT's slot, credit the user's share -> run again a week later at a discounted
quote -> ``run_changes``.

Only the edge is recorded (:mod:`gecko.anyquery.recorded`). Both signers are injected,
because the stand-ins for Privy's enclave sign with throwaway keys and key material never
lives in ``gecko/``; ``scripts/any_query_demo.py`` supplies them.

Every source's values are stored for the customer who asked (founder ruling, 8 Oct), so
the demo needs no storage override: a source's ``storage`` label is metadata only.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..access import public_session
from ..credentials import ChainResolver, KeyringBackend
from ..orthogonal.api import OrthogonalApi
from ..orthogonal.job import JobDeps
from ..orthogonal.spend import MemoryLedger
from ..send_endpoint import HeliusSend, PublicSend
from ..signer import SigningBackend
from ..wallet_binding import WalletBinding
from .catalog_load import LoadedCatalog, load_catalog
from .config import AnyQueryConfig
from .enrol import InMemoryWalletBinder, WalletEnrolment
from .execute import execute
from .model_planner import ModelPlanner
from .payment_tx import usdc_ata
from .recorded import (
    RecordedChain,
    RecordedHeliusPost,
    RecordedKeyring,
    RecordedOrthogonal,
    fixed_rate_sol_pricer,
    fixed_rate_swap_planner,
    recorded_model_call,
    recorded_wallet_creator,
)
from .retrieve import bm25_candidates
from .run import RunDeps
from .service import AnyQueryDeps, call_anyquery_tool
from .rebates import MemoryRebateIndex
from .settlement import Settlement
from .solana_billing import SolanaBilling
from .store import MemoryStore
from .validate import validator_for

__all__ = [
    "DemoClock",
    "DemoFixtures",
    "RecordedRig",
    "build_rig",
    "demo_address",
    "load_fixtures",
    "run_demo",
]

_RPC_URL = "https://recorded.invalid"
_RATE_MICRO = 150_000_000  # 150 USDC per SOL at the recorded pool
_REBATE_LAMPORTS = 400_000  # what the first settlement's slot shows the relay gain
_SETTLEMENT_WALLET_ID = "recordedsettlement1"
_SPLIT_USER_PCT = 50
_WEEK_S = 7 * 24 * 3600


def demo_address(label: str) -> str:
    """A stable, valid, off-curve address for a demo role. Nobody holds its key."""
    from solders.pubkey import Pubkey

    seed = label.encode()[:32]
    program = Pubkey.from_string("11111111111111111111111111111111")
    return str(Pubkey.find_program_address([b"gecko-demo", seed], program)[0])


@dataclass
class DemoClock:
    """One clock for the service (ISO), the billing (ISO) and the core (epoch)."""

    moment: dt.datetime = dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.UTC)

    def iso(self) -> str:
        return self.moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def epoch(self) -> float:
        return self.moment.timestamp()

    def advance(self, seconds: int) -> None:
        self.moment += dt.timedelta(seconds=seconds)


@dataclass(frozen=True)
class DemoFixtures:
    details: list[dict[str, Any]]
    ledgers: tuple[Path, ...]
    planner_reply: str
    query: str
    subjects: tuple[str, ...]
    upstream_runs: list[list[dict[str, Any]]]
    swap_idl: dict[str, Any]


def load_fixtures(folder: Path, query_id: str = "company-signals") -> DemoFixtures:
    """The committed fixtures in ``tests/fixtures/anyquery``. Reads files only."""

    def read(name: str) -> Any:
        return json.loads((folder / name).read_text(encoding="utf-8"))

    golden = {q["id"]: q for q in read("golden_queries.json")["queries"]}[query_id]
    return DemoFixtures(
        details=read("catalog_details_fixture.json")["records"],
        ledgers=(folder / "observed_fields.jsonl",),
        planner_reply=read("planner_replies.json")["replies"][query_id],
        query=golden["query"],
        subjects=tuple(golden["subjects"]),
        upstream_runs=read("demo_upstream_replies.json")["runs"],
        swap_idl=read("whirlpool_swap_v2_idl.json"),
    )


@dataclass
class RecordedRig:
    deps: AnyQueryDeps
    enrolment: WalletEnrolment
    chain: RecordedChain
    upstream: RecordedOrthogonal
    helius: RecordedHeliusPost
    clock: DemoClock
    catalog: LoadedCatalog
    creator: Any
    fee_wallet: str
    relay: str
    settlement_wallet: str


def build_rig(
    fixtures: DemoFixtures,
    *,
    payer: str,
    signer_for: Callable[[WalletBinding], SigningBackend],
    settlement_signer: SigningBackend,
    folder: Path,
) -> RecordedRig:
    """Every lane's real implementation, with the recorded edge plugged in."""
    fee_wallet = demo_address("fee-wallet")
    relay = demo_address("kora-relay")
    pool = demo_address("whirlpool-sol-usdc")
    settlement_wallet = settlement_signer.pubkey
    config = AnyQueryConfig(
        billing="solana",
        fee_wallet=fee_wallet,
        relay_fee_payer=relay,
        rebate_split_user_pct=_SPLIT_USER_PCT,
        wallet_create_enabled=True,
        settlement_wallet_id=_SETTLEMENT_WALLET_ID,
        settlement_wallet=settlement_wallet,
        settlement_pool=pool,
    )
    clock = DemoClock()
    ids = (f"{n:016x}" for n in itertools.count(1))
    store = MemoryStore()
    binder = InMemoryWalletBinder()
    chain = RecordedChain(
        payer=payer,
        fee_wallet=fee_wallet,
        relay=relay,
        pool=pool,
        rate_micro=_RATE_MICRO,
        settlement=settlement_wallet,
        rebates={0: _REBATE_LAMPORTS},
    )
    helius_post = RecordedHeliusPost(chain)
    helius = HeliusSend(
        resolver=ChainResolver([KeyringBackend(module=RecordedKeyring())]),
        post=helius_post,
        relay_fee_payer=relay,
    )
    public = PublicSend(_RPC_URL, chain)
    settlement = Settlement(
        config=config,
        store=store,
        rpc_url=_RPC_URL,
        network="mainnet",
        backend=lambda: settlement_signer,
        swap_planner=fixed_rate_swap_planner(
            fixtures.swap_idl,
            pool=pool,
            vault_a=demo_address("vault-a"),
            vault_b=demo_address("vault-b"),
            tick_arrays=[demo_address(f"tick-{i}") for i in range(3)],
            rate_micro=_RATE_MICRO,
            slippage_bps=config.swap_slippage_bps,
        ),
        rpc_call=chain,
        send=helius,
        sleep=lambda _s: None,
        clock=clock.iso,
        new_id=lambda: next(ids),
    )
    billing = SolanaBilling(
        config=config,
        store=store,
        wallets=binder,
        rpc_url=_RPC_URL,
        network="mainnet",
        backend_for=signer_for,
        sol_pricer=fixed_rate_sol_pricer(
            pool=pool, rate_micro=_RATE_MICRO, margin_bps=config.sol_payment_margin_bps
        ),
        settlement=settlement,
        rpc_call=chain,
        send=public,
        sleep=lambda _s: None,
        clock=clock.iso,
        new_id=lambda: next(ids),
        rebate_index=MemoryRebateIndex(),
    )
    catalog = load_catalog(fixtures.details, ledgers=fixtures.ledgers)
    upstream = RecordedOrthogonal(fixtures.upstream_runs)
    base = JobDeps(
        api=OrthogonalApi(transport=upstream, session=public_session()),
        ledger=MemoryLedger(),
        folder=folder,
        mode="recorded",
        clock=clock.epoch,
        total_cap=500,
    )
    deps = AnyQueryDeps(
        config=config,
        store=store,
        billing=billing,
        planner=ModelPlanner(call=recorded_model_call(fixtures.planner_reply)),
        candidates_for=bm25_candidates,
        validate_plan=validator_for(catalog.confirmed),
        catalog=lambda: catalog.candidates,
        execute=execute,
        run_deps=RunDeps(
            base=base, mode="recorded", store=store, public_fetch=_no_public_fetch
        ),
        payee_usdc_ata=lambda: usdc_ata(fee_wallet),
        clock=clock.iso,
    )
    creator = recorded_wallet_creator("privy-recorded-wallet-1", payer)
    return RecordedRig(
        deps=deps,
        enrolment=WalletEnrolment(config, binder, creator),
        chain=chain,
        upstream=upstream,
        helius=helius_post,
        clock=clock,
        catalog=catalog,
        creator=creator,
        fee_wallet=fee_wallet,
        relay=relay,
        settlement_wallet=settlement_wallet,
    )


def _no_public_fetch(_url: str) -> tuple[int, str]:
    # Public-catalog sources are skipped by the runner today; a call here is a bug.
    raise RuntimeError("recorded demo makes no public fetch")


@dataclass
class DemoReport:
    steps: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def add(self, name: str, answer: Mapping[str, Any]) -> dict[str, Any]:
        self.steps.append((name, dict(answer)))
        return dict(answer)

    def get(self, name: str) -> dict[str, Any]:
        return next(answer for step, answer in self.steps if step == name)


class DemoStepError(Exception):
    """A step answered with a refusal the story does not expect. Our words only."""


def _call(
    rig: RecordedRig,
    report: DemoReport,
    step: str,
    account: str,
    tool: str,
    **args: Any,
) -> dict[str, Any]:
    answer = call_anyquery_tool(rig.deps, account, tool, args)
    report.add(step, answer)
    if "error" in answer:
        raise DemoStepError(f"{step}: {answer.get('code')}: {answer['error']}")
    return answer


def run_demo(
    rig: RecordedRig, account: str, *, subjects: Sequence[str], query: str
) -> DemoReport:
    """The story, step by step. Each step's tool answer goes into the report."""
    report = DemoReport()
    binding = rig.enrolment.enrol(account)
    report.add(
        "onboard",
        {
            "account": account,
            "wallet_id": binding.wallet_id,
            "address": binding.pubkey,
            "bound_on": "create (Privy's answer), never a caller's assertion",
        },
    )
    planned = _call(
        rig, report, "plan", account, "plan_query", query=query, subjects=list(subjects)
    )
    plan_id = planned["plan"]["plan_id"]
    _call(
        rig,
        report,
        "run_1",
        account,
        "run_query",
        plan_id=plan_id,
        quote_id=planned["quote"]["quote_id"],
        confirm=True,
        pay_with="SOL",
    )
    _call(rig, report, "payments_after_run_1", account, "payments")
    # A week later: the next Monday's run, against the next recorded replies.
    rig.clock.advance(_WEEK_S)
    rig.upstream.next_run()
    requote = _call(
        rig, report, "requote", account, "run_query", plan_id=plan_id, confirm=False
    )
    second = _call(
        rig,
        report,
        "run_2",
        account,
        "run_query",
        plan_id=plan_id,
        quote_id=requote["quote"]["quote_id"],
        confirm=True,
        pay_with="SOL",
    )
    run_1 = report.get("run_1")["run"]["run_id"]
    run_2 = second["run"]["run_id"]
    _call(rig, report, "run_rows_run_2", account, "run_rows", run_id=run_2)
    _call(rig, report, "run_changes", account, "run_changes", run_id=run_1)
    _call(rig, report, "payments", account, "payments")
    report.add(
        "edge",
        {
            "orthogonal_calls": len(rig.upstream.seen),
            "chain_payments": [
                {
                    "signature": p.signature,
                    "slot": p.slot,
                    "lamports": p.tx.lamports,
                    "to": rig.settlement_wallet,
                }
                for p in rig.chain.paid
            ],
            "chain_settlements": [
                {
                    "signature": x.signature,
                    "slot": x.slot,
                    "lamports_swapped": x.tx.lamports,
                    "usdc_out_micro": x.usdc_out,
                    "usdc_to_treasury_micro": x.tx.usdc,
                }
                for x in rig.chain.settled
            ],
            "helius_rebate_address": rig.helius.rebate_addresses,
            "fee_wallet": rig.fee_wallet,
            "settlement_wallet": rig.settlement_wallet,
            "relay_fee_payer": rig.relay,
            "privy_create_calls": len(rig.creator.asked),
        },
    )
    return report

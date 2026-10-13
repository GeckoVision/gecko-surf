"""``anyquery_deps_from_env``: a deploy mounts any-query only when every lane is configured.

Fakes only: the keychain is a dict behind a ``ChainResolver``, Mongo is the in-memory
client, and nothing here may reach a network (the model, Orthogonal, Privy and the RPC
are built, never called).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tests.anyquery_fake_mongo import FakeMongoClient
from gecko.anyquery.hosted import anyquery_deps_from_env
from gecko.anyquery.model_planner import ModelPlanner
from gecko.anyquery.mongo import MongoStore
from gecko.anyquery.solana_billing import SolanaBilling
from gecko.anyquery.upstream_spend import MongoSpendLedger
from gecko.credentials import ChainResolver, CredentialRef

FIXTURES = Path(__file__).parent / "fixtures" / "anyquery"
FEE_WALLET = "EQcc9TQFAZCAirqWskbDmUoVMs71uHfgcWvuKYg69z66"
RELAY = "2bg1ZvtSMCXNBkhmV9R9QSftNcpYEBfjvxf6xzY4dRdj"
RPC_URL = "https://rpc.example.invalid/?api-key=rpc-secret-in-url"

#: Values a log line must never carry.
SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-fake-secret",
    "orthogonal": "orth-fake-secret",
    "MONGODB_URI": "mongodb+srv://user:mongo-fake-secret@cluster.invalid/",
    "PRIVY_APP_ID": "privy-app-fake",
    "PRIVY_APP_SECRET": "privy-secret-fake",
}


class DictBackend:
    name = "fake"

    def __init__(self, held: dict[str, str]) -> None:
        self.held = held

    def available(self) -> bool:
        return True

    def get(self, ref: CredentialRef) -> str | None:
        return self.held.get(ref.slot())


def _details_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "details"
    folder.mkdir()
    records = json.loads(
        (FIXTURES / "orthogonal_details_subset.json").read_text(encoding="utf-8")
    )["records"]
    for n, record in enumerate(records):
        (folder / f"{n:03d}.json").write_text(json.dumps(record), encoding="utf-8")
    return folder


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "GECKO_ANYQUERY_BILLING": "solana",
        "GECKO_FEE_WALLET": FEE_WALLET,
        "GECKO_RELAY_FEE_PAYER": RELAY,
        "GECKO_REBATE_SPLIT_USER_PCT": "50",
        "GECKO_ANYQUERY_DETAILS_DIR": str(_details_dir(tmp_path)),
        "GECKO_ANYQUERY_DATA_DIR": str(tmp_path / "data"),
        "GECKO_ANYQUERY_UPSTREAM_CAP_CENTS": "2000",
        "GECKO_ANYQUERY_RPC_URL": RPC_URL,
        "GECKO_ANYQUERY_NETWORK": "mainnet",
    }


def _build(
    env: dict[str, str], held: dict[str, str] | None = None
) -> tuple[Any, list[str]]:
    connected: list[str] = []

    def connect(uri: str) -> FakeMongoClient:
        connected.append(uri)
        return FakeMongoClient()

    deps = anyquery_deps_from_env(
        env,
        resolver=ChainResolver([DictBackend(dict(SECRETS if held is None else held))]),
        connect=connect,
        swap_idl_fetch=lambda _program: {},
    )
    return deps, connected


def test_a_configured_deploy_gets_every_real_lane(tmp_path: Path) -> None:
    deps, connected = _build(_env(tmp_path))

    assert deps is not None
    assert isinstance(deps.planner, ModelPlanner)
    assert isinstance(deps.store, MongoStore)
    assert isinstance(deps.billing, SolanaBilling)
    assert deps.billing.network == "mainnet"
    assert deps.billing.rebate_index is not None
    assert deps.run_deps.mode == "live" and deps.run_deps.base.mode == "live"
    assert deps.run_deps.base.total_cap == 2000
    assert deps.catalog().candidates, "the details folder became candidates"
    assert connected == [SECRETS["MONGODB_URI"]], "one client, from the keychain"
    # The payee is derived from the fee wallet, never configured on its own.
    assert deps.payee_usdc_ata() != FEE_WALLET


def test_the_hosted_spend_cap_is_kept_in_mongo_not_in_the_task(
    tmp_path: Path,
) -> None:
    """A Fargate task has no persistent disk: a file ledger reset the cap per deploy."""
    deps, _ = _build(_env(tmp_path))

    ledger = deps.run_deps.base.ledger
    assert isinstance(ledger, MongoSpendLedger)
    assert ledger.client is deps.store.client, "one client for the store and the cap"
    assert not (tmp_path / "data" / "orthogonal-spend.json").exists()


def _without(env: dict[str, str], name: str) -> dict[str, str]:
    return {k: v for k, v in env.items() if k != name}


@pytest.mark.parametrize(
    ("name", "said"),
    [
        ("GECKO_ANYQUERY_BILLING", "billing"),
        ("GECKO_ANYQUERY_DETAILS_DIR", "GECKO_ANYQUERY_DETAILS_DIR"),
        ("GECKO_ANYQUERY_UPSTREAM_CAP_CENTS", "GECKO_ANYQUERY_UPSTREAM_CAP_CENTS"),
        ("GECKO_ANYQUERY_RPC_URL", "GECKO_ANYQUERY_RPC_URL"),
        ("GECKO_ANYQUERY_NETWORK", "GECKO_ANYQUERY_NETWORK"),
        ("GECKO_FEE_WALLET", "GECKO_FEE_WALLET"),
    ],
)
def test_a_missing_setting_is_no_mount_with_the_reason_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, name: str, said: str
) -> None:
    with caplog.at_level(logging.INFO, logger="gecko.anyquery.hosted"):
        deps, connected = _build(_without(_env(tmp_path), name))
    assert deps is None
    assert said in caplog.text
    assert connected == [], "nothing is opened before the config is complete"


@pytest.mark.parametrize(
    "slot",
    [
        "ANTHROPIC_API_KEY",
        "orthogonal",
        "MONGODB_URI",
        "PRIVY_APP_ID",
        "PRIVY_APP_SECRET",
    ],
)
def test_a_missing_keychain_slot_is_no_mount_and_names_the_slot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, slot: str
) -> None:
    held = {k: v for k, v in SECRETS.items() if k != slot}
    with caplog.at_level(logging.INFO, logger="gecko.anyquery.hosted"):
        deps, _ = _build(_env(tmp_path), held)
    assert deps is None
    assert slot in caplog.text


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GECKO_ANYQUERY_NETWORK", "fork"),
        ("GECKO_ANYQUERY_UPSTREAM_CAP_CENTS", "lots"),
        ("GECKO_ANYQUERY_UPSTREAM_CAP_CENTS", "0"),
        ("GECKO_ANYQUERY_RPC_URL", "file:///etc/passwd"),
        ("GECKO_ANYQUERY_BILLING", "fake"),
    ],
)
def test_a_bad_setting_is_no_mount(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, name: str, value: str
) -> None:
    env = {**_env(tmp_path), name: value}
    with caplog.at_level(logging.INFO, logger="gecko.anyquery.hosted"):
        deps, _ = _build(env)
    assert deps is None
    assert name in caplog.text


def test_an_empty_details_folder_is_no_mount(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {**_env(tmp_path), "GECKO_ANYQUERY_DETAILS_DIR": str(empty)}
    with caplog.at_level(logging.INFO, logger="gecko.anyquery.hosted"):
        deps, _ = _build(env)
    assert deps is None
    assert "no catalog candidates" in caplog.text


def test_no_log_line_carries_a_secret_or_the_rpc_url(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = _env(tmp_path)
    with caplog.at_level(logging.DEBUG):
        _build(env)
        _build(_without(env, "GECKO_ANYQUERY_NETWORK"))
        _build(env, {"ANTHROPIC_API_KEY": SECRETS["ANTHROPIC_API_KEY"]})
    for secret in [*SECRETS.values(), RPC_URL, "rpc-secret-in-url"]:
        assert secret not in caplog.text


# --- x402 mode (founder ruling, 9 Oct) ----------------------------------------------------

_SETTLEMENT_ENV = {
    "GECKO_SETTLEMENT_WALLET_ID": "testwallet00000000000002",
    "GECKO_SETTLEMENT_WALLET": "FqQtW9mNLSbicvbhgdZHj7vpdxniJKLXsgmvDm3F46U4",
}


def test_the_key_path_is_unchanged_without_the_switch(tmp_path: Path) -> None:
    deps, _ = _build({**_env(tmp_path), **_SETTLEMENT_ENV})
    assert deps is not None and deps.run_deps.base.api.x402 is None


def test_x402_mode_pays_orthogonal_from_the_settlement_wallet(tmp_path: Path) -> None:
    from gecko.anyquery.upstream_x402 import OrthogonalX402

    env = {**_env(tmp_path), **_SETTLEMENT_ENV, "GECKO_ORTHOGONAL_PAY": "x402"}
    # The Orthogonal key is not needed to pay over x402.
    held = {k: v for k, v in SECRETS.items() if k != "orthogonal"}
    deps, _ = _build(env, held)

    assert deps is not None
    payer = deps.run_deps.base.api.x402
    assert isinstance(payer, OrthogonalX402)
    assert payer.policy.wallet == _SETTLEMENT_ENV["GECKO_SETTLEMENT_WALLET"]
    # No single pin: each operation's payTo comes from the registry's last probe.
    assert payer.policy.pay_to is None and payer.pay_to_for is not None
    assert payer.network == "mainnet" and payer.rpc_url == RPC_URL


def test_x402_mode_with_the_env_override_pins_every_call_to_it(tmp_path: Path) -> None:
    pay_to = "DHZJ3PpyVtaomjEiHVwzL982prM2LeENNuHiFMxJy4NC"
    env = {
        **_env(tmp_path),
        **_SETTLEMENT_ENV,
        "GECKO_ORTHOGONAL_PAY": "x402",
        "GECKO_ORTHOGONAL_X402_PAYTO": pay_to,
    }
    deps, _ = _build(env)
    assert deps is not None and deps.run_deps.base.api.x402 is not None
    assert deps.run_deps.base.api.x402.policy.pay_to == pay_to


def test_x402_mode_without_a_settlement_wallet_is_no_mount(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = {**_env(tmp_path), "GECKO_ORTHOGONAL_PAY": "x402"}
    with caplog.at_level(logging.WARNING):
        deps, _ = _build(env)
    assert deps is None
    assert "settlement wallet" in caplog.text

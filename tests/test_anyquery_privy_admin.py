"""The founder-run Privy steps (option B, 9 Oct), and the User-Agent Privy's edge needs.

Offline: Privy is an injected transport that records every request and answers in the
shape Privy answers in. Nothing here reaches a network, a keychain or a key. What is
proven is what Gecko SENDS and what it refuses to send, not that Privy accepts it; the
words Privy echoed on 9 Oct are pinned in test_anyquery_wallet_policy.
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

import pytest

pytest.importorskip("solders")

from gecko.anyquery.config import ConfigError, load_config  # noqa: E402
from gecko.anyquery.payment_tx import usdc_ata  # noqa: E402
from gecko.anyquery.privy_admin import (  # noqa: E402
    AdminError,
    create_settlement_wallet,
    settlement_rules,
    update_settlement_policy,
    update_user_policy,
    user_rules,
)
from gecko.anyquery.settlement_tx import wsol_ata  # noqa: E402
from gecko.privy_backend import (  # noqa: E402
    POLICY_CREATE_PATH,
    PRIVY_API_BASE,
    UNCONFIRMED_POLICY_UPDATE_METHOD,
    UNCONFIRMED_WALLET_POLICY_IDS_FIELD,
    PrivyBackendError,
    PrivyRequest,
    _default_transport,
    get_wallet,
    update_policy,
)
from tests.test_anyquery_solana_billing import (  # noqa: E402
    CONFIG,
    FEE_WALLET,
    SETTLEMENT,
)

SECRET = "privy-secret-SENTINEL-DO-NOT-LEAK"
ENV = {"PRIVY_APP_ID": "app", "PRIVY_APP_SECRET": SECRET}
NEW_WALLET = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
USER_WALLET_ID = "testwallet00000000000001"
USER_POLICY_ID = "testpolicy00000000000001"
#: No settlement wallet yet: the state before the founder's create.
BEFORE = replace(
    CONFIG,
    settlement_wallet=None,
    settlement_wallet_id=None,
    settlement_create_enabled=True,
)


@dataclass
class Privy:
    """Answers like Privy: an id for a policy, a wallet for a create, the policy back
    for an update, and a wallet record for a read."""

    attached: list[str] = field(default_factory=lambda: [USER_POLICY_ID])
    fail_update: bool = False
    seen: list[PrivyRequest] = field(default_factory=list)
    headers: list[Mapping[str, str]] = field(default_factory=list)

    def __call__(
        self, request: PrivyRequest, headers: Mapping[str, str]
    ) -> dict[str, Any]:
        self.seen.append(request)
        self.headers.append(headers)
        if request.method == "GET":
            return {
                "id": USER_WALLET_ID,
                "address": NEW_WALLET,
                "chain_type": "solana",
                UNCONFIRMED_WALLET_POLICY_IDS_FIELD: self.attached,
            }
        if request.method == UNCONFIRMED_POLICY_UPDATE_METHOD:
            if self.fail_update:
                raise PrivyBackendError("Privy answered HTTP 400")
            policy_id = request.url.rsplit("/", 1)[-1]
            assert request.body is not None
            return {"id": policy_id, "rules": request.body["rules"]}
        if request.url.endswith(POLICY_CREATE_PATH):
            return {"id": "settlepolicy0001"}
        return {"id": "settlewallet0001", "address": NEW_WALLET, "chain_type": "solana"}


# --------------------------------------------------------------------------------------
# The settlement wallet's create: policy, wallet, full rules
# --------------------------------------------------------------------------------------


def test_the_settlement_create_is_three_calls_in_order() -> None:
    privy = Privy()

    made = create_settlement_wallet(BEFORE, env=ENV, transport=privy)

    assert (made.wallet_id, made.address, made.policy_id) == (
        "settlewallet0001",
        NEW_WALLET,
        "settlepolicy0001",
    )
    assert made.full_rules
    policy, wallet, update = privy.seen
    assert (policy.method, policy.url) == (
        "POST",
        f"{PRIVY_API_BASE}{POLICY_CREATE_PATH}",
    )
    # Before the wallet exists its wSOL account cannot be named: no wrap rule yet.
    assert policy.body is not None
    assert policy.body["rules"] == settlement_rules(BEFORE, wallet=None)
    assert "Transfer.to" not in repr(policy.body)
    assert wallet.body == {
        "chain_type": "solana",
        UNCONFIRMED_WALLET_POLICY_IDS_FIELD: ["settlepolicy0001"],
    }
    assert update.method == UNCONFIRMED_POLICY_UPDATE_METHOD
    assert update.url == f"{PRIVY_API_BASE}{POLICY_CREATE_PATH}/settlepolicy0001"
    assert update.body == {"rules": settlement_rules(BEFORE, wallet=NEW_WALLET)}
    assert wsol_ata(NEW_WALLET) in repr(update.body)
    assert SECRET not in repr(privy.seen)


def test_a_failed_third_call_still_reports_the_wallet_it_made() -> None:
    privy = Privy(fail_update=True)

    made = create_settlement_wallet(BEFORE, env=ENV, transport=privy)

    assert not made.full_rules and made.address == NEW_WALLET
    assert "update-settlement-policy" in made.note
    assert SECRET not in made.note


def test_the_settlement_create_ships_disabled() -> None:
    privy = Privy()
    with pytest.raises(AdminError, match="off"):
        create_settlement_wallet(
            replace(BEFORE, settlement_create_enabled=False), env=ENV, transport=privy
        )
    assert privy.seen == []


def test_gecko_keeps_exactly_one_settlement_wallet() -> None:
    privy = Privy()
    with pytest.raises(AdminError, match="already configured"):
        create_settlement_wallet(
            replace(CONFIG, settlement_create_enabled=True), env=ENV, transport=privy
        )
    assert privy.seen == []


def test_the_settlement_create_needs_the_app_secret_from_the_keychain() -> None:
    with pytest.raises(AdminError, match="PRIVY_APP_SECRET"):
        create_settlement_wallet(BEFORE, env={"PRIVY_APP_ID": "app"}, transport=Privy())


def test_the_settlement_policy_update_names_the_configured_wallet() -> None:
    privy = Privy()
    echo = update_settlement_policy(
        CONFIG, policy_id="settlepolicy0001", env=ENV, transport=privy
    )
    [update] = privy.seen
    assert update.body == {"rules": settlement_rules(CONFIG, wallet=SETTLEMENT)}
    assert echo["id"] == "settlepolicy0001"


FIBER_PAY_TO = "DHZJ3PpyVtaomjEiHVwzL982prM2LeENNuHiFMxJy4NC"
SCRAPE_PAY_TO = "4Bf6zpkrv4gtirAzM3kzqXrRRLXNmbKrpHLebTpAk3Wg"


def test_the_settlement_policy_update_carries_one_rule_per_pinned_pay_to() -> None:
    pinned = {SCRAPE_PAY_TO: ("scrapecreators",), FIBER_PAY_TO: ("fiber",)}
    privy = Privy()
    update_settlement_policy(
        CONFIG,
        policy_id="settlepolicy0001",
        x402_pay_tos=pinned,
        env=ENV,
        transport=privy,
    )
    [update] = privy.seen
    assert update.body is not None
    names = [r["name"] for r in update.body["rules"]]
    assert "USDC to scrapecreators (4Bf6), capped" in names
    assert "USDC to fiber (DHZJ), capped" in names
    assert all(len(n) < 50 for n in names)


# --------------------------------------------------------------------------------------
# The user wallet's policy: read the wallet first, then replace the rules
# --------------------------------------------------------------------------------------


def test_the_user_policy_update_reads_the_wallet_then_replaces_the_rules() -> None:
    privy = Privy()

    echo = update_user_policy(
        CONFIG,
        wallet_id=USER_WALLET_ID,
        policy_id=USER_POLICY_ID,
        env=ENV,
        transport=privy,
    )

    read, update = privy.seen
    assert (read.method, read.url) == (
        "GET",
        f"{PRIVY_API_BASE}/v1/wallets/{USER_WALLET_ID}",
    )
    assert update.url == f"{PRIVY_API_BASE}{POLICY_CREATE_PATH}/{USER_POLICY_ID}"
    assert update.body == {"rules": user_rules(CONFIG)}
    usdc, sol = update.body["rules"]
    assert usdc_ata(FEE_WALLET) in repr(usdc)
    assert SETTLEMENT in repr(sol) and FEE_WALLET not in repr(sol)
    assert echo["id"] == USER_POLICY_ID


def test_a_policy_not_attached_to_the_wallet_is_never_changed() -> None:
    privy = Privy(attached=["someotherpolicy1"])
    with pytest.raises(AdminError, match="nothing was changed"):
        update_user_policy(
            CONFIG,
            wallet_id=USER_WALLET_ID,
            policy_id=USER_POLICY_ID,
            env=ENV,
            transport=privy,
        )
    assert [r.method for r in privy.seen] == ["GET"]


def test_the_user_rules_need_the_settlement_wallet() -> None:
    with pytest.raises(ConfigError, match="GECKO_SETTLEMENT_WALLET"):
        user_rules(replace(CONFIG, settlement_wallet=None, settlement_wallet_id=None))


@pytest.mark.parametrize("bad", ["../wallets/x", "ABC", "a" * 65, "", "short"])
def test_an_id_that_is_not_a_privy_id_never_reaches_a_url(bad: str) -> None:
    privy = Privy()
    with pytest.raises(PrivyBackendError):
        update_policy(
            app_id="app",
            app_secret=SECRET,
            policy_id=bad,
            rules=[{"name": "x"}],
            transport=privy,
        )
    with pytest.raises(PrivyBackendError):
        get_wallet(app_id="app", app_secret=SECRET, wallet_id=bad, transport=privy)
    assert privy.seen == []


def test_an_update_echo_that_differs_from_what_was_sent_is_refused() -> None:
    def short_echo(request: PrivyRequest, headers: Mapping[str, str]) -> dict:
        return {"id": USER_POLICY_ID, "rules": []}

    with pytest.raises(PrivyBackendError, match="rules that were sent"):
        update_policy(
            app_id="app",
            app_secret=SECRET,
            policy_id=USER_POLICY_ID,
            rules=[{"name": "x"}],
            transport=short_echo,
        )


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------


def test_the_settlement_settings_load_from_the_environment() -> None:
    config = load_config(
        {
            "GECKO_FEE_WALLET": FEE_WALLET,
            "GECKO_SETTLEMENT_WALLET_ID": "settlewallet0001",
            "GECKO_SETTLEMENT_WALLET": SETTLEMENT,
            "GECKO_SETTLEMENT_POOL": NEW_WALLET,
            "GECKO_SOL_PAYMENT_MARGIN_BPS": "150",
            "GECKO_SETTLEMENT_WALLET_CREATE": "1",
        }
    )
    assert config.settlement_wallet_id == "settlewallet0001"
    assert config.settlement_wallet == SETTLEMENT
    assert config.settlement_pool == NEW_WALLET
    assert config.sol_payment_margin_bps == 150
    assert config.settlement_create_enabled
    assert not load_config({}).settlement_create_enabled


@pytest.mark.parametrize(
    "env",
    [
        {"GECKO_SETTLEMENT_WALLET_ID": "settlewallet0001"},  # id without address
        {"GECKO_SETTLEMENT_WALLET": SETTLEMENT},  # address without id
        {"GECKO_SETTLEMENT_WALLET_ID": "../x", "GECKO_SETTLEMENT_WALLET": SETTLEMENT},
        {
            "GECKO_SETTLEMENT_WALLET_ID": "settlewallet0001",
            "GECKO_SETTLEMENT_WALLET": "x",
        },
        {
            "GECKO_FEE_WALLET": FEE_WALLET,
            "GECKO_SETTLEMENT_WALLET_ID": "settlewallet0001",
            "GECKO_SETTLEMENT_WALLET": FEE_WALLET,
        },
        {"GECKO_SETTLEMENT_POOL": "not-a-pool"},
        {"GECKO_SOL_PAYMENT_MARGIN_BPS": "5000"},
    ],
)
def test_a_wrong_settlement_setting_is_refused_by_name(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="GECKO_"):
        load_config(env)


# --------------------------------------------------------------------------------------
# The User-Agent: Privy's Cloudflare answers 403 (error 1010) to Python's default one
# --------------------------------------------------------------------------------------


def test_the_privy_transport_sends_a_real_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _Answer:
        def __enter__(self) -> "_Answer":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"id": "settlepolicy0001"}'

    def fake_urlopen(req: urllib.request.Request, timeout: float) -> _Answer:
        captured["ua"] = req.get_header("User-agent")  # urllib title-cases the key
        captured["headers"] = dict(req.header_items())
        return _Answer()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    request = PrivyRequest(
        method="POST",
        url=f"{PRIVY_API_BASE}{POLICY_CREATE_PATH}",
        body={"rules": []},
        privy_headers={"privy-app-id": "app"},
    )

    answer = _default_transport(request, {"privy-app-id": "app"})

    assert answer == {"id": "settlepolicy0001"}
    ua = captured["ua"]
    assert ua and ua.startswith("gecko-surf")
    assert "Python-urllib" not in ua
    # Exactly one User-Agent: a caller's header cannot add a second, default one.
    assert [k for k in captured["headers"] if k.lower() == "user-agent"] == [
        "User-agent"
    ]


def test_a_caller_cannot_replace_the_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _Answer:
        def __enter__(self) -> "_Answer":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(req: urllib.request.Request, timeout: float) -> _Answer:
        captured["ua"] = req.get_header("User-agent")
        return _Answer()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    request = PrivyRequest("GET", f"{PRIVY_API_BASE}/v1/wallets/x", None, {})
    _default_transport(request, {"User-Agent": "Python-urllib/3.12"})
    assert captured["ua"].startswith("gecko-surf")

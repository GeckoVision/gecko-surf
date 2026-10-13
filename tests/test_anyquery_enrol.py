"""Onboarding: bind on CREATE, never on ASSERT. Offline; Privy is an injected transport."""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field
from typing import Any, Mapping

import pytest

from gecko.anyquery.config import AnyQueryConfig, load_config
from gecko.anyquery.enrol import (
    AlreadyEnrolledError,
    EnrolmentDisabledError,
    EnrolmentError,
    InMemoryWalletBinder,
    MongoWalletBinder,
    WalletEnrolment,
    privy_wallet_creator,
)
from gecko.anyquery.wallet_policy import WalletPolicy
from gecko.credentials import ChainResolver, KeyringBackend
from gecko.privy_backend import (
    PRIVY_API_BASE,
    CreatedWallet,
    PrivyBackendError,
    PrivyRequest,
    create_solana_wallet,
    idempotency_key_for,
    resolve_config,
)
from gecko.wallet_binding import WalletBinding, WalletBindingError

ADDRESS = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
SECRET = "privy-secret-SENTINEL-DO-NOT-LEAK"
ON = AnyQueryConfig(wallet_create_enabled=True)


@dataclass
class Creator:
    keys: list[str] = field(default_factory=list)
    answer: CreatedWallet = CreatedWallet("wlt_123", ADDRESS)

    def __call__(self, idempotency_key: str) -> CreatedWallet:
        self.keys.append(idempotency_key)
        return self.answer


def test_creation_ships_disabled() -> None:
    assert load_config({}).wallet_create_enabled is False
    creator = Creator()
    enrolment = WalletEnrolment(AnyQueryConfig(), InMemoryWalletBinder(), creator)
    with pytest.raises(EnrolmentDisabledError):
        enrolment.enrol("acct_1")
    assert creator.keys == []


def test_enrol_makes_the_wallet_and_binds_privys_answer() -> None:
    creator = Creator()
    binder = InMemoryWalletBinder()

    binding = WalletEnrolment(ON, binder, creator).enrol("acct_1")

    assert binding == WalletBinding("acct_1", "wlt_123", ADDRESS)
    assert binder.wallet_for("acct_1") == binding
    # The create key is derived from the account, never the account id itself.
    assert creator.keys == [idempotency_key_for("acct_1")]
    assert "acct_1" not in creator.keys[0]


def test_enrol_takes_an_account_and_nothing_a_caller_could_use_to_name_a_wallet() -> (
    None
):
    params = list(inspect.signature(WalletEnrolment.enrol).parameters)
    assert params == ["self", "account_id"]
    assert list(inspect.signature(create_solana_wallet).parameters) == [
        "app_id",
        "app_secret",
        "idempotency_key",
        "policy_ids",
        "authorization_key",
        "transport",
    ]


def test_a_second_enrol_is_refused_and_never_creates_again() -> None:
    creator = Creator()
    enrolment = WalletEnrolment(ON, InMemoryWalletBinder(), creator)
    enrolment.enrol("acct_1")
    with pytest.raises(AlreadyEnrolledError):
        enrolment.enrol("acct_1")
    assert len(creator.keys) == 1


def test_a_directory_that_cannot_answer_never_leads_to_a_create() -> None:
    class Broken(InMemoryWalletBinder):
        def wallet_for(self, account_id: str) -> WalletBinding | None:
            raise WalletBindingError("down")

    creator = Creator()
    with pytest.raises(EnrolmentError, match="could not be read"):
        WalletEnrolment(ON, Broken(), creator).enrol("acct_1")
    assert creator.keys == []


def test_a_custodian_answer_that_is_not_an_address_is_not_bound() -> None:
    binder = InMemoryWalletBinder()
    creator = Creator(answer=CreatedWallet("wlt_1", "not-an-address!"))
    with pytest.raises(EnrolmentError, match="refused"):
        WalletEnrolment(ON, binder, creator).enrol("acct_1")
    assert binder.bindings == {}


def test_an_empty_account_is_refused() -> None:
    with pytest.raises(EnrolmentError):
        WalletEnrolment(ON, InMemoryWalletBinder(), Creator()).enrol("  ")


class _DuplicateKeyError(Exception):
    pass


_DuplicateKeyError.__name__ = "DuplicateKeyError"


@dataclass
class FakeCollection:
    docs: list[dict[str, Any]] = field(default_factory=list)

    def find_one(self, query: Mapping[str, Any]) -> dict[str, Any] | None:
        return next(
            (d for d in self.docs if d["account_id"] == query["account_id"]), None
        )

    def insert_one(self, doc: dict[str, Any]) -> None:
        if self.find_one(doc) is not None:
            raise _DuplicateKeyError("E11000 mongodb+srv://user:pw@host")
        self.docs.append(dict(doc))


def test_mongo_binder_turns_a_duplicate_key_into_already_enrolled_without_the_uri() -> (
    None
):
    binder = MongoWalletBinder(FakeCollection())
    binder.bind_new(WalletBinding("acct_1", "wlt_1", ADDRESS))
    with pytest.raises(AlreadyEnrolledError) as caught:
        binder.bind_new(WalletBinding("acct_1", "wlt_2", ADDRESS))
    assert "mongodb" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert binder.wallet_for("acct_1") == WalletBinding("acct_1", "wlt_1", ADDRESS)


# --------------------------------------------------------------------------------------
# The Privy create call
# --------------------------------------------------------------------------------------


@dataclass
class Transport:
    answer: dict[str, Any]
    seen: list[tuple[PrivyRequest, Mapping[str, str]]] = field(default_factory=list)

    def __call__(
        self, request: PrivyRequest, headers: Mapping[str, str]
    ) -> dict[str, Any]:
        self.seen.append((request, headers))
        return self.answer


def test_create_asks_privy_to_make_a_solana_wallet_with_an_idempotency_key() -> None:
    transport = Transport({"id": "wlt_9", "address": ADDRESS, "chain_type": "solana"})

    created = create_solana_wallet(
        app_id="app",
        app_secret=SECRET,
        idempotency_key="k1",
        policy_ids=["pol_1"],
        transport=transport,
    )

    assert created == CreatedWallet("wlt_9", ADDRESS)
    [(request, headers)] = transport.seen
    assert request.method == "POST"
    assert request.url == f"{PRIVY_API_BASE}/v1/wallets"
    assert request.body == {"chain_type": "solana", "policy_ids": ["pol_1"]}
    assert request.privy_headers["privy-idempotency-key"] == "k1"
    # The secret rides only in the transport's headers, never in the request record.
    assert SECRET not in repr(request)
    assert headers["Authorization"].startswith("Basic ")


@pytest.mark.parametrize(
    "answer",
    [
        {"address": ADDRESS, "chain_type": "solana"},
        {"id": "wlt_9", "chain_type": "solana"},
        {"id": "wlt_9", "address": ADDRESS, "chain_type": "ethereum"},
    ],
)
def test_create_refuses_an_incomplete_or_foreign_answer(answer: dict) -> None:
    with pytest.raises(PrivyBackendError):
        create_solana_wallet(
            app_id="app",
            app_secret=SECRET,
            idempotency_key="k1",
            policy_ids=["pol_1"],
            transport=Transport(answer),
        )


@pytest.mark.parametrize("policy_ids", [[], [""]])
def test_a_wallet_is_never_created_without_its_policy(policy_ids: list[str]) -> None:
    transport = Transport({"id": "wlt_9", "address": ADDRESS, "chain_type": "solana"})
    with pytest.raises(PrivyBackendError, match="policy"):
        create_solana_wallet(
            app_id="app",
            app_secret=SECRET,
            idempotency_key="k1",
            policy_ids=policy_ids,
            transport=transport,
        )
    assert transport.seen == []


def test_the_live_creator_names_missing_keychain_entries_never_values() -> None:
    create = privy_wallet_creator(
        policy=WalletPolicy(ADDRESS, ADDRESS, 1, 1, ADDRESS),
        env={"PRIVY_APP_SECRET": SECRET},
    )
    with pytest.raises(EnrolmentError) as caught:
        create("k1")
    assert "PRIVY_APP_ID" in str(caught.value)
    assert SECRET not in str(caught.value)


class _Keyring:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get_keyring(self) -> object:
        return object()

    def get_password(self, service: str, user: str) -> str | None:
        return self._values.get(service.removeprefix("gecko:"))


def test_privy_config_comes_from_the_keychain_and_never_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIVY_APP_SECRET", "from-env-must-not-be-read")
    monkeypatch.setenv("GECKO_CRED_PRIVY_APP_SECRET", "from-env-must-not-be-read")
    resolver = ChainResolver(
        [KeyringBackend(module=_Keyring({"PRIVY_APP_ID": "app-from-keychain"}))]
    )

    resolved = resolve_config(resolver=resolver)

    assert resolved["PRIVY_APP_ID"] == "app-from-keychain"
    assert resolved["PRIVY_APP_SECRET"] == ""


def test_idempotency_key_is_stable_and_one_way() -> None:
    key = idempotency_key_for("acct_1")
    assert key == idempotency_key_for("acct_1") != idempotency_key_for("acct_2")
    assert key == hashlib.sha256(b"gecko-wallet:acct_1").hexdigest()

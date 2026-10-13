"""The hosted any-query secret source: keychain alone locally, keychain then the platform's
injected environment on a host that says ``GECKO_HOST_SECRETS=platform``.

No real keychain is touched (``KeyringBackend`` gets a fake module), no AWS CLI runs (the
push and compare helpers take a fake runner), nothing reaches a network.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from gecko.anyquery import host_secrets
from gecko.anyquery.hosted import anyquery_deps_from_env
from gecko.anyquery.host_secrets import (
    ANYQUERY_SLOTS,
    HostSecretsError,
    anyquery_resolver,
    compare_slots,
    push_slots,
    ssm_parameter_name,
)
from gecko.credentials import (
    ChainResolver,
    CredentialError,
    CredentialRef,
    KeyringBackend,
    PlatformSecretBackend,
    platform_env_name,
)
from gecko.privy_backend import PrivyBackend
from gecko.send_endpoint import HeliusSend
from gecko.wallet_binding import WalletBinding
from tests.anyquery_fake_mongo import FakeMongoClient
from tests.test_anyquery_hosted_deps import SECRETS, _env

HELIUS = "helius-fake-secret"
REQUIRED = [
    "ANTHROPIC_API_KEY",
    "orthogonal",
    "MONGODB_URI",
    "PRIVY_APP_ID",
    "PRIVY_APP_SECRET",
]
ALL_VALUES = {**SECRETS, "HELIUS_API_KEY": HELIUS}


class _FakeActive:
    """A keyring backend whose module is not ``.fail``/``.null``: a usable keychain."""


class FakeKeyring:
    def __init__(self, held: dict[str, str]) -> None:
        self.held = held
        self.asked: list[str] = []

    def get_keyring(self) -> Any:
        return _FakeActive()

    def get_password(self, service: str, _user: str) -> str | None:
        self.asked.append(service)
        return self.held.get(service.removeprefix("gecko:"))


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    """The keychain every any-query resolver builds, empty unless a test fills it."""
    fake = FakeKeyring({})
    monkeypatch.setattr(
        host_secrets, "KeyringBackend", lambda: KeyringBackend(module=fake)
    )
    return fake


def _platform(values: dict[str, str]) -> dict[str, str]:
    return {platform_env_name(slot): value for slot, value in values.items()}


def _build(env: dict[str, str]) -> Any:
    return anyquery_deps_from_env(
        env,
        connect=lambda _uri: FakeMongoClient(),
        swap_idl_fetch=lambda _program: {},
    )


# --- the resolver ------------------------------------------------------------


def test_the_local_default_is_the_keychain_alone() -> None:
    resolver = anyquery_resolver({platform_env_name("orthogonal"): "x"})
    assert [type(b) for b in resolver.backends] == [KeyringBackend]


def test_the_host_flag_adds_the_platform_after_the_keychain() -> None:
    resolver = anyquery_resolver({"GECKO_HOST_SECRETS": "platform"})
    assert [type(b) for b in resolver.backends] == [
        KeyringBackend,
        PlatformSecretBackend,
    ]


@pytest.mark.parametrize("flag", ["1", "env", "true", "Platform", "dotenv"])
def test_an_unknown_host_flag_is_refused_not_guessed(flag: str) -> None:
    with pytest.raises(HostSecretsError, match="GECKO_HOST_SECRETS"):
        anyquery_resolver({"GECKO_HOST_SECRETS": flag})


def test_the_platform_reads_only_its_allowlist() -> None:
    env = {"GECKO_CRED_TXODDS": "t", "GECKO_CRED_ORTHOGONAL": "o"}
    backend = PlatformSecretBackend(ANYQUERY_SLOTS, environ=env)
    assert backend.get(CredentialRef("orthogonal")) == "o"
    assert backend.get(CredentialRef("txodds")) is None


@pytest.mark.parametrize("value", ["", "  ", "__unset__", " __unset__\n"])
def test_the_ssm_sentinel_and_blank_are_absent(value: str) -> None:
    backend = PlatformSecretBackend(
        ANYQUERY_SLOTS, environ={"GECKO_CRED_MONGODB_URI": value}
    )
    assert backend.get(CredentialRef("MONGODB_URI")) is None


def test_the_keychain_wins_over_the_platform(keychain: FakeKeyring) -> None:
    keychain.held["orthogonal"] = "from-keychain"
    env = {"GECKO_HOST_SECRETS": "platform", **_platform({"orthogonal": "from-env"})}
    assert (
        anyquery_resolver(env).resolve(CredentialRef("orthogonal")) == "from-keychain"
    )


def test_without_the_flag_an_injected_variable_is_not_read(
    keychain: FakeKeyring,
) -> None:
    resolver = anyquery_resolver(_platform(ALL_VALUES))
    with pytest.raises(CredentialError):
        resolver.resolve(CredentialRef("ANTHROPIC_API_KEY"))


# --- the deps, end to end ----------------------------------------------------


def test_a_host_with_the_flag_builds_every_lane_from_the_platform(
    tmp_path: Path, keychain: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The send transport reads its switch from the process environment, as it does on
    # the host.
    monkeypatch.setenv("GECKO_SEND_REBATE", "1")
    env = {
        **_env(tmp_path),
        "GECKO_HOST_SECRETS": "platform",
        **_platform(ALL_VALUES),
        # Option B: the settlement wallet and its pool make SOL payable at all.
        "GECKO_SETTLEMENT_WALLET_ID": "settlewallet0001",
        "GECKO_SETTLEMENT_WALLET": "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL",
        "GECKO_SETTLEMENT_POOL": "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE",
    }
    deps = _build(env)
    assert deps is not None
    # Privy and Helius are built per payment; both must see the platform source too,
    # or the mount exists and every payment fails on a host with no keychain.
    binding = WalletBinding(
        account_id="acct-1",
        wallet_id="wallet-1",
        pubkey="EQcc9TQFAZCAirqWskbDmUoVMs71uHfgcWvuKYg69z66",
    )
    backend = deps.billing.backend_for(binding)
    assert isinstance(backend, PrivyBackend)
    assert backend.app_id == SECRETS["PRIVY_APP_ID"]
    assert backend.wallet_id == "wallet-1"
    # The user's payment is a plain transfer (public endpoint); the settlement swap is
    # what goes through Helius, and its Privy backend is the settlement wallet's.
    assert not isinstance(deps.billing.send, HeliusSend)
    settlement = deps.billing.settlement
    assert settlement is not None
    assert isinstance(settlement._send(), HeliusSend)
    settler = settlement.backend()
    assert isinstance(settler, PrivyBackend)
    assert settler.wallet_id == "settlewallet0001"
    assert settler.app_id == SECRETS["PRIVY_APP_ID"]


def test_locally_the_same_variables_serve_nothing(
    tmp_path: Path, keychain: FakeKeyring, caplog: pytest.LogCaptureFixture
) -> None:
    env = {**_env(tmp_path), **_platform(ALL_VALUES)}
    with caplog.at_level(logging.INFO, logger="gecko.anyquery.hosted"):
        assert _build(env) is None
    assert "ANTHROPIC_API_KEY" in caplog.text


@pytest.mark.parametrize("slot", REQUIRED)
@pytest.mark.parametrize("absent_as", ["missing", "sentinel"])
def test_a_missing_platform_secret_is_no_mount_naming_the_slot(
    tmp_path: Path,
    keychain: FakeKeyring,
    caplog: pytest.LogCaptureFixture,
    slot: str,
    absent_as: str,
) -> None:
    values = {k: v for k, v in ALL_VALUES.items() if k != slot}
    if absent_as == "sentinel":
        values[slot] = "__unset__"
    env = {**_env(tmp_path), "GECKO_HOST_SECRETS": "platform", **_platform(values)}
    with caplog.at_level(logging.DEBUG):
        assert _build(env) is None
    assert slot in caplog.text
    for value in ALL_VALUES.values():
        assert value not in caplog.text


def test_an_unknown_host_flag_is_no_mount_naming_the_flag(
    tmp_path: Path, keychain: FakeKeyring, caplog: pytest.LogCaptureFixture
) -> None:
    env = {**_env(tmp_path), "GECKO_HOST_SECRETS": "env", **_platform(ALL_VALUES)}
    with caplog.at_level(logging.INFO, logger="gecko.anyquery.hosted"):
        assert _build(env) is None
    assert "GECKO_HOST_SECRETS" in caplog.text


def test_a_dotenv_file_is_never_read(
    tmp_path: Path,
    keychain: FakeKeyring,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [f"{platform_env_name(s)}={v}" for s, v in ALL_VALUES.items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for slot in ALL_VALUES:
        monkeypatch.delenv(platform_env_name(slot), raising=False)
    env = {**_env(tmp_path), "GECKO_HOST_SECRETS": "platform"}
    with caplog.at_level(logging.INFO, logger="gecko.anyquery.hosted"):
        assert _build(env) is None
    assert "ANTHROPIC_API_KEY" in caplog.text


def test_sentinel_settings_are_unset_and_quiet(
    tmp_path: Path, keychain: FakeKeyring, caplog: pytest.LogCaptureFixture
) -> None:
    """A host provisions every parameter as ``__unset__`` before any-query is turned
    on; that boot must read as "not configured", at INFO, not as a bad setting."""
    env = {
        **_env(tmp_path),
        "GECKO_HOST_SECRETS": "platform",
        "GECKO_ANYQUERY_BILLING": "__unset__",
        "GECKO_FEE_WALLET": "__unset__",
        **_platform(ALL_VALUES),
    }
    with caplog.at_level(logging.INFO, logger="gecko.anyquery.hosted"):
        assert _build(env) is None
    assert caplog.records and all(r.levelno == logging.INFO for r in caplog.records)
    assert "must be 'fake' or 'solana'" not in caplog.text


# --- keychain -> SSM ----------------------------------------------------------


class FakeAws:
    def __init__(self, answers: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.answers = answers or {}

    def __call__(self, argv: list[str], stdin: str | None) -> tuple[int, str]:
        self.calls.append((argv, stdin))
        if argv[2] == "get-parameter":
            return self.answers.get(argv[argv.index("--name") + 1], (254, ""))
        return 0, "3\n"


def _keychain_resolver(held: dict[str, str]) -> ChainResolver:
    return ChainResolver([KeyringBackend(module=FakeKeyring(held))])


class FileReadingAws(FakeAws):
    """Reads ``--cli-input-json file://<path>`` during the call, as the real CLI does."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[str] = []
        self.paths: list[Path] = []
        self.modes: list[int] = []

    def __call__(self, argv: list[str], stdin: str | None) -> tuple[int, str]:
        source = argv[argv.index("--cli-input-json") + 1]
        assert source.startswith("file://") and source != "file:///dev/stdin"
        path = Path(source.removeprefix("file://"))
        self.paths.append(path)
        self.modes.append(path.stat().st_mode & 0o777)
        self.requests.append(path.read_text(encoding="utf-8"))
        return super().__call__(argv, stdin)


def test_push_hands_the_request_over_in_a_private_file_it_then_deletes() -> None:
    """Measured 10 Oct, aws-cli 2.35: ``file:///dev/stdin`` from a pipe reads as empty
    ("Invalid JSON received", exit 252), so every push failed."""
    aws = FileReadingAws()
    lines = push_slots(
        ["ANTHROPIC_API_KEY", "orthogonal"],
        region="us-east-2",
        resolver=_keychain_resolver(ALL_VALUES),
        run=aws,
    )
    assert lines == [
        "pushed /gecko-mcp/ANTHROPIC_API_KEY (version 3)",
        "pushed /gecko-mcp/ORTHOGONAL (version 3)",
    ]
    assert aws.modes == [0o600, 0o600]
    assert not any(path.exists() for path in aws.paths)
    assert all('"SecureString"' in request for request in aws.requests)
    assert SECRETS["orthogonal"] in aws.requests[1]
    for argv, _stdin in aws.calls:
        for value in ALL_VALUES.values():
            assert all(value not in arg for arg in argv)


def test_push_deletes_the_request_file_when_the_cli_fails() -> None:
    seen: list[Path] = []

    def failing(argv: list[str], stdin: str | None) -> tuple[int, str]:
        seen.append(
            Path(argv[argv.index("--cli-input-json") + 1].removeprefix("file://"))
        )
        raise OSError("aws not found")

    with pytest.raises(OSError):
        push_slots(
            ["ANTHROPIC_API_KEY"],
            region="us-east-2",
            resolver=_keychain_resolver(ALL_VALUES),
            run=failing,
        )
    assert seen and not seen[0].exists()


def test_push_refuses_before_writing_when_a_slot_is_missing() -> None:
    aws = FakeAws()
    with pytest.raises(HostSecretsError, match="HELIUS_API_KEY") as refused:
        push_slots(
            ["ANTHROPIC_API_KEY", "HELIUS_API_KEY"],
            region="us-east-2",
            resolver=_keychain_resolver(SECRETS),
            run=aws,
        )
    assert aws.calls == []
    assert SECRETS["ANTHROPIC_API_KEY"] not in str(refused.value)


def test_push_refuses_a_slot_it_does_not_know() -> None:
    with pytest.raises(HostSecretsError, match="TELEGRAM_PAYBOT_TOKEN"):
        push_slots(
            ["TELEGRAM_PAYBOT_TOKEN"],
            region="us-east-2",
            resolver=_keychain_resolver({"TELEGRAM_PAYBOT_TOKEN": "t"}),
            run=FakeAws(),
        )


def test_dry_run_writes_nothing() -> None:
    aws = FakeAws()
    lines = push_slots(
        ["MONGODB_URI"],
        region="us-east-2",
        resolver=_keychain_resolver(ALL_VALUES),
        run=aws,
        dry_run=True,
    )
    assert aws.calls == [] and lines == [
        "would push /gecko-mcp/MONGODB_URI (from keychain slot 'MONGODB_URI')"
    ]


def test_compare_says_same_different_or_absent_and_never_a_value() -> None:
    aws = FakeAws(
        {
            "/gecko-mcp/PRIVY_APP_ID": (0, SECRETS["PRIVY_APP_ID"] + "\n"),
            "/gecko-mcp/MONGODB_URI": (0, "mongodb+srv://someone-else\n"),
        }
    )
    lines = compare_slots(
        ["PRIVY_APP_ID", "MONGODB_URI", "ANTHROPIC_API_KEY"],
        region="us-east-2",
        resolver=_keychain_resolver(ALL_VALUES),
        run=aws,
    )
    assert lines == [
        "/gecko-mcp/PRIVY_APP_ID: same",
        "/gecko-mcp/MONGODB_URI: different",
        "/gecko-mcp/ANTHROPIC_API_KEY: absent or unreadable (aws exit 254)",
    ]
    assert all(stdin is None for _argv, stdin in aws.calls)


def test_ssm_names_keep_the_existing_parameters() -> None:
    assert ssm_parameter_name("MONGODB_URI") == "/gecko-mcp/MONGODB_URI"
    assert ssm_parameter_name("PRIVY_APP_SECRET") == "/gecko-mcp/PRIVY_APP_SECRET"
    assert ssm_parameter_name("orthogonal") == "/gecko-mcp/ORTHOGONAL"

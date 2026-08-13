"""``--store X`` must select X's ACCOUNTS, X's instruction argument and X's spend policy.

THE REGRESSION THIS ANCHORS IS MEASURED. On mainnet, ``--store geckocoffee`` set only the
instruction argument; ``receipts``, ``authority`` and the credited token account stayed on
the module constants of the store the script was written for, and so did the spend
policy's ``allowed_destinations``. The program refused it — ``AnchorError caused by
account: receipts ... ConstraintSeeds`` (2006), left ``H7Bj…``, right ``HVkb…``. The flag
read like a working control and failed at the last possible moment; the plan was already
wrong when it left the machine.

So these tests assert at the SCRIPT boundary, where the wiring lives. They drive
``main()`` and inspect what it handed onwards — the plan, the gate, and the bytes headed
for the builder — because a test of the resolver alone would prove the new function works
while saying nothing about whether the script calls it.

Three light fakes, no mocking library: a real keypair in a temp file, a fake node, and a
capture standing in for the step that would spend (``run_purchase`` in one script, the
builder request in the other). Nothing reaches a network; nothing is signed.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from gecko.autonomous_purchase import PurchasePlan, PurchaseRefused
from test_store_accounts import (
    GECKOCOFFEE_AUTHORITY,
    GECKOCOFFEE_RECEIPTS,
    GECKOCOFFEE_TOKEN_ACCOUNT,
    JONASBAR_AUTHORITY,
    JONASBAR_RECEIPTS,
    JONASBAR_TOKEN_ACCOUNT,
    USDC,
    node_with,
)

_REPO = Path(__file__).resolve().parents[1]


def _script(name: str) -> ModuleType:
    """``scripts/`` is not a package, so the module is loaded by path."""
    spec = importlib.util.spec_from_file_location(
        name, _REPO / "scripts" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def keypair_file(tmp_path: Path) -> Path:
    """A throwaway keypair, generated here. No key in the repo, none on a real network."""
    from solders.keypair import Keypair

    path = tmp_path / "buyer.json"
    path.write_text(json.dumps(list(bytes(Keypair()))))
    return path


class _CaptureRun:
    """Stands where the spend would happen: records what it was handed, refuses, returns."""

    def __init__(self) -> None:
        self.plan: PurchasePlan | None = None
        self.gate: Any = None

    def __call__(self, **kwargs: Any) -> PurchaseRefused:
        self.plan = kwargs["plan"]
        self.gate = kwargs["spend_gate"]
        return PurchaseRefused(
            code="plan-refused",
            reason="captured by the test before anything could be built or signed",
            network=kwargs["network"],
        )


def test_the_autonomous_run_carries_the_named_stores_accounts_not_the_defaults(
    keypair_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Name geckocoffee and every store-side address must be geckocoffee's."""
    module = _script("autonomous_purchase")
    capture = _CaptureRun()
    monkeypatch.setattr(module, "default_rpc_call", node_with("geckocoffee"))
    monkeypatch.setattr(module, "run_purchase", capture)

    code = module.main(
        [
            "--network",
            "fork",
            "--rpc-url",
            "http://127.0.0.1:8999",
            "--keypair",
            str(keypair_file),
            "--store",
            "geckocoffee",
            "--product",
            "Espresso",
            "--ledger",
            ":memory:",
        ]
    )

    assert code == 1  # the captured refusal, not a settlement
    plan = capture.plan
    assert plan is not None
    accounts = plan.accounts
    assert accounts["receipts"] == GECKOCOFFEE_RECEIPTS
    assert accounts["authority"] == GECKOCOFFEE_AUTHORITY
    assert accounts["recipient_token_account"] == GECKOCOFFEE_TOKEN_ACCOUNT
    assert plan.args["store_name"] == "geckocoffee"
    assert plan.args["product_name"] == "Espresso"
    # None of the store the script used to hardcode survives anywhere in the plan.
    for stale in (JONASBAR_RECEIPTS, JONASBAR_AUTHORITY, JONASBAR_TOKEN_ACCOUNT):
        assert stale not in accounts.values()

    # THE THIRD PLACE THE FLAG HAS TO REACH. A policy allowlisting another store's
    # accounts permits the wrong payee while refusing the right one.
    allowed = capture.gate.policy.allowed_destinations
    assert GECKOCOFFEE_RECEIPTS in allowed
    assert GECKOCOFFEE_AUTHORITY in allowed
    assert GECKOCOFFEE_TOKEN_ACCOUNT in allowed
    assert JONASBAR_TOKEN_ACCOUNT not in allowed
    assert (
        accounts["sender_token_account"] in allowed
    )  # the buyer's own, for the refund


def test_the_default_store_is_still_the_store_it_always_was(
    keypair_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saying nothing must keep meaning jonasbar — a fix that moves the default is a break."""
    module = _script("autonomous_purchase")
    capture = _CaptureRun()
    monkeypatch.setattr(module, "default_rpc_call", node_with("jonasbar"))
    monkeypatch.setattr(module, "run_purchase", capture)

    module.main(
        [
            "--network",
            "fork",
            "--rpc-url",
            "http://127.0.0.1:8999",
            "--keypair",
            str(keypair_file),
            "--ledger",
            ":memory:",
        ]
    )

    assert capture.plan is not None
    assert capture.plan.accounts["receipts"] == JONASBAR_RECEIPTS
    assert capture.plan.accounts["authority"] == JONASBAR_AUTHORITY
    assert capture.plan.accounts["recipient_token_account"] == JONASBAR_TOKEN_ACCOUNT
    assert capture.plan.args["store_name"] == "jonasbar"


def test_self_transfer_still_binds_the_buyers_own_account_as_recipient(
    keypair_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo of the plan refusal must survive the fix, or the refusal loses its proof."""
    module = _script("autonomous_purchase")
    capture = _CaptureRun()
    monkeypatch.setattr(module, "default_rpc_call", node_with("geckocoffee"))
    monkeypatch.setattr(module, "run_purchase", capture)

    module.main(
        [
            "--network",
            "fork",
            "--rpc-url",
            "http://127.0.0.1:8999",
            "--keypair",
            str(keypair_file),
            "--store",
            "geckocoffee",
            "--product",
            "Espresso",
            "--self-transfer",
            "--ledger",
            ":memory:",
        ]
    )

    assert capture.plan is not None
    accounts = capture.plan.accounts
    assert accounts["recipient_token_account"] == accounts["sender_token_account"]
    assert accounts["recipient_token_account"] != GECKOCOFFEE_TOKEN_ACCOUNT
    # Still THIS store's receipts: the demo swaps the payee, not the storefront.
    assert accounts["receipts"] == GECKOCOFFEE_RECEIPTS


def test_an_unknown_store_never_reaches_the_builder(
    keypair_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence is a refusal. A store nobody deployed must stop before anything is built."""
    module = _script("autonomous_purchase")
    capture = _CaptureRun()
    monkeypatch.setattr(module, "default_rpc_call", node_with("geckocoffee"))
    monkeypatch.setattr(module, "run_purchase", capture)

    code = module.main(
        [
            "--network",
            "fork",
            "--rpc-url",
            "http://127.0.0.1:8999",
            "--keypair",
            str(keypair_file),
            "--store",
            "nosuchbar",
            "--ledger",
            ":memory:",
        ]
    )

    assert code == 2
    assert capture.plan is None  # nothing was planned, so nothing could be signed


def test_a_product_that_store_does_not_sell_never_reaches_the_builder(
    keypair_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _script("autonomous_purchase")
    capture = _CaptureRun()
    monkeypatch.setattr(module, "default_rpc_call", node_with("geckocoffee"))
    monkeypatch.setattr(module, "run_purchase", capture)

    code = module.main(
        [
            "--network",
            "fork",
            "--rpc-url",
            "http://127.0.0.1:8999",
            "--keypair",
            str(keypair_file),
            "--store",
            "geckocoffee",
            "--product",
            "Jägermeister",  # jonasbar sells it; this store does not
            "--ledger",
            ":memory:",
        ]
    )

    assert code == 2
    assert capture.plan is None


# -- the keyless pre-flight, same rule --------------------------------------------------


class _CaptureBuild:
    """Stands in for the builder: records the request body, answers with no transaction."""

    def __init__(self) -> None:
        self.body: dict[str, Any] = {}

    def __call__(self, request: urllib.request.Request, **_kwargs: object) -> Any:
        self.body = json.loads(request.data or b"{}")

        class _Response:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def read(self) -> bytes:
                return b"{}"  # no serializedTransaction: the run stops right after

        return _Response()


def _node_with_balance(store: str) -> Any:
    """The store's account, plus a funded token account for the buyer."""
    store_node = node_with(store)

    def call(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getTokenAccountBalance":
            return {"result": {"value": {"uiAmount": 0.5}}}
        return store_node(url, method, params)

    return call


def test_the_preflight_sends_the_named_stores_accounts_to_the_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``prepare_purchase.py`` grew a ``--store`` flag; it must select the accounts too."""
    module = _script("prepare_purchase")
    build = _CaptureBuild()
    monkeypatch.setattr(module, "default_rpc_call", _node_with_balance("geckocoffee"))
    monkeypatch.setattr(urllib.request, "urlopen", build)

    module.main(
        [
            "--signer",
            "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi",
            "--rpc-url",
            "http://node",
            "--network",
            "mainnet",
            "--store",
            "geckocoffee",
            "--product",
            "Espresso",
        ]
    )

    accounts = build.body["accounts"]
    assert accounts["receipts"] == GECKOCOFFEE_RECEIPTS
    assert accounts["authority"] == GECKOCOFFEE_AUTHORITY
    assert accounts["recipient_token_account"] == GECKOCOFFEE_TOKEN_ACCOUNT
    assert accounts["mint"] == USDC
    assert build.body["args"]["store_name"] == "geckocoffee"
    for stale in (JONASBAR_RECEIPTS, JONASBAR_AUTHORITY, JONASBAR_TOKEN_ACCOUNT):
        assert stale not in accounts.values()


def test_the_preflights_default_store_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented invocation has no ``--store``; it must still mean jonasbar/Water."""
    module = _script("prepare_purchase")
    build = _CaptureBuild()
    monkeypatch.setattr(module, "default_rpc_call", _node_with_balance("jonasbar"))
    monkeypatch.setattr(urllib.request, "urlopen", build)

    module.main(
        [
            "--signer",
            "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi",
            "--rpc-url",
            "http://node",
            "--network",
            "mainnet",
        ]
    )

    accounts = build.body["accounts"]
    assert accounts["receipts"] == JONASBAR_RECEIPTS
    assert accounts["authority"] == JONASBAR_AUTHORITY
    assert accounts["recipient_token_account"] == JONASBAR_TOKEN_ACCOUNT
    assert build.body["args"] == {
        "store_name": "jonasbar",
        "product_name": "Water",
        "table_number": 11,
    }


def test_the_preflight_refuses_an_unknown_store_before_the_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _script("prepare_purchase")
    build = _CaptureBuild()
    monkeypatch.setattr(module, "default_rpc_call", _node_with_balance("jonasbar"))
    monkeypatch.setattr(urllib.request, "urlopen", build)

    code = module.main(
        [
            "--signer",
            "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi",
            "--rpc-url",
            "http://node",
            "--network",
            "mainnet",
            "--store",
            "nosuchbar",
        ]
    )

    assert code == 2
    assert build.body == {}  # the builder was never asked

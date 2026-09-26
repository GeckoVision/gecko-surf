"""POST /telegram/webhook — the chat door, falsified with no network and no bot.

Pattern B: the reply sender is injected (a list), the engine is either a light fake or
the REAL ``OrquestraCatalogSurface`` with an injected RPC, and nothing here touches a
socket. What the file is really asserting, in order of how much it would cost to get
wrong:

1. **Fail closed on the secret.** No ``TELEGRAM_PAYBOT_WEBHOOK`` ⇒ no route at all
   (404), never an open one. A wrong/absent header ⇒ 403 before the body is read.
2. **Control plane (invariant #1).** A message's text, the sender's name and the chat
   id reach no log, no store, and no field on the webhook object. Asserted against the
   captured log records, because "we did not write it down" is exactly the kind of
   promise that decays silently.
3. **A refusal is not an error.** ``prepare_purchase`` refusing and Gecko breaking must
   read differently to the person in the chat.
4. **Nothing signs.** A prepared purchase comes back as unsigned bytes + a statement.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from gecko import telegram_api  # noqa: E402
from gecko.http_server import build_multi_surface_app  # noqa: E402
from gecko.providers.catalog_surface import OrquestraCatalogSurface  # noqa: E402
from gecko.store_directory import (  # noqa: E402
    LET_ME_BUY_PROGRAM_ID,
    StoreListing,
    StoreProduct,
    encode_store,
)
from gecko.telegram_webhook import (  # noqa: E402
    MAX_UPDATE_BYTES,
    SECRET_HEADER,
    WEBHOOK_PATH,
    WEBHOOK_SECRET_ENV,
    build_telegram_webhook,
    resolve_webhook_secret,
    telegram_routes,
)

PEGANA = "tests/fixtures/pegana_openapi.json"
SECRET = "a-long-shared-secret-value"
BUYER = "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
AUTHORITY = "DMjTEZJuV3mpfzBNeeuFy9m47A1bj5CXVhCNVo7BEPzy"

# Exactly what Telegram sends for one text message, including the fields we must NOT
# touch — a username and a first name are the PII this route has to stay ignorant of.
SECRET_TEXT = "buy espresso from geckocoffee"
USERNAME = "quietbuyer99"


def update(text: str, *, chat_id: int = 99001) -> dict[str, Any]:
    return {
        "update_id": 10_000,
        "message": {
            "message_id": 7,
            "from": {
                "id": chat_id,
                "is_bot": False,
                "first_name": "Ana",
                "username": USERNAME,
            },
            "chat": {"id": chat_id, "type": "private", "username": USERNAME},
            "date": 1_759_000_000,
            "text": text,
        },
    }


class FakeEngine:
    """A surface that records what it was asked and answers from a script."""

    def __init__(self, answers: dict[str, Any] | None = None, raises: bool = False):
        self.answers = answers or {}
        self.raises = raises
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        if self.raises:
            raise TimeoutError("upstream node went away")
        return self.answers.get(name, {})


class Outbox:
    """The injected reply sender. A list, not a mock."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def __call__(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


MENU = {
    "program": LET_ME_BUY_PROGRAM_ID,
    "stores": [
        {
            "store": "geckocoffee",
            "address": "Addr111",
            "authority": AUTHORITY,
            "total_purchases": 2,
            "fulfilment": {"telegram_channel_id": "-100123", "set": True},
            "products": [
                {
                    "name": "Espresso",
                    "price_raw": 100_000,
                    "decimals": 6,
                    "price_ui": "0.1",
                    "mint": USDC,
                    "mint_note": "USDC",
                }
            ],
        }
    ],
    "skipped_undecodable": 0,
}

PREPARED = {
    "network": "mainnet",
    "effects": {
        "fee_payer": BUYER,
        "programs": [LET_ME_BUY_PROGRAM_ID],
        "origin": "measured",
        "tokens_out": [
            {
                "mint": USDC,
                "owner": BUYER,
                "amount": "0.1",
                "amount_raw": "100000",
                "decimals": 6,
            }
        ],
    },
    "transaction": {
        "signed": False,
        "encoding": "base64",
        "unsigned_transaction": "AQIDBAUGBwgJ" * 8,
        "who_signs": "you do, in your own wallet — Gecko holds no key",
    },
    "submit": {"rpc_url": "https://api.mainnet-beta.solana.com"},
    "expires": {
        "blocks_remaining": 148,
        "seconds_remaining_estimate": 59,
        "last_valid_block_height": 1_000,
    },
}

REFUSED = {
    "refused": True,
    "code": "store-not-found",
    "reason": "no store named 'geckocoffee' exists on mainnet",
}


def app_with(routes: list[Any]) -> Any:
    return build_multi_surface_app(
        [("pegana", PEGANA)], allowed_hosts=["testserver"], extra_routes=routes
    )


def post(client: TestClient, body: Any, *, secret: str | None = SECRET) -> Any:
    headers = {} if secret is None else {SECRET_HEADER: secret}
    if isinstance(body, (bytes, str)):
        return client.post(WEBHOOK_PATH, content=body, headers=headers)
    return client.post(WEBHOOK_PATH, json=body, headers=headers)


# --- 1. fail closed on the secret ------------------------------------------- #


def test_no_secret_means_no_route_at_all(monkeypatch) -> None:
    monkeypatch.delenv(WEBHOOK_SECRET_ENV, raising=False)
    monkeypatch.delenv(telegram_api.BOT_TOKEN_ENV, raising=False)
    routes = telegram_routes(FakeEngine(), sender=Outbox())
    assert routes == []
    with TestClient(app_with(routes)) as client:
        # 404, the same answer a path that was never registered gives. An unset secret
        # cannot be read as "allow everyone" because there is nothing to be permissive at.
        assert client.post(WEBHOOK_PATH, json=update("stores")).status_code == 404


def test_sentinel_secret_is_unset(monkeypatch) -> None:
    # `__unset__` is a literal in a PUBLIC repo; honouring it would make the placeholder
    # that keeps the door shut the key that opens it.
    monkeypatch.setenv(WEBHOOK_SECRET_ENV, telegram_api.SENTINEL)
    assert resolve_webhook_secret() is None
    assert telegram_routes(FakeEngine(), sender=Outbox()) == []


def test_secret_without_a_way_to_reply_does_not_mount(monkeypatch) -> None:
    # A route that accepts updates and answers nobody looks healthy and is not.
    monkeypatch.delenv(telegram_api.BOT_TOKEN_ENV, raising=False)
    assert build_telegram_webhook(FakeEngine(), secret=SECRET) is None


def test_no_engine_means_no_route() -> None:
    # A webhook with nothing behind it can only answer errors, which reads to a person
    # as "the bot is broken" rather than "the host is misconfigured".
    assert build_telegram_webhook(None, secret=SECRET, sender=Outbox()) is None
    assert telegram_routes(None, secret=SECRET, sender=Outbox()) == []


def test_wrong_secret_is_refused_before_the_body_is_read() -> None:
    engine, outbox = FakeEngine(), Outbox()
    routes = telegram_routes(engine, secret=SECRET, sender=outbox)
    with TestClient(app_with(routes)) as client:
        assert post(client, update("stores"), secret="not-it").status_code == 403
        assert post(client, update("stores"), secret=None).status_code == 403
        assert post(client, update("stores"), secret=SECRET[:-1]).status_code == 403
    assert engine.calls == []  # nothing was parsed, nothing was called
    assert outbox.sent == []


# --- 2. control plane -------------------------------------------------------- #


def test_nothing_from_the_message_is_logged_or_kept(caplog) -> None:
    engine = FakeEngine({"prepare_purchase": PREPARED, "list_stores": MENU})
    outbox = Outbox()
    webhook = build_telegram_webhook(engine, secret=SECRET, sender=outbox)
    assert webhook is not None
    with caplog.at_level(logging.DEBUG):
        assert webhook.send(update(SECRET_TEXT)) is True
        assert webhook.send(update("stores")) is True
    logged = caplog.text
    assert SECRET_TEXT not in logged
    assert USERNAME not in logged
    assert "99001" not in logged  # the chat id is not an identifier we write down
    # The object carries no history: three declared fields, and none of them grew.
    assert set(vars(webhook)) == {"engine", "secret", "sender"}
    assert vars(webhook)["engine"] is engine


def test_the_webhook_holds_nothing_between_updates() -> None:
    engine = FakeEngine({"list_stores": MENU})
    webhook = build_telegram_webhook(engine, secret=SECRET, sender=Outbox())
    assert webhook is not None
    before = dict(vars(webhook))
    webhook.send(update("stores", chat_id=1))
    webhook.send(update("stores", chat_id=2))
    assert dict(vars(webhook)) == before


# --- 3. the three answers: menu, refusal, error ------------------------------ #


def test_browse_calls_list_stores_and_renders_the_menu() -> None:
    engine = FakeEngine({"list_stores": MENU})
    outbox = Outbox()
    routes = telegram_routes(engine, secret=SECRET, sender=outbox)
    with TestClient(app_with(routes)) as client:
        response = post(client, update("stores"))
    assert response.status_code == 200
    assert response.json() == {"ok": True, "replied": True}
    assert engine.calls == [("list_stores", {})]
    chat_id, reply = outbox.sent[0]
    assert chat_id == 99001
    assert "geckocoffee" in reply and "Espresso" in reply and "0.1" in reply


def test_a_product_question_filters_the_menu() -> None:
    engine = FakeEngine({"list_stores": MENU})
    webhook = build_telegram_webhook(engine, secret=SECRET, sender=Outbox())
    assert webhook is not None
    webhook.send(update("how much is espresso"))
    assert engine.calls == [("list_stores", {"product": "espresso"})]


def test_a_refusal_reads_as_an_answer_not_a_failure() -> None:
    engine = FakeEngine({"prepare_purchase": REFUSED})
    outbox = Outbox()
    webhook = build_telegram_webhook(engine, secret=SECRET, sender=outbox)
    assert webhook is not None
    webhook.send(update(f"buy espresso from geckocoffee for {BUYER}"))
    _, reply = outbox.sent[0]
    assert "store-not-found" in reply
    assert "no store named" in reply
    # The words that would make a correct refusal look like a bug.
    assert "failed on my side" not in reply


def test_an_engine_exception_reads_as_our_failure() -> None:
    engine = FakeEngine(raises=True)
    outbox = Outbox()
    webhook = build_telegram_webhook(engine, secret=SECRET, sender=outbox)
    assert webhook is not None
    webhook.send(update("stores"))
    _, reply = outbox.sent[0]
    assert "failed on my side" in reply
    assert "TimeoutError" in reply  # the class, never the upstream message
    assert "went away" not in reply


def test_an_unreadable_message_is_refused_with_the_shapes_that_work() -> None:
    engine = FakeEngine()
    outbox = Outbox()
    webhook = build_telegram_webhook(engine, secret=SECRET, sender=outbox)
    assert webhook is not None
    webhook.send(update("what is the weather in Lisbon"))
    _, reply = outbox.sent[0]
    assert "refusal, not a failure" in reply
    assert engine.calls == []  # an unreadable message costs no engine call


# --- 4. the purchase path: unsigned, and never a signature ------------------- #


def test_a_complete_buy_prepares_unsigned_bytes() -> None:
    engine = FakeEngine({"prepare_purchase": PREPARED})
    outbox = Outbox()
    webhook = build_telegram_webhook(engine, secret=SECRET, sender=outbox)
    assert webhook is not None
    webhook.send(update(f"buy Espresso from geckocoffee for {BUYER}"))
    assert engine.calls == [
        (
            "prepare_purchase",
            {"store": "geckocoffee", "product": "Espresso", "buyer": BUYER},
        )
    ]
    _, reply = outbox.sent[0]
    assert "Nothing is signed" in reply
    assert "0.1" in reply  # what would move
    assert PREPARED["transaction"]["unsigned_transaction"] in reply  # type: ignore[index]
    assert "Gecko holds no key" in reply
    assert "59s" in reply  # the expiry budget, not prose


def test_a_buy_with_no_wallet_refuses_before_the_clock_starts() -> None:
    engine = FakeEngine({"prepare_purchase": PREPARED})
    outbox = Outbox()
    webhook = build_telegram_webhook(engine, secret=SECRET, sender=outbox)
    assert webhook is not None
    webhook.send(update("buy espresso from geckocoffee"))
    # No call at all: `prepare_purchase` opens a ~60s blockhash window, and spending it
    # to discover the wallet was missing is the expensive way to find out.
    assert engine.calls == []
    _, reply = outbox.sent[0]
    assert "wallet address" in reply
    assert "holds no key" in reply


def test_a_buy_with_no_store_names_what_is_missing() -> None:
    engine = FakeEngine()
    outbox = Outbox()
    webhook = build_telegram_webhook(engine, secret=SECRET, sender=outbox)
    assert webhook is not None
    webhook.send(update(f"buy espresso {BUYER}"))
    assert engine.calls == []
    _, reply = outbox.sent[0]
    assert "store" in reply and "buy <product> from <store>" in reply


def test_oversized_unsigned_bytes_are_withheld_rather_than_truncated() -> None:
    huge = dict(PREPARED)
    huge["transaction"] = dict(PREPARED["transaction"], unsigned_transaction="A" * 5000)  # type: ignore[arg-type]
    engine = FakeEngine({"prepare_purchase": huge})
    outbox = Outbox()
    webhook = build_telegram_webhook(engine, secret=SECRET, sender=outbox)
    assert webhook is not None
    webhook.send(update(f"buy espresso from geckocoffee for {BUYER}"))
    _, reply = outbox.sent[0]
    assert len(reply) <= 4096
    assert "A" * 5000 not in reply
    # A partial copy of bytes somebody would sign is worse than none.
    assert "too long for one chat message" in reply


# --- the wire: bad bodies, wrong shapes ------------------------------------- #


def test_an_oversized_body_is_refused() -> None:
    engine = FakeEngine()
    routes = telegram_routes(engine, secret=SECRET, sender=Outbox())
    with TestClient(app_with(routes)) as client:
        response = post(client, b"{" + b"x" * (MAX_UPDATE_BYTES + 10))
    assert response.status_code == 413
    assert engine.calls == []


def test_junk_json_is_a_400_and_never_a_500() -> None:
    routes = telegram_routes(FakeEngine(), secret=SECRET, sender=Outbox())
    with TestClient(app_with(routes)) as client:
        assert post(client, b"not json at all").status_code == 400
        assert post(client, b"[1,2,3]").status_code == 400
        assert post(client, b"").status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {"update_id": 1},  # no message
        {"update_id": 1, "message": {"chat": {"id": 5}}},  # no text
        {"update_id": 1, "message": {"text": "stores"}},  # no chat
        {"update_id": 1, "message": {"chat": {"id": True}, "text": "hi"}},  # bool != id
        {"update_id": 1, "edited_message": {"chat": {"id": 5}, "text": "stores"}},
        {"update_id": 1, "message": {"chat": {"id": 5}, "photo": []}},
    ],
)
def test_an_update_with_nothing_to_answer_is_accepted_and_ignored(body: Any) -> None:
    engine, outbox = FakeEngine(), Outbox()
    routes = telegram_routes(engine, secret=SECRET, sender=outbox)
    with TestClient(app_with(routes)) as client:
        response = post(client, body)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "replied": False}
    assert engine.calls == [] and outbox.sent == []


def test_a_send_failure_still_answers_200() -> None:
    # A non-2xx makes Telegram redeliver the same update and re-run the engine; a retry
    # storm is not a recovery.
    def explode(chat_id: int, text: str) -> None:
        raise telegram_api.TelegramApiError("sendMessage failed: URLError")

    routes = telegram_routes(
        FakeEngine({"list_stores": MENU}), secret=SECRET, sender=explode
    )
    with TestClient(app_with(routes)) as client:
        response = post(client, update("stores"))
    assert response.status_code == 200
    assert response.json() == {"ok": True, "replied": False}


# --- the REAL engine, offline: wired is not the same as reaches -------------- #


def _store_account() -> dict[str, Any]:
    listing = StoreListing(
        store_name="geckocoffee",
        address="Addr111",
        authority=AUTHORITY,
        total_purchases=2,
        products=(
            StoreProduct(name="Espresso", price_raw=100_000, decimals=6, mint=USDC),
        ),
        telegram_channel_id="",
    )
    raw = encode_store(listing)
    return {
        "pubkey": "Addr111",
        "account": {"data": [base64.b64encode(raw).decode(), "base64"]},
    }


def test_a_chat_message_reaches_the_real_catalog_surface() -> None:
    """One browse, through the surface the MCP mounts serve, with an injected RPC.

    The point is that the chat is not a second comprehension: the bytes come from
    ``encode_store`` in the program's own layout and the answer is decoded by the same
    ``list_stores`` an agent calls.
    """
    calls: list[str] = []

    def fake_rpc(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        calls.append(method)
        if method == "getProgramAccounts":
            return {"result": [_store_account()]}
        return {"result": {"value": []}}

    engine = OrquestraCatalogSurface(find_start_pages=0, purchase_rpc_call=fake_rpc)
    outbox = Outbox()
    webhook = build_telegram_webhook(engine, secret=SECRET, sender=outbox)
    assert webhook is not None
    webhook.send(update("stores"))
    assert "getProgramAccounts" in calls
    _, reply = outbox.sent[0]
    assert "geckocoffee" in reply
    assert "Espresso" in reply
    # The store has no delivery channel, and that is said BEFORE anyone pays.
    assert "no delivery channel set" in reply


def test_the_env_alone_mounts_the_route_and_replies(monkeypatch) -> None:
    """The plumbing the deploy actually uses: two env vars, no injection.

    Every other test here injects a sender, which proves the logic and not the wiring.
    This one sets only ``TELEGRAM_PAYBOT_WEBHOOK`` + ``TELEGRAM_PAYBOT_TOKEN`` — what SSM
    provides — and asserts a real ``sendMessage`` was assembled. The POST helper is
    replaced, so no packet leaves.
    """
    monkeypatch.setenv(WEBHOOK_SECRET_ENV, SECRET)
    monkeypatch.setenv(telegram_api.BOT_TOKEN_ENV, "999:bearer-credential")
    posted: list[dict[str, Any]] = []

    def capture(url: str, body: bytes) -> dict[str, Any]:
        posted.append({"url": url, "body": json.loads(body)})
        return {"ok": True, "result": {}}

    monkeypatch.setattr(telegram_api, "post_json", capture)
    routes = telegram_routes(FakeEngine({"list_stores": MENU}))
    assert len(routes) == 1
    with TestClient(app_with(routes)) as client:
        assert post(client, update("stores")).status_code == 200
    assert posted[0]["url"].endswith("/sendMessage")
    assert "geckocoffee" in posted[0]["body"]["text"]


# --- the bot token is a bearer credential ----------------------------------- #


def test_a_send_failure_never_carries_the_token(monkeypatch) -> None:
    token = "123456:AA-this-is-a-bearer-credential"

    def boom(url: str, body: bytes) -> dict[str, Any]:
        raise OSError(f"connection refused for {url}")

    monkeypatch.setattr(telegram_api, "post_json", boom)
    with pytest.raises(telegram_api.TelegramApiError) as caught:
        telegram_api.send_message(token, 1, "hello")
    assert token not in str(caught.value)
    assert "api.telegram.org" not in str(caught.value)
    assert "OSError" in str(caught.value)


def test_only_sendmessage_can_be_called() -> None:
    # The method is the one path segment we assemble; an open set would make this a
    # generic proxy onto somebody's bot, including setWebhook.
    with pytest.raises(telegram_api.TelegramApiError):
        telegram_api._method_url("token", "setWebhook")


def test_the_sender_posts_plain_text_to_the_fixed_host(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def capture(url: str, body: bytes) -> dict[str, Any]:
        seen["url"] = url
        seen["body"] = json.loads(body)
        return {"ok": True, "result": {}}

    monkeypatch.setattr(telegram_api, "post_json", capture)
    telegram_api.send_message("tok", 42, "hello")
    assert seen["url"].startswith(telegram_api.TELEGRAM_API_HOST + "/bottok/")
    assert seen["body"]["chat_id"] == 42
    # No parse_mode: a store name read off a chain must not be able to restyle — or
    # break — a message about money.
    assert "parse_mode" not in seen["body"]

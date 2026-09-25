"""``POST /telegram/webhook`` — a person in a chat, talking to the engine.

This is the whole Telegram surface: verify the shared secret, read one update, route
the text through :mod:`gecko.telegram_intent` into the tools that already exist
(``list_stores`` / ``prepare_purchase``), render the answer with
:mod:`gecko.telegram_reply`, and send it back. There is no state between updates.

**CONTROL PLANE (invariant #1), stated precisely, because a Telegram message IS user
data.** For the duration of one request this module holds, as local variables: the
request body (capped at :data:`MAX_UPDATE_BYTES`), the parsed update, the chat id it
must reply to, and the message text. All four go out of scope when the handler
returns. Nothing is written to a file, a database, an event, or a module-level dict;
there is no per-user map, no session, no cache, and no conversation history — which is
also why the grammar is one-shot ("buy X from Y for <address>") rather than a dialogue
that would need to remember the last turn. The log lines carry the intent KIND and a
status, never the text, the username, or the chat id.

**Gecko never signs and never holds a key.** The purchase path ends at unsigned bytes
plus what we can say about them; the buyer is an address the person typed, because
there is no wallet here to look one up in.

**Fail closed on the secret.** ``TELEGRAM_WEBHOOK_SECRET`` unset means the route is not
mounted at all — :func:`telegram_routes` returns an empty list, exactly as
``build_course_surface`` returns ``None`` rather than mounting an empty corpus. An
unset secret can therefore never be read as "allow everyone", because there is no door
to be permissive at. With a secret set, a request whose
``X-Telegram-Bot-Api-Secret-Token`` does not match is refused before the body is
parsed, compared with :func:`hmac.compare_digest`.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping

from .telegram_api import SENTINEL, Sender, resolve_bot_token, sender_from_token
from .telegram_intent import Intent, parse_intent
from .telegram_reply import (
    CANNOT_ANSWER,
    NEED_BUYER,
    NEED_PRODUCT,
    NEED_STORE,
    browse_text,
    error_text,
    help_text,
    purchase_text,
)

__all__ = [
    "MAX_UPDATE_BYTES",
    "SECRET_HEADER",
    "WEBHOOK_PATH",
    "WEBHOOK_SECRET_ENV",
    "TelegramWebhook",
    "build_telegram_webhook",
    "chat_and_text",
    "resolve_webhook_secret",
    "telegram_routes",
]

logger = logging.getLogger("gecko.telegram_webhook")

WEBHOOK_PATH = "/telegram/webhook"

#: Telegram sends the secret in this header on every update (set once, with
#: ``setWebhook``). Lowercase because that is how ASGI/Starlette normalises it.
SECRET_HEADER = "x-telegram-bot-api-secret-token"

WEBHOOK_SECRET_ENV = "TELEGRAM_WEBHOOK_SECRET"

#: An update is a small JSON envelope. Capped before parsing because this is an
#: unauthenticated-until-the-header-checks door on a public host.
MAX_UPDATE_BYTES = 64 * 1024

#: The engine tools this surface may call. A closed set: the text a stranger types must
#: never be able to choose which tool runs, only which of these two.
_BROWSE_TOOL = "list_stores"
_PURCHASE_TOOL = "prepare_purchase"


def resolve_webhook_secret(env: Mapping[str, str] | None = None) -> str | None:
    """The shared secret, or ``None`` when it is genuinely unset.

    Empty, whitespace and the SSM ``__unset__`` sentinel all mean unset. The sentinel
    matters more here than almost anywhere: it is a literal in a PUBLIC repository, so
    honouring it as a real secret would turn the placeholder that keeps the door shut
    into the key that opens it — the same reasoning ``GECKO_SERVABLE_TOKEN`` carries.
    """
    source: Mapping[str, str] = env if env is not None else os.environ
    raw = (source.get(WEBHOOK_SECRET_ENV) or "").strip()
    if not raw or raw == SENTINEL:
        return None
    return raw


def chat_and_text(update: Mapping[str, Any]) -> tuple[int, str] | None:
    """``(chat_id, text)`` from an update, or ``None`` when there is nothing to answer.

    UNTRUSTED input: every field is checked for type rather than assumed. Only a plain
    ``message`` with text is handled — an edit, a channel post, a callback, a photo, a
    join event and a poll all return ``None``, which the route answers with a 200 and
    no reply. Ignoring an update we do not understand is correct; guessing at it would
    mean answering a person who did not ask.
    """
    message = update.get("message")
    if not isinstance(message, Mapping):
        return None
    chat = message.get("chat")
    if not isinstance(chat, Mapping):
        return None
    chat_id = chat.get("id")
    text = message.get("text")
    # `bool` is an int in Python; a chat id that is `True` is not a chat id.
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    return (chat_id, text)


@dataclass
class TelegramWebhook:
    """One update in, one reply out — with no memory of either.

    ``engine`` is any duck-typed surface with ``call_tool(name, arguments)``; the hosted
    host passes the ``OrquestraCatalogSurface`` it already serves over MCP, so the chat
    and the MCP client reach the SAME comprehension with the same refusals. ``sender``
    is the reply seam (:data:`gecko.telegram_api.Sender`), injected so the whole path
    runs offline.
    """

    engine: Any
    secret: str
    sender: Sender

    def verify(self, header_value: str | None) -> bool:
        """Does this request carry our secret? Constant-time, and false for absent.

        There is no "no secret configured" branch: this object cannot be built without
        one (:func:`build_telegram_webhook`), so the only way to be permissive here
        would be to delete the comparison.
        """
        if not isinstance(header_value, str) or not header_value:
            return False
        return hmac.compare_digest(header_value, self.secret)

    def answer(self, text: str) -> str:
        """The reply for one message. Pure apart from the engine call; never raises.

        An engine failure becomes an ERROR message and a refusal becomes a refusal —
        :mod:`gecko.telegram_reply` keeps them apart, because "the store does not
        exist" and "Gecko broke" ask different things of the reader.
        """
        intent = parse_intent(text)
        if intent.kind == "help":
            return help_text()
        if intent.kind == "unknown":
            return CANNOT_ANSWER
        if intent.kind in ("browse", "product"):
            return self._browse(intent)
        return self._buy(intent)

    def handle(self, update: Mapping[str, Any]) -> tuple[int, str] | None:
        """``(chat_id, reply)`` for an update, or ``None`` when there is nothing to say.

        Split from :meth:`send` so the decision (what to answer) is testable without a
        transport, and so the route can keep the reply out of its own log line.
        """
        parsed = chat_and_text(update)
        if parsed is None:
            return None
        chat_id, text = parsed
        return (chat_id, self.answer(text))

    def send(self, update: Mapping[str, Any]) -> bool:
        """Answer an update. ``True`` when a reply was sent.

        The chat id and the text exist only inside this call. A send failure is logged
        by CLASS and swallowed: the route answers 200 either way, because a non-2xx
        makes Telegram redeliver the same update and re-run the engine, which is a
        retry storm rather than a recovery.
        """
        answered = self.handle(update)
        if answered is None:
            return False
        chat_id, reply = answered
        try:
            self.sender(chat_id, reply)
        except Exception as exc:  # noqa: BLE001 - never echo a chat id or a token
            logger.warning("telegram reply not delivered (%s)", type(exc).__name__)
            return False
        return True

    # -- the two engine paths -------------------------------------------------

    def _call(self, tool: str, arguments: dict[str, Any]) -> Mapping[str, Any] | str:
        """Call one of the two allowed tools. Returns the result, or an error STRING
        when the call itself blew up — the surfaces promise a structured refusal for
        every expected answer, so an exception here really is our failure."""
        if tool not in (_BROWSE_TOOL, _PURCHASE_TOOL):  # pragma: no cover - closed set
            raise ValueError(f"tool {tool!r} is not reachable from a chat message")
        try:
            result = self.engine.call_tool(tool, arguments)
        except Exception as exc:  # noqa: BLE001 - a chat must not 500; name the class
            logger.warning("engine call failed tool=%s (%s)", tool, type(exc).__name__)
            return error_text(type(exc).__name__)
        if not isinstance(result, Mapping):
            logger.warning("engine returned a non-mapping for tool=%s", tool)
            return error_text("UnreadableResult")
        return result

    def _browse(self, intent: Intent) -> str:
        arguments: dict[str, Any] = {}
        if intent.product:
            arguments["product"] = intent.product
        if intent.store:
            arguments["store"] = intent.store
        result = self._call(_BROWSE_TOOL, arguments)
        if isinstance(result, str):
            return result
        return browse_text(result, filtered=bool(arguments))

    def _buy(self, intent: Intent) -> str:
        # Every missing piece is named before anything is prepared: `prepare_purchase`
        # starts a ~60-second clock on a live blockhash, and spending that window to
        # learn the store was missing is the expensive way to find out.
        if not intent.product:
            return NEED_PRODUCT
        if not intent.store:
            return NEED_STORE
        if not intent.buyer:
            return NEED_BUYER
        result = self._call(
            _PURCHASE_TOOL,
            {"store": intent.store, "product": intent.product, "buyer": intent.buyer},
        )
        if isinstance(result, str):
            return result
        return purchase_text(result)


def build_telegram_webhook(
    engine: Any,
    *,
    secret: str | None = None,
    sender: Sender | None = None,
) -> TelegramWebhook | None:
    """The webhook, or ``None`` when this deploy has not configured one.

    ``None`` — and therefore no route — whenever any of three things is missing:

    * no secret (explicit or ``TELEGRAM_WEBHOOK_SECRET``): an open webhook would let
      anyone drive the bot, so the absence of a secret closes the door rather than
      widening it;
    * no way to reply (explicit ``sender`` or ``TELEGRAM_BOT_TOKEN``): a route that
      accepts updates and answers nobody looks healthy and is not;
    * no ``engine``: a webhook with nothing behind it can only answer errors, which
      reads to a person as "the bot is broken" rather than "the host is misconfigured".

    Same shape and same reason as ``build_course_surface``: a mount that does not
    appear is a deploy problem somebody notices, and a mount that cannot do its job is
    one nobody does.
    """
    if engine is None:
        return None
    resolved_secret = secret if secret is not None else resolve_webhook_secret()
    if not resolved_secret:
        return None
    resolved_sender = sender
    if resolved_sender is None:
        token = resolve_bot_token()
        if token is None:
            return None
        resolved_sender = sender_from_token(token)
    return TelegramWebhook(
        engine=engine, secret=resolved_secret, sender=resolved_sender
    )


def telegram_routes(
    engine: Any,
    *,
    secret: str | None = None,
    sender: Sender | None = None,
) -> list[Any]:
    """``[Route(WEBHOOK_PATH)]``, or ``[]`` when the webhook is not configured.

    Returned as routes (not mounted here) so the transport stays generic: the HTTP
    layer appends a list of Starlette routes and never imports this surface, the same
    seam ``registry_routes`` uses.
    """
    webhook = build_telegram_webhook(engine, secret=secret, sender=sender)
    if webhook is None:
        logger.info(
            "telegram webhook NOT mounted: set %s and %s to serve it",
            WEBHOOK_SECRET_ENV,
            "TELEGRAM_BOT_TOKEN",
        )
        return []
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def _endpoint(request: Request) -> Any:
        # The secret first, before the body is read: an unauthenticated caller must not
        # be able to make us parse anything, and must learn nothing from the answer.
        if not webhook.verify(request.headers.get(SECRET_HEADER)):
            logger.info("telegram webhook refused: secret mismatch")
            return JSONResponse({"error": "forbidden"}, status_code=403)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_UPDATE_BYTES:
            return JSONResponse({"error": "update too large"}, status_code=413)
        raw = await request.body()
        if len(raw) > MAX_UPDATE_BYTES:
            return JSONResponse({"error": "update too large"}, status_code=413)
        try:
            update = json.loads(raw) if raw else None
        except (ValueError, UnicodeDecodeError):
            update = None
        if not isinstance(update, dict):
            return JSONResponse({"error": "invalid update"}, status_code=400)
        from starlette.concurrency import run_in_threadpool

        # The engine and the Bot API are synchronous stdlib calls; off the event loop so
        # one slow RPC cannot stall every other route on this host.
        sent = await run_in_threadpool(webhook.send, update)
        # `ok` means we processed the update, which is what Telegram reads it as.
        # `replied` is for us, and neither field carries anything from the message.
        return JSONResponse({"ok": True, "replied": sent})

    return [Route(WEBHOOK_PATH, endpoint=_endpoint, methods=["POST"])]

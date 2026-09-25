"""The one outbound call this surface makes: Telegram's ``sendMessage``.

Telegram's Bot API is plain HTTPS with a JSON body, so there is no framework here and
no new dependency — the POST goes through :func:`gecko.rpc.post_json`, the same
stdlib-``urllib`` helper the JSON-RPC transport uses. One helper, one User-Agent, one
place where a timeout is set.

Three things this module is careful about:

* **The host is a constant.** An update names a chat, never a server. The only URL
  ever built is ``https://api.telegram.org/bot<token>/<method>`` with ``method`` from
  a closed set, so no value from an untrusted update can steer a request (no SSRF).
* **The token is a bearer credential and it lives in the URL.** Anyone holding it
  controls the bot and can read every message sent to it. It is read from the
  environment, never logged, and never allowed into an exception message — a failure
  is re-raised as :class:`TelegramApiError` carrying the exception CLASS only.
* **Sending is injectable.** :data:`Sender` is the seam, so the whole webhook path is
  falsifiable offline with a list instead of a network.

CONTROL PLANE (invariant #1): a chat id and a reply pass THROUGH this module as call
arguments. Neither is stored, cached, or logged.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable

from .rpc import post_json

__all__ = [
    "BOT_TOKEN_ENV",
    "SENTINEL",
    "TELEGRAM_API_HOST",
    "Sender",
    "TelegramApiError",
    "resolve_bot_token",
    "send_message",
    "sender_from_token",
]

logger = logging.getLogger("gecko.telegram_api")

#: Telegram's fixed API host. A constant, never a configurable, and never read from an
#: update — that is the whole SSRF posture of this surface.
TELEGRAM_API_HOST = "https://api.telegram.org"

BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"

#: The SSM boot sentinel (infra/push-ssm-params.sh). An ECS ``secrets:`` ValueFrom must
#: resolve or the task dies at boot, so an unfilled param is pushed with this
#: placeholder and the runtime must read it as unset — the convention
#: ``gecko/events.py`` and ``gecko/http_server.py`` already follow.
SENTINEL = "__unset__"

#: The only Bot API methods this surface may call. A closed set, because the method is
#: the one path segment we build and an open one would be a generic proxy onto
#: somebody's bot — including the methods that change its webhook.
_ALLOWED_METHODS: frozenset[str] = frozenset({"sendMessage"})

#: Send a reply: ``(chat_id, text) -> None``. The seam every caller takes, so a test
#: appends to a list and the engine path never touches a socket.
Sender = Callable[[int, str], None]


class TelegramApiError(Exception):
    """A Bot API call failed. Carries the failure CLASS and nothing else: the URL holds
    the bot token, and an upstream error body is untrusted output."""


def resolve_bot_token(env: dict[str, str] | None = None) -> str | None:
    """The bot token, or ``None`` when it is genuinely unset.

    Empty, whitespace and the SSM sentinel all mean unset. ``None`` is what keeps the
    webhook route from mounting at all: a bot that cannot answer must not be reachable,
    because a route that accepts an update and then silently drops it is
    indistinguishable from a working one.
    """
    source = env if env is not None else os.environ
    raw = (source.get(BOT_TOKEN_ENV) or "").strip()
    if not raw or raw == SENTINEL:
        return None
    return raw


def _method_url(token: str, method: str) -> str:
    """The Bot API URL for ``method``. Raises on any method outside the closed set, so
    the only path segment we assemble cannot be widened by a caller."""
    if method not in _ALLOWED_METHODS:
        raise TelegramApiError(f"refusing to call Bot API method {method!r}")
    return f"{TELEGRAM_API_HOST}/bot{token}/{method}"


def send_message(token: str, chat_id: int, text: str) -> None:
    """POST one ``sendMessage``. Raises :class:`TelegramApiError` on any failure.

    No ``parse_mode``: the reply carries store names and addresses read from a chain,
    and asking Telegram to parse that as Markdown would let an on-chain string break
    (or restyle) a message about money. Plain text renders exactly what we wrote.
    """
    body = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode()
    try:
        response = post_json(_method_url(token, "sendMessage"), body)
    except TelegramApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - redact: the URL carries the bot token
        raise TelegramApiError(f"sendMessage failed: {type(exc).__name__}") from None
    if not response.get("ok"):
        # `description` is Telegram's own error text. It is upstream output, so only its
        # presence is reported — never its content, and never the request we sent.
        raise TelegramApiError("sendMessage was refused by the Bot API")


def sender_from_token(token: str) -> Sender:
    """Bind ``token`` into a :data:`Sender`, so nothing downstream holds the credential
    or can choose a different method."""

    def _send(chat_id: int, text: str) -> None:
        send_message(token, chat_id, text)

    return _send

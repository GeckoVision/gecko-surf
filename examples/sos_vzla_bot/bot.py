"""Telegram transport — thin. The comprehension + agent logic lives in
``surfcall_tools`` and ``agent``; this module only bridges Telegram to them.

``handle_message`` and ``RateLimiter`` are pure (a responder + clock are injected)
so they test offline without ``python-telegram-bot`` or ``anthropic``. ``run`` wires
the real long-polling bot and is exercised only by the founder-run live smoke.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .agent import FALLBACK_ES

TELEGRAM_LIMIT = 4096

RATE_LIMIT_ES = (
    "Estás enviando muchas consultas muy rápido. Espera unos segundos y vuelve a "
    "intentar, por favor. 🙏"
)
EMPTY_ES = (
    "¡Hola! Escríbeme tu consulta: puedo buscar personas desaparecidas/encontradas, "
    "ver reportes del mapa, cifras, daños o noticias. Emergencias: 171."
)
WELCOME_ES = (
    "Soy el asistente de SOS Venezuela 2026. Pregúntame en lenguaje normal, por "
    "ejemplo: «¿está reportada María Pérez?», «¿cuántos desaparecidos hay?» o "
    "«últimas noticias». Datos públicos, gratis. Emergencias: 171.\n\n"
    "(You can also write in English.)"
)
HELP_ES = (
    "Puedes escribirme en lenguaje normal, o usar comandos:\n"
    "/buscar <nombre> — buscar una persona desaparecida o encontrada\n"
    "/cifras — desaparecidos y encontrados\n"
    "/reportes — reportes recientes del mapa\n"
    "/noticias — últimas noticias del terremoto\n\n"
    "Datos de «SOS Venezuela 2026». Emergencias en Venezuela: 171."
)
BUSCAR_PROMPT_ES = (
    "Escribe el nombre después del comando, por ejemplo:\n/buscar María Pérez"
)
# Commands that map to a canned query the agent answers with LIVE data.
COMMAND_QUERIES = {
    "cifras": "Dame las cifras agregadas: cuántas personas desaparecidas y cuántas encontradas.",
    "reportes": "Muéstrame los reportes recientes del mapa de peligros y resume los más relevantes.",
    "noticias": "Dame las últimas noticias verificadas del terremoto.",
}


class RateLimiter:
    """Per-user sliding-window limiter — protects the upstream ~90 req/min budget
    and our LLM spend from a single chatty user."""

    def __init__(self, max_per_min: int) -> None:
        self.max = max_per_min
        self._hits: dict[int, list[float]] = {}

    def allow(self, user_id: int, now: float) -> bool:
        window = [t for t in self._hits.get(user_id, []) if now - t < 60.0]
        if len(window) >= self.max:
            self._hits[user_id] = window
            return False
        window.append(now)
        self._hits[user_id] = window
        return True


def handle_message(
    text: str,
    user_id: int,
    *,
    responder: Callable[[str], str],
    limiter: RateLimiter,
    now: float,
) -> str:
    """Pure per-message handler: rate-check → agent → reply. Never raises; a failure
    degrades to a friendly Spanish fallback that never leaks internals."""
    if not text or not text.strip():
        return EMPTY_ES
    if not limiter.allow(user_id, now):
        return RATE_LIMIT_ES
    try:
        return responder(text.strip())
    except Exception:  # noqa: BLE001 - the bot must never crash on one bad turn
        return FALLBACK_ES


def resolve_command(command: str, arg: str) -> tuple[str | None, str | None]:
    """Map a command to (static_reply, agent_query). Exactly one is non-None for a
    known command; (None, None) for an unknown one. Strips a leading slash and any
    @bot mention."""
    cmd = command.lstrip("/").split("@")[0].lower()
    if cmd == "start":
        return WELCOME_ES, None
    if cmd in ("ayuda", "help"):
        return HELP_ES, None
    if cmd == "buscar":
        if arg.strip():
            return None, f"Busca personas reportadas con el nombre: {arg.strip()}"
        return BUSCAR_PROMPT_ES, None
    if cmd in COMMAND_QUERIES:
        return None, COMMAND_QUERIES[cmd]
    return None, None


def handle_command(
    command: str,
    arg: str,
    user_id: int,
    *,
    responder: Callable[[str], str],
    limiter: RateLimiter,
    now: float,
) -> str:
    """Resolve a command to a static reply or route its canned query through the
    same rate-limited, never-raises agent path as free text."""
    static, query = resolve_command(command, arg)
    if static is not None:
        return static
    if query is None:
        return HELP_ES  # unknown command — show help, never silence
    return handle_message(query, user_id, responder=responder, limiter=limiter, now=now)


def chunk_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split a reply into Telegram-sized chunks (<= limit), preferring paragraph,
    then line, then word boundaries; hard-splits only as a last resort."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return [c for c in chunks if c]


def run(config=None) -> None:  # pragma: no cover - founder-run live smoke
    """Start the long-polling Telegram bot. Requires the `sosbot` extra
    (`uv sync --extra sosbot`) and TELEGRAM_BOT_TOKEN + ANTHROPIC_API_KEY in env."""
    import asyncio
    import time

    try:
        from telegram import Update
        from telegram.constants import ChatAction
        from telegram.ext import (
            Application,
            CommandHandler,
            ContextTypes,
            MessageHandler,
            filters,
        )
    except ImportError as exc:
        raise SystemExit("Instala el extra del bot: uv sync --extra sosbot") from exc

    from . import agent
    from .config import SPEC_PATH, SYSTEM_ES, BotConfig
    from .providers import make_llm
    from .surfcall_tools import SurfcallTools

    cfg = config or BotConfig.from_env()
    llm = make_llm(cfg.provider, cfg.llm_api_key)
    tools = SurfcallTools(SPEC_PATH, mode=cfg.mode)
    limiter = RateLimiter(cfg.rate_limit_per_min)

    def responder(text: str) -> str:
        return agent.respond(
            text,
            llm=llm,
            tools=tools,
            model=cfg.model,
            system=SYSTEM_ES,
            max_tokens=cfg.max_tokens,
            max_iters=cfg.max_iters,
        )

    async def _respond(msg: Any, thunk: Callable[[], str]) -> None:
        # Show "typing…", run the blocking agent off the event loop (so one slow
        # request doesn't freeze the bot for everyone), then send chunked.
        await msg.chat.send_action(ChatAction.TYPING)
        reply = await asyncio.get_running_loop().run_in_executor(None, thunk)
        for chunk in chunk_message(reply):
            await msg.reply_text(chunk)

    async def on_command(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        msg, user = update.message, update.effective_user
        if msg is None or user is None or not msg.text:
            return
        uid = user.id
        parts = (msg.text or "")[1:].split(maxsplit=1)
        command = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        await _respond(
            msg,
            lambda: handle_command(
                command,
                arg,
                uid,
                responder=responder,
                limiter=limiter,
                now=time.monotonic(),
            ),
        )

    async def on_message(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        uid, text = user.id, (msg.text or "")
        await _respond(
            msg,
            lambda: handle_message(
                text, uid, responder=responder, limiter=limiter, now=time.monotonic()
            ),
        )

    app = Application.builder().token(cfg.telegram_token).build()
    app.add_handler(
        CommandHandler(
            ["start", "ayuda", "help", "buscar", "cifras", "reportes", "noticias"],
            on_command,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.run_polling()

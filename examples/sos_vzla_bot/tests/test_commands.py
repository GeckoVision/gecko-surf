"""Command-menu routing — pure, offline.

Commands map to either a static reply (/start, /ayuda, /buscar with no name) or a
canned query routed through the agent (/cifras, /reportes, /noticias, /buscar
<name>). Tested without Telegram or any LLM.
"""

from __future__ import annotations

from examples.sos_vzla_bot.bot import (
    BUSCAR_PROMPT_ES,
    HELP_ES,
    WELCOME_ES,
    RateLimiter,
    handle_command,
    resolve_command,
)


def test_resolve_static_and_query_commands():
    assert resolve_command("start", "") == (WELCOME_ES, None)
    assert resolve_command("/ayuda", "")[0] == HELP_ES
    assert resolve_command("help", "")[0] == HELP_ES  # English alias
    static, query = resolve_command("cifras", "")
    assert static is None and "desaparecid" in (query or "").lower()
    # bot @-mention is stripped
    assert resolve_command("cifras@DEV_VEZbot", "")[1] is not None


def test_buscar_needs_a_name():
    assert resolve_command("buscar", "")[0] == BUSCAR_PROMPT_ES
    static, query = resolve_command("buscar", "María Pérez")
    assert static is None and "María Pérez" in (query or "")


def test_handle_command_static_skips_agent():
    rl = RateLimiter(10)
    calls: list[str] = []
    out = handle_command(
        "ayuda",
        "",
        1,
        responder=lambda t: (calls.append(t), "x")[1],
        limiter=rl,
        now=0.0,
    )
    assert out == HELP_ES and calls == []  # no LLM call for a static command


def test_handle_command_query_calls_agent():
    rl = RateLimiter(10)
    seen: list[str] = []
    out = handle_command(
        "cifras",
        "",
        1,
        responder=lambda t: (seen.append(t), "RESP")[1],
        limiter=rl,
        now=0.0,
    )
    assert out == "RESP" and seen and "desaparecid" in seen[0].lower()


def test_handle_command_buscar_with_name_calls_agent():
    rl = RateLimiter(10)
    seen: list[str] = []
    out = handle_command(
        "buscar",
        "José",
        1,
        responder=lambda t: (seen.append(t), "OK")[1],
        limiter=rl,
        now=0.0,
    )
    assert out == "OK" and "José" in seen[0]


def test_unknown_command_does_not_crash():
    rl = RateLimiter(10)
    out = handle_command("xyz", "", 1, responder=lambda t: "NO", limiter=rl, now=0.0)
    assert isinstance(out, str) and out

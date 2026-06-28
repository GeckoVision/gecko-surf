"""Bot transport logic — pure, no Telegram, no Anthropic.

`handle_message` is the seam between the Telegram wire and the agent; keeping it a
pure function (inject the responder + a clock) lets us test rate-limiting, empty
input, and error degradation offline.
"""

from __future__ import annotations

from examples.sos_vzla_bot.bot import RATE_LIMIT_ES, RateLimiter, handle_message


def test_rate_limiter_blocks_after_quota():
    rl = RateLimiter(max_per_min=2)
    assert rl.allow(1, now=1000.0) is True
    assert rl.allow(1, now=1000.5) is True
    assert rl.allow(1, now=1001.0) is False  # third within the minute
    # a different user is unaffected
    assert rl.allow(2, now=1001.0) is True
    # window slides: after 60s the first user is allowed again
    assert rl.allow(1, now=1062.0) is True


def test_handle_message_rate_limited_returns_spanish_notice():
    rl = RateLimiter(max_per_min=1)
    calls = []

    def responder(text):
        calls.append(text)
        return "ok"

    assert handle_message("hola", 7, responder=responder, limiter=rl, now=0.0) == "ok"
    out = handle_message("otra", 7, responder=responder, limiter=rl, now=0.1)
    assert out == RATE_LIMIT_ES
    assert calls == ["hola"]  # the blocked message never reached the agent


def test_handle_message_empty_input_prompts():
    rl = RateLimiter(max_per_min=10)
    out = handle_message("   ", 7, responder=lambda t: "nope", limiter=rl, now=0.0)
    assert "escríbeme" in out.lower() or "consulta" in out.lower()


def test_handle_message_swallows_responder_errors():
    rl = RateLimiter(max_per_min=10)

    def boom(text):
        raise RuntimeError("upstream blew up")

    out = handle_message("hola", 7, responder=boom, limiter=rl, now=0.0)
    assert isinstance(out, str) and out  # a friendly Spanish fallback, not a crash
    assert "upstream blew up" not in out  # never leak internals

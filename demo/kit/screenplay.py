"""Screenplay helpers — the house voice for Gecko demo casts.

A demo is a SCRIPTED, REAL run: every command executes for real, every status
code on screen is the one the wire returned. The screenplay only controls
pacing and typography. See README.md for the style contract.

    from screenplay import out, put, clear, BOLD, CYAN, GREEN, RED, YELLOW, RESET

    clear()                       # also the scene separator the renderer keys on
    out(f"{BOLD}HEADLINE{RESET}") # typed, typewriter pacing
    out(f"{CYAN}$ the command{RESET}", 0.02)
    put(f"{GREEN}✓ HTTP 200{RESET} — the real result", pause=1.5)  # instant
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import time

BOLD = "\033[1m"
CYAN = "\033[38;5;45m"
GREEN = "\033[38;5;42m"
RED = "\033[38;5;203m"
YELLOW = "\033[38;5;220m"
RESET = "\033[0m"


def out(
    text: str = "", delay: float = 0.045, end: str = "\n", pause: float = 0.0
) -> None:
    """Typewriter line — for headlines, narration, and ``$`` command lines."""
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay if ch != "\033" else 0)
    sys.stdout.write(end)
    sys.stdout.flush()
    if pause:
        time.sleep(pause)


def put(text: str = "", pause: float = 0.9) -> None:
    """Instant line — for results and outputs (a machine answered, not a typist)."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    time.sleep(pause)


def clear(settle: float = 0.4) -> None:
    """New scene. The renderer advances the title/tagline on this escape."""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()
    time.sleep(settle)


#: Shapes that must never appear in a command line we display. The rule is "no secrets,
#: ever" and a screenplay is written under time pressure the night before a recording —
#: better to refuse than to render a cast that has to be thrown away.
_SECRET_SHAPES = (
    re.compile(r"(?i)(api[-_]?key|secret|token|password|bearer)\s*[=:]\s*\S+"),
    re.compile(r"://[^/\s:]+:[^/\s@]+@"),  # credentials in a URL
    re.compile(r"\b[A-Za-z0-9_-]{32,}\.[A-Za-z0-9_-]{16,}\."),  # jwt-ish
)


class LeakGuard(Exception):
    """A command was about to put something secret-shaped on camera."""


def run(
    command: str,
    *,
    delay: float = 0.03,
    pause: float = 1.2,
    timeout: int = 300,
    prompt: str = "$ ",
) -> subprocess.CompletedProcess[str]:
    """Type a command, then ACTUALLY RUN IT, streaming its real output to the cast.

    This is the difference between a demo and a mockup. Everything above only *prints*;
    this executes, and what lands on screen is what the process wrote — including when
    that is a failure. The module contract has always said "every command executes for
    real"; until this existed, every screenplay printed a transcript instead, and the
    viewer had no way to tell the difference. That is exactly the gap a demo must not have.

    A non-zero exit is shown, never swallowed. If a command fails during a take, the take
    is wrong or the product is — both are worth knowing before the video ships, and a
    screenplay that hides it produces a video we cannot defend.

    Returns the CompletedProcess so a screenplay can branch on the real result rather
    than assume one.
    """
    for pattern in _SECRET_SHAPES:
        if pattern.search(command):
            raise LeakGuard(
                "command looks like it carries a secret; read it from the environment "
                "inside the script and show only the $VAR name"
            )

    out(f"{CYAN}{prompt}{command}{RESET}", delay)
    try:
        completed = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        put(f"{RED}✗ timed out after {timeout}s{RESET}", pause)
        return subprocess.CompletedProcess(command, 124, "", "timeout")
    except FileNotFoundError:
        put(f"{RED}✗ command not found{RESET}", pause)
        return subprocess.CompletedProcess(command, 127, "", "not found")

    body = completed.stdout.rstrip() or completed.stderr.rstrip()
    if body:
        sys.stdout.write(body + "\n")
        sys.stdout.flush()
    if completed.returncode != 0:
        put(f"{YELLOW}(exit {completed.returncode}){RESET}", 0.2)
    time.sleep(pause)
    return completed

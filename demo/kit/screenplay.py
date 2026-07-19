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

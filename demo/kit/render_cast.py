#!/usr/bin/env python3
"""Render an asciinema v2 cast into the branded Gecko demo MP4 (the house style).

The style contract (see README.md): 1200x676 @30fps, dark terminal in a macOS-style
window on a light frame, footer brand line, per-scene title + tagline. Scenes are
detected from the cast itself: every clear-screen (``ESC[2J``) after t=1s advances
to the next ``--scene``.

    uv run --with pyte --with pillow python demo/kit/render_cast.py demo.cast out.mp4 \
        --scene "Gecko — the painful API, raw|Two tokens, no spec, no mercy" \
        --scene "Gecko — the a-ha: intent → chain|One sentence → the right chain"

Requires DejaVu fonts (stock on Debian/Ubuntu) and ffmpeg on PATH.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1200, 676
FPS = 30
COLS, ROWS = 80, 20  # the style contract: record casts at exactly 80x20
TERM_X, TERM_Y, TERM_W, TERM_H = 42, 38, 1116, 599
CONTENT_X, CONTENT_Y = 72, 104
CELL_W, CELL_H = 13, 26

BG = "#0A0F1A"  # Gecko blue — the house frame
TERMINAL = "#0D1117"
TERMINAL_BAR = "#161B22"
TEXT = "#E6EDF3"
DIM_TEXT = "#8B949E"
BORDER = "#30363D"
GECKO_CYAN = "#35C2D4"

COLORS = {
    "default": TEXT,
    "black": "#484F58",
    "red": "#FF7B72",
    "green": "#3FB950",
    "yellow": "#D29922",
    "blue": "#58A6FF",
    "magenta": "#BC8CFF",
    "cyan": GECKO_CYAN,
    "white": TEXT,
    "42": "#00D75F",
    "45": "#00D7FF",
    "203": "#FF5F5F",
    "220": "#FFD700",
}

_FONTS = "/usr/share/fonts/truetype/dejavu"
MONO = ImageFont.truetype(f"{_FONTS}/DejaVuSansMono.ttf", 21)
MONO_BOLD = ImageFont.truetype(f"{_FONTS}/DejaVuSansMono-Bold.ttf", 21)
UI = ImageFont.truetype(f"{_FONTS}/DejaVuSans.ttf", 16)
UI_BOLD = ImageFont.truetype(f"{_FONTS}/DejaVuSans-Bold.ttf", 16)


def load_cast(path: Path) -> list[tuple[float, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    events: list[tuple[float, str]] = []
    for line in lines[1:]:
        stamp, kind, payload = json.loads(line)
        if kind == "o":
            events.append((float(stamp), payload))
    return events


def char_color(char: object) -> str:
    fg = str(getattr(char, "fg", "default"))
    color = COLORS.get(fg, TEXT)
    if getattr(char, "reverse", False):
        return TERMINAL
    if getattr(char, "bold", False) and fg == "default":
        return "#FFFFFF"
    return color


def render_frame(
    screen: pyte.Screen, title: str, tagline: str, brand: str
) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle(
        (TERM_X, TERM_Y, TERM_X + TERM_W, TERM_Y + TERM_H),
        radius=20,
        fill=TERMINAL,
        outline=BORDER,
        width=2,
    )
    draw.rounded_rectangle(
        (TERM_X + 1, TERM_Y + 1, TERM_X + TERM_W - 1, TERM_Y + 58),
        radius=19,
        fill=TERMINAL_BAR,
    )
    draw.rectangle(
        (TERM_X + 1, TERM_Y + 37, TERM_X + TERM_W - 1, TERM_Y + 59),
        fill=TERMINAL_BAR,
    )
    for index, color in enumerate(("#FF5F56", "#FFBD2E", "#27C93F")):
        cx = TERM_X + 26 + index * 24
        draw.ellipse((cx, TERM_Y + 22, cx + 12, TERM_Y + 34), fill=color)

    draw.text((TERM_X + 112, TERM_Y + 20), title, font=UI_BOLD, fill="#C9D1D9")
    label = "RECORDED WITH ASCIINEMA"
    lw = draw.textbbox((0, 0), label, font=UI)[2]
    draw.text((TERM_X + TERM_W - lw - 24, TERM_Y + 20), label, font=UI, fill="#6E7681")

    for row in range(ROWS):
        for col in range(COLS):
            char = screen.buffer[row][col]
            data = char.data
            if not data or data == " ":
                continue
            font = MONO_BOLD if getattr(char, "bold", False) else MONO
            if getattr(char, "reverse", False):
                draw.rectangle(
                    (
                        CONTENT_X + col * CELL_W,
                        CONTENT_Y + row * CELL_H,
                        CONTENT_X + (col + 1) * CELL_W,
                        CONTENT_Y + (row + 1) * CELL_H,
                    ),
                    fill=TEXT,
                )
            draw.text(
                (CONTENT_X + col * CELL_W, CONTENT_Y + row * CELL_H),
                data,
                font=font,
                fill=char_color(char),
            )

    draw.text((52, 650), brand, font=UI_BOLD, fill="#35C2D4")
    fw = draw.textbbox((0, 0), tagline, font=UI)[2]
    draw.text((WIDTH - fw - 52, 650), tagline, font=UI, fill="#8B949E")
    return image


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cast", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument(
        "--scene",
        action="append",
        default=[],
        metavar="TITLE|TAGLINE",
        help="Window title + footer tagline; repeat per scene. Advances on every "
        "clear-screen in the cast after t=1s.",
    )
    ap.add_argument(
        "--brand",
        default="GECKO  •  THE API LANGUAGE LAYER FOR AGENTS",
        help="Footer brand line (left side).",
    )
    ap.add_argument(
        "--thumb-at",
        type=float,
        default=0.97,
        help="Save a thumbnail PNG at this fraction of the duration (0 disables).",
    )
    args = ap.parse_args()

    scenes = [tuple((s.split("|", 1) + [""])[:2]) for s in args.scene] or [
        ("Gecko — live demo", "")
    ]
    events = load_cast(args.cast)
    duration = events[-1][0]
    total_frames = int(duration * FPS) + 1
    thumb_frame = int(total_frames * args.thumb_at) if args.thumb_at else -1

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    scene_idx, event_index = 0, 0

    proc = subprocess.Popen(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{WIDTH}x{HEIGHT}",
            "-r",
            str(FPS),
            "-i",
            "-",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(args.output),
        ],
        stdin=subprocess.PIPE,
    )
    assert proc.stdin is not None

    for frame_no in range(total_frames):
        t = frame_no / FPS
        while event_index < len(events) and events[event_index][0] <= t:
            payload = events[event_index][1]
            # a clear-screen after the opening one advances the scene
            if "\x1b[2J" in payload and events[event_index][0] > 1.0:
                scene_idx = min(scene_idx + 1, len(scenes) - 1)
            stream.feed(payload)
            event_index += 1
        title, tagline = scenes[scene_idx]
        frame = render_frame(screen, title, tagline, args.brand)
        proc.stdin.write(frame.tobytes())
        if frame_no == thumb_frame:
            frame.save(args.output.with_suffix(".thumb.png"))

    proc.stdin.close()
    proc.wait()
    print(f"wrote {args.output}  ({duration:.0f}s, {total_frames} frames)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

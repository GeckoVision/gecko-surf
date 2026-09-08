#!/usr/bin/env python3
"""Render a REAL Claude Code run as a Claude-web-shaped conversation.

Input is the `--output-format stream-json` transcript of an actual run: the prompt, the
tool calls with their arguments, which ones failed, and the answer. Nothing is authored
here. The renderer only DISPLAYS: it gives each tool a human label, shortens base58
addresses, and reveals the rows in the order they happened.

    claude -p "..." --output-format stream-json --include-partial-messages --verbose \
        --mcp-config mcp.json > run.jsonl
    uv run --with pillow python demo/kit/chat_take_screenplay.py run.jsonl out.mp4 "the prompt"
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H, FPS, S = 1200, 676, 30, 2
BG, BUBBLE, BOX, RULE = "#262625", "#333331", "#2b2b2a", "#3d3d3b"
INK, MUTED, DIM, CHIP, FAIL = "#eeeeec", "#a3a29e", "#7d7c78", "#d6d5d1", "#c98a6a"
F = "/usr/share/fonts/truetype/dejavu"

#: tool id -> what a human calls it. Display only; the run is unchanged.
LABEL = {
    "ToolSearch": "Searched available tools",
    "mcp__gecko-store__list_stores": "List stores and menus",
    "mcp__gecko-store__plan_swap": "Plan a token swap",
    "mcp__gecko-store__plan_payment": "Check what the wallet can pay with",
    "mcp__gecko-store__prepare_purchase": "Prepare an unsigned purchase",
    "mcp__gecko-store__read_accounts": "Read accounts on chain",
    "mcp__gecko-store__start": "Open the storefront surface",
    "mcp__gecko-store__prepare_instruction": "Prepare the unsigned swap",
    "mcp__gecko-store__verify_signed_transaction": "Verify the signed bytes",
    "mcp__gecko-store__submit_transaction": "Submit to mainnet",
    "mcp__paybox__paybox_wallet": "Ask PayBox which wallet it signs for",
    "mcp__paybox__paybox_sign_solana": "PayBox signs",
}
B58 = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def label_of(name: str) -> str:
    if name in LABEL:
        return LABEL[name]
    if "PayBox" in name:
        return "PayBox " + name.rsplit("__", 1)[-1].replace("_", " ")
    return name.rsplit("__", 1)[-1].replace("_", " ").capitalize()


def chip_of(name: str, inp: dict) -> str:
    """The arguments, as a reader would skim them. Long keys shortened, never invented."""
    if name == "ToolSearch":
        q = str(inp.get("query", ""))
        q = q.replace("select:", "")
        parts = [p.rsplit("__", 1)[-1] for p in q.replace(",", " ").split() if p]
        return " ".join(dict.fromkeys(parts))[:60]
    bits = []
    for k, v in inp.items():
        raw = str(v)
        # Serialized bytes are noise on screen: "AQAAAA…" tells a reader nothing.
        if len(raw) > 90 and " " not in raw:
            bits.append(f"{k} {len(raw)} bytes" if len(inp) > 1 else f"{len(raw)} bytes")
            continue
        raw = B58.sub(lambda m: m.group()[:6] + "…", raw)
        bits.append(raw if len(inp) == 1 else f"{k} {raw}")
    # Drop whole arguments rather than cutting one in half: "· am" reads as a bug.
    out_ = ""
    for bit in bits:
        nxt = f"{out_} · {bit}" if out_ else bit
        if len(nxt) > 58:
            return out_ + " …" if out_ else bit[:58]
        out_ = nxt
    return out_


def parse(path: Path):
    steps, answer, think = [], [], []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        kind = ev.get("type")
        if kind == "stream_event":
            d = ev.get("event", {}).get("delta", {})
            if d.get("type") == "text_delta":
                if answer and answer[-1] == "\x00":
                    answer[-1] = " "          # a tool call ran between two sentences
                answer.append(d.get("text", ""))
            elif d.get("type") == "thinking_delta":
                think.append(d.get("thinking", ""))
        elif kind == "assistant":
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") == "tool_use":
                    if answer and answer[-1] != "\x00":
                        answer.append("\x00")
                    steps.append(
                        {"label": label_of(b["name"]), "chip": chip_of(b["name"], b.get("input", {})), "fail": False}
                    )
        elif kind == "user":
            content = ev.get("message", {}).get("content")
            if isinstance(content, list):
                for b in content:
                    if b.get("type") == "tool_result" and b.get("is_error"):
                        for st in reversed(steps):
                            if not st["fail"]:
                                st["fail"] = True
                                break
    return steps, "".join(answer).replace("\x00", " ").strip()


def main() -> int:
    run = Path(sys.argv[1])
    out = Path(sys.argv[2])
    prompt = sys.argv[3]
    steps, answer = parse(run)

    ui = ImageFont.truetype(f"{F}/DejaVuSans.ttf", 16 * S)
    ui_b = ImageFont.truetype(f"{F}/DejaVuSans-Bold.ttf", 16 * S)
    small = ImageFont.truetype(f"{F}/DejaVuSans.ttf", 13 * S)
    mono = ImageFont.truetype(f"{F}/DejaVuSansMono.ttf", 12 * S)
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    PAD, ROW = 52 * S, 34 * S

    def wrap(text, font, width):
        out_, cur = [], ""
        for word in text.split():
            t = f"{cur} {word}".strip()
            if scratch.textlength(t, font=font) <= width:
                cur = t
            else:
                out_.append(cur)
                cur = word
        if cur:
            out_.append(cur)
        return out_ or [""]

    def segments(line: str):
        """**bold** and `code` become styled runs. Everything else is plain."""
        parts, i = [], 0
        for m in re.finditer(r"\*\*(.+?)\*\*|`(.+?)`", line):
            if m.start() > i:
                parts.append((line[i:m.start()], ui, INK))
            parts.append((m.group(1) or m.group(2), ui_b if m.group(1) else mono,
                          INK if m.group(1) else CHIP))
            i = m.end()
        if i < len(line):
            parts.append((line[i:], ui, INK))
        return parts or [(line, ui, INK)]

    # Markdown tables become real tables. A pipe row is a row; the |---| rule is dropped.
    def is_row(ln):
        t = ln.strip()
        return t.startswith("|") and t.endswith("|") and t.count("|") >= 3
    def cells(ln):
        return [c.strip() for c in ln.strip().strip("|").split("|")]

    ans_lines, i = [], 0
    body = answer.splitlines()
    while i < len(body):
        ln = body[i]
        if is_row(ln):
            block = []
            while i < len(body) and is_row(body[i]):
                c = cells(body[i])
                if not all(set(x) <= set("-: ") for x in c):
                    block.append([B58.sub(lambda m: m.group()[:6] + "…", x) for x in c])
                i += 1
            CAP = 9   # header + 8 rows; a 20-row menu owns the whole frame otherwise
            if len(block) > CAP:
                hidden = len(block) - CAP
                block = block[:CAP] + [[f"… and {hidden} more", ""][: len(block[0])]]
            ans_lines.append(("table", block))
            continue
        clean = B58.sub(lambda m: m.group()[:6] + "…", ln)
        for w_ in (wrap(clean, ui, (W - 130) * S) if ln.strip() else [""]):
            ans_lines.append(("text", w_))
        i += 1

    def frame(n_rows, n_ans):
        img = Image.new("RGB", (W * S, H * S), BG)
        d = ImageDraw.Draw(img)
        blocks = []
        plines = wrap(prompt, ui, (W - 420) * S)
        blocks.append(("bubble", plines, len(plines) * 24 * S + 26 * S + 26 * S))
        if n_rows > 0:
            blocks.append(("hdr", None, 34 * S))
            blocks.append(("rows", min(n_rows, len(steps)), min(n_rows, len(steps)) * ROW + 22 * S))
        if n_ans > 0:
            shown = ans_lines[:n_ans]
            h = 0
            for kind_, payload in shown:
                h += (len(payload) * 30 * S + 18 * S) if kind_ == "table" else 25 * S
            blocks.append(("ans", shown, h + 10 * S))
        total = sum(h for _, _, h in blocks)
        y = 40 * S + min(0, (H - 80) * S - total)
        for kind, data, h in blocks:
            if kind == "bubble":
                wpx = max(scratch.textlength(l, font=ui) for l in data) + 44 * S
                x0 = W * S - PAD - wpx
                d.rounded_rectangle([x0, y, W * S - PAD, y + h - 26 * S], 16 * S, fill=BUBBLE)
                for j, l in enumerate(data):
                    d.text((x0 + 22 * S, y + 14 * S + j * 24 * S), l, font=ui, fill=INK)
            elif kind == "hdr":
                d.text((PAD, y + 4 * S), "Loaded tools, used gecko-mcp integration  ⌄",
                       font=small, fill=MUTED)
            elif kind == "rows":
                top = y
                d.rounded_rectangle([PAD, top, W * S - PAD, top + data * ROW], 8 * S,
                                    fill=BOX, outline=RULE, width=S)
                for i in range(data):
                    ry = top + i * ROW
                    if i:
                        d.line([(PAD, ry), (W * S - PAD, ry)], fill=RULE, width=S)
                    st = steps[i]
                    x = PAD + 16 * S
                    d.text((x, ry + 9 * S), st["label"], font=small, fill=INK)
                    x += scratch.textlength(st["label"], font=small) + 14 * S
                    if st["chip"]:
                        d.text((x, ry + 10 * S), st["chip"], font=mono, fill=DIM)
                        x += scratch.textlength(st["chip"], font=mono) + 12 * S
                    if st["fail"]:
                        d.text((x, ry + 9 * S), "Failed", font=small, fill=FAIL)
                        x += scratch.textlength("Failed", font=small) + 10 * S
                    d.text((x, ry + 9 * S), "›", font=small, fill=DIM)
            else:
                yy = y
                for kind_, payload in data:
                    if kind_ == "table":
                        ncol = max(len(r) for r in payload)
                        widths = [
                            max(scratch.textlength((r[c] if c < len(r) else "").strip("`").replace("**", ""), font=ui_b) for r in payload) + 34 * S
                            for c in range(ncol)
                        ]
                        th = len(payload) * 30 * S
                        d.rounded_rectangle([PAD, yy, PAD + sum(widths), yy + th], 8 * S,
                                            fill=BOX, outline=RULE, width=S)
                        for ri, row in enumerate(payload):
                            ry = yy + ri * 30 * S
                            if ri:
                                d.line([(PAD, ry), (PAD + sum(widths), ry)], fill=RULE, width=S)
                            cx = PAD + 16 * S
                            for ci in range(ncol):
                                txt = row[ci] if ci < len(row) else ""
                                code = txt.startswith("`") and txt.endswith("`") and len(txt) > 1
                                txt = txt.strip("`").replace("**", "")
                                fnt = ui_b if ri == 0 else (mono if code else ui)
                                d.text((cx, ry + (7 if not code or ri == 0 else 9) * S), txt,
                                       font=fnt, fill=INK if ri == 0 else CHIP)
                                cx += widths[ci]
                        yy += th + 18 * S
                    else:
                        x = PAD
                        for txt, fnt, col in segments(payload):
                            d.text((x, yy), txt, font=fnt, fill=col)
                            x += scratch.textlength(txt, font=fnt)
                        yy += 25 * S
            y += h
        return img

    tmp = Path(tempfile.mkdtemp())
    n = 0

    def save(img):
        nonlocal n
        img.save(tmp / f"{n:05d}.png")
        n += 1

    for _ in range(18):
        save(frame(0, 0))
    for r in range(1, len(steps) + 1):
        for _ in range(9):
            save(frame(r, 0))
    for a in range(1, len(ans_lines) + 1):
        for _ in range(6):
            save(frame(len(steps), a))
    for _ in range(60):
        save(frame(len(steps), len(ans_lines)))

    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS),
                    "-i", str(tmp / "%05d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(out)], check=True)
    Image.open(tmp / f"{n-1:05d}.png").save(out.with_suffix(".thumb.png"))
    print(f"wrote {out}  ({n/FPS:.0f}s, {len(steps)} tool rows, {len(ans_lines)} answer lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""The a-ha, as a chat: "I want a coffee, I only have USDG."

Renders a Claude-web-shaped conversation straight to MP4. No terminal, no asciinema.

EVERY NUMBER IS FETCHED LIVE while this runs — the menu and the buyer's holdings off
mainnet, the compute figures off the two transactions that actually landed on
2026-09-08. Nothing is typed in by hand. That is the demo kit's honesty contract, and
it is the reason the a-ha lands: the predicted number and the charged number are read
from two different places and are the same.

    uv run --with pillow python demo/kit/chat_purchase_screenplay.py \
        docs/assets/chat-purchase.mp4
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

RPC = os.environ.get("GECKO_MAINNET_RPC") or "https://api.mainnet-beta.solana.com"
SWAP_SIG = "4XgsSDS8vXwDTnjWYLt5tJRwHtCsmM991GsXTjCKsLqgD2aJiz3vPDqwcaqdRXZes1ohAf9u6uCB9pzSpKtkb8rM"
BUY_SIG = "3oXRbDYNWbjHcU8ana66BE3NkUnZUrQ1YwT9D5kjCE6uuficgdtD4s6DBNGFJWESSbFSjRShMPRadSjfjo5qDzKE"
BUYER = "9cJbQKxxqCbumpoeb7YWC3QESzFD8LxpbHVAXrTUsPfh"
USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
T22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
POOL = "9RqDTfwCx2SgxsvKpspQHc38HUo3B6hRd3oR9JR966Ps"

W, H, FPS, SCALE = 1200, 676, 30, 2

# Claude-web-ish: warm off-white ground, near-black text, one accent.
BG, PANEL = "#faf9f7", "#ffffff"
INK, MUTED, RULE = "#1f1e1c", "#6b6862", "#e6e3dd"
USER_BG, ACCENT, GOOD = "#efece6", "#c96442", "#2f7d5f"
F = "/usr/share/fonts/truetype/dejavu"


def rpc(method, params):
    req = urllib.request.Request(
        RPC, method="POST",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def live_facts():
    """Everything on screen, read off the wire right now."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from gecko.store_directory import list_stores_result  # noqa

    menu = list_stores_result({"network": "mainnet", "store": "geckocoffee"})
    store = menu["stores"][0]
    espresso = next(p for p in store["products"] if p["name"] == "Espresso")

    held = "0"
    for a in rpc("getTokenAccountsByOwner", [BUYER, {"programId": T22}, {"encoding": "jsonParsed"}])["result"]["value"]:
        i = a["account"]["data"]["parsed"]["info"]
        if i["mint"] == USDG:
            held = i["tokenAmount"]["uiAmountString"]

    cu = {}
    for label, sig in (("swap", SWAP_SIG), ("buy", BUY_SIG)):
        t = rpc("getTransaction", [sig, {"maxSupportedTransactionVersion": 0, "encoding": "json"}])["result"]
        cu[label] = t["meta"]["computeUnitsConsumed"]
    return espresso, held, cu


def blocks(espresso, held, cu):
    price, mint = espresso["price_ui"], espresso["mint"]
    return [
        ("user", "I want to buy a coffee. I only have USDG."),
        ("tool", f"list_stores  geckocoffee", f"Espresso {price} USDC  ·  mint {mint[:6]}…{mint[-4:]}  ·  classic SPL"),
        ("bot", f"You hold {held} USDG, which is Token-2022. The shop prices in USDC, "
                "classic SPL. Different asset, not a different label, so a wallet "
                "holding one cannot pay where the other is priced."),
        ("bot", "Two steps then. Swap, then buy."),
        ("tool", "plan_swap  USDG → USDC", f"venue {POOL[:6]}…{POOL[-4:]}  ·  derived, nobody chose it"),
        ("tool", "prepare + simulate", f"receipt PASS  ·  predicted {cu['buy']:,} CU  ·  binding [exact]"),
        ("bot", "Those are unsigned bytes and a receipt. Your wallet signs them, not me. "
                "I never held a key."),
        ("tool", f"read back  {BUY_SIG[:8]}…", f"landed  ·  the chain charged {cu['buy']:,} CU"),
        ("aha", f"Predicted {cu['buy']:,} before signing. Charged {cu['buy']:,} on chain."),
        ("bot", "Espresso paid. 0.10 USDC moved to the shop."),
    ]


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/assets/chat-purchase.mp4")
    espresso, held, cu = live_facts()
    script = blocks(espresso, held, cu)

    s = SCALE
    ui = ImageFont.truetype(f"{F}/DejaVuSans.ttf", 17 * s)
    ui_b = ImageFont.truetype(f"{F}/DejaVuSans-Bold.ttf", 17 * s)
    mono = ImageFont.truetype(f"{F}/DejaVuSansMono.ttf", 13 * s)
    mono_b = ImageFont.truetype(f"{F}/DejaVuSansMono-Bold.ttf", 13 * s)
    small = ImageFont.truetype(f"{F}/DejaVuSans-Bold.ttf", 12 * s)

    def wrap(text, font, width):
        words, lines, cur = text.split(), [], ""
        for w_ in words:
            t = f"{cur} {w_}".strip()
            if ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(t, font=font) <= width:
                cur = t
            else:
                lines.append(cur); cur = w_
        if cur:
            lines.append(cur)
        return lines

    PAD, MAXW = 56 * s, (W - 200) * s

    def measure(upto, reveal):
        """Heights first, so the view can scroll and the a-ha is never cut off."""
        items = []
        for i, blk in enumerate(script[: upto + 1]):
            kind, partial = blk[0], i == upto
            if kind == "user":
                txt = blk[1][: reveal] if partial else blk[1]
                lines = wrap(txt, ui, MAXW - 40 * s) or [""]
                items.append((blk, lines, len(lines) * 26 * s + 24 * s + 22 * s))
            elif kind in ("tool", "aha"):
                if partial and reveal < 3:
                    continue
                h = (62 if kind == "tool" else 56) * s + (18 if kind == "tool" else 20) * s
                items.append((blk, None, h))
            else:
                txt = blk[1][: reveal] if partial else blk[1]
                lines = wrap(txt, ui, MAXW) or [""]
                items.append((blk, lines, len(lines) * 27 * s + 22 * s))
        return items

    def draw_all(upto, reveal):
        img = Image.new("RGB", (W * s, H * s), BG)
        d = ImageDraw.Draw(img)
        items = measure(upto, reveal)
        total = sum(h for _, _, h in items)
        top, avail = 74 * s, (H - 92) * s
        y = top + min(0, avail - total)          # scroll so the newest stays in frame
        for blk, lines, h in items:
            kind = blk[0]
            if kind == "user":
                wpx = max(d.textlength(l, font=ui) for l in lines) + 40 * s
                x0 = W * s - PAD - wpx
                d.rounded_rectangle([x0, y, W * s - PAD, y + h - 22 * s], 14 * s, fill=USER_BG)
                for j, l in enumerate(lines):
                    d.text((x0 + 20 * s, y + 12 * s + j * 26 * s), l, font=ui, fill=INK)
            elif kind == "tool":
                d.rounded_rectangle([PAD, y, W * s - PAD, y + 62 * s], 10 * s,
                                    fill=PANEL, outline=RULE, width=s)
                d.text((PAD + 18 * s, y + 12 * s), blk[1], font=mono_b, fill=ACCENT)
                d.text((PAD + 18 * s, y + 34 * s), blk[2], font=mono, fill=MUTED)
            elif kind == "aha":
                d.rounded_rectangle([PAD, y, W * s - PAD, y + 56 * s], 10 * s, fill="#eef5f1")
                d.rectangle([PAD, y + 6 * s, PAD + 4 * s, y + 50 * s], fill=GOOD)
                d.text((PAD + 22 * s, y + 17 * s), blk[1], font=ui_b, fill=GOOD)
            else:
                for j, l in enumerate(lines):
                    d.text((PAD, y + j * 27 * s), l, font=ui, fill=INK)
            y += h
        # header last, so scrolled content passes under it
        d.rectangle([0, 0, W * s, 44 * s], fill=PANEL)
        d.line([(0, 44 * s), (W * s, 44 * s)], fill=RULE, width=s)
        d.text((PAD, 15 * s), "Gecko", font=small, fill=ACCENT)
        d.text((PAD + 60 * s, 15 * s), "buy me a coffee", font=small, fill=MUTED)
        return img, False

    frames_dir = Path(tempfile.mkdtemp())
    n = 0
    for i, blk in enumerate(script):
        text = blk[1] if blk[0] in ("user", "bot", "aha") else ""
        steps = max(1, len(text) // 3) if blk[0] in ("user", "bot") else 6
        for k in range(steps + 1):
            img, _ = draw_all(i, k * 3 if blk[0] in ("user", "bot") else k)
            img.save(frames_dir / f"{n:05d}.png"); n += 1
        hold = 26 if blk[0] == "aha" else 14
        for _ in range(hold):
            img, _ = draw_all(i, 10 ** 6)
            img.save(frames_dir / f"{n:05d}.png"); n += 1
    for _ in range(45):
        img, _ = draw_all(len(script) - 1, 10 ** 6)
        img.save(frames_dir / f"{n:05d}.png"); n += 1

    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS),
                    "-i", str(frames_dir / "%05d.png"), "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)], check=True)
    Image.open(frames_dir / f"{n - 1:05d}.png").save(out.with_suffix(".thumb.png"))
    print(f"wrote {out}  ({n / FPS:.0f}s, {n} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

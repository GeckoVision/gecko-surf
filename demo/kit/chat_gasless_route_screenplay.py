#!/usr/bin/env python3
"""The prompt, the MCP calls, the execution, the diagram. As a chat, straight to MP4.

Every card on screen is the answer of something that ran while this script ran:

- ``list_stores`` and ``plan_swap`` are REAL MCP calls to the Gecko store surface served
  locally (``python -m gecko.serve_mcp``, ``/orquestra/mcp``). The venue and the accounts
  the execution then uses are taken from the ``plan_swap`` answer, so the MCP call is
  load-bearing, not decoration.
- the execution is the fork route lane (``gecko.sandbox.rehearse_route``) on a local copy
  of mainnet with a Kora fee relay: convert USDG to USDC, buy the espresso, both fees paid
  by the relay, the buyer never holding SOL. Its trace is written to ``<out>.run.jsonl``
  and drawn to ``<out>.run.html``; the last seconds of the video scroll that drawing.

Nothing is typed in by hand. If a step fails, the video says so and stops.

    cd <worktree with the route lane>
    export KORA_RPC_URL=http://127.0.0.1:8080 KORA_API_KEY=...        # fork only
    export GECKO_MCP_URL=http://127.0.0.1:8765/orquestra/mcp
    uv run --with pillow python demo/kit/chat_gasless_route_screenplay.py out.mp4
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

REPO = Path(os.environ.get("GECKO_DEMO_REPO", Path.cwd()))
sys.path.insert(0, str(REPO))

FORK = os.environ.get("GECKO_FORK_RPC", "http://127.0.0.1:8899")
MCP = os.environ.get("GECKO_MCP_URL", "http://127.0.0.1:8765/orquestra/mcp")
USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
T22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
AMOUNT = 110_000  # 0.11 USDG, enough for a 0.10 espresso after the swap

W, H, FPS, SCALE = 1200, 676, 30, 2
BG, PANEL = "#faf9f7", "#ffffff"
INK, MUTED, RULE = "#1f1e1c", "#6b6862", "#e6e3dd"
USER_BG, ACCENT, GOOD, BAD = "#efece6", "#c96442", "#2f7d5f", "#b3422f"
F = "/usr/share/fonts/truetype/dejavu"


# --- the MCP client: plain JSON-RPC over Streamable HTTP, no SDK -----------------------
def _post(body, sid=None):
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if sid:
        headers["mcp-session-id"] = sid
    req = urllib.request.Request(MCP, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=180) as r:
        sid = r.headers.get("mcp-session-id") or sid
        raw = r.read().decode()
    if raw.startswith(("event:", "data:")) or "\ndata:" in raw:
        datas = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
        raw = datas[-1] if datas else "{}"
    return (json.loads(raw) if raw.strip() else {}), sid


def mcp_session() -> str:
    _, sid = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "gecko-chat-demo", "version": "0"}}})
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    return sid


def mcp_call(sid: str, name: str, args: dict, _id=[10]):
    _id[0] += 1
    res, _ = _post({"jsonrpc": "2.0", "id": _id[0], "method": "tools/call",
                    "params": {"name": name, "arguments": args}}, sid)
    r = res.get("result", res)
    text = next((c.get("text") for c in (r.get("content") or []) if c.get("type") == "text"), None)
    try:
        return json.loads(text) if text else r
    except Exception:  # noqa: BLE001 - a non-JSON answer is still an answer
        return {"text": text}


def short(a: str, n: int = 6) -> str:
    return f"{a[:n]}…{a[-4:]}" if a else "?"


# --- everything that runs, before a single frame is drawn ------------------------------
def live_run(out: Path) -> dict:
    from gecko.orquestra_build import orquestra_seams
    from gecko.providers.whirlpool import WHIRLPOOL_PROGRAM
    from gecko.sandbox import ephemeral_signer, prove_surfnet
    from gecko.sandbox.cheatcodes import fund_sol
    from gecko.sandbox.rehearse_route import RouteLeg, rehearse_gasless_route
    from gecko.trace import Trace
    from scripts.kora_relay import KoraRelay

    facts: dict = {}
    sid = mcp_session()
    stores = mcp_call(sid, "list_stores", {"network": "mainnet", "store": "geckocoffee"})
    store = (stores.get("stores") or [{}])[0]
    espresso = next(p for p in store.get("products", []) if p.get("name") == "Espresso")
    facts["espresso"] = espresso

    proof = prove_surfnet(FORK)
    buyer = ephemeral_signer(proof)
    relay = KoraRelay.from_env()
    fund_sol(proof, relay.pubkey, 50_000_000)  # the operator's balance, a cheatcode on a fork
    facts["buyer"] = buyer.pubkey
    facts["relay"] = relay.pubkey

    plan = mcp_call(sid, "plan_swap", {"input_mint": USDG, "output_mint": USDC,
                                       "amount_in": AMOUNT, "user": buyer.pubkey})
    if plan.get("refused") or "values" not in plan:
        facts["plan_refused"] = plan.get("reason") or plan.get("text") or str(plan)[:120]
        return facts
    facts["plan"] = {"pool": plan.get("pool"), "instruction": plan.get("instruction"),
                     "quote": plan.get("quote") or {}}

    idl_fetch, build_call = orquestra_seams()
    trace = Trace(lane="route", network="fork")
    route = rehearse_gasless_route(
        proof, buyer=buyer, relay=relay,
        convert=RouteLeg(program_id=WHIRLPOOL_PROGRAM, instruction="swap_v2",
                         values=plan["values"],
                         fund_tokens=[(USDG, AMOUNT, T22), (USDC, 0)],
                         idl_fetch=idl_fetch, build_call=build_call),
        store="geckocoffee", product="Espresso", table_number=1, trace=trace,
    )
    trace_path = out.with_suffix(".run.jsonl")
    trace.write(trace_path)
    facts["trace_rows"] = [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]
    facts["graph"] = None
    try:
        from scripts.trace_to_graph import render, spec_from_trace
        spec_path = out.with_suffix(".run.sequence.json")
        spec_path.write_text(json.dumps(spec_from_trace(trace), indent=2) + "\n")
        html = out.with_suffix(".run.html")
        if render(spec_path, html).get("rendered"):
            facts["graph"] = html
    except Exception as exc:  # noqa: BLE001 - the video still ships without the drawing
        facts["graph_error"] = str(exc)[:120]

    c, p = route.convert, route.purchase
    facts["convert"] = {"landed": c.landed, "sig": c.signature, "cu": c.compute_units,
                        "relay_sol": c.relay_sol.moved if c.relay_sol else None,
                        "refusals": [f"{r.step}: {r.reason}" for r in c.refusals]}
    facts["purchase"] = None if p is None else {
        "landed": p.landed, "sig": p.signature, "predicted": p.simulated_units,
        "charged": p.units_consumed, "relay_sol": p.relay_sol.moved if p.relay_sol else None,
        "refusals": [f"{r.step}: {r.reason}" for r in p.refusals]}
    facts["buyer_sol"] = (route.buyer_sol.before, route.buyer_sol.after)
    facts["relay_sol"] = route.relay_sol.moved
    facts["objections"] = list(route.objections)
    facts["ok"] = bool(route.landed and not route.objections)
    return facts


# --- the conversation, written from the facts --------------------------------------------
def blocks(f: dict) -> list:
    e = f["espresso"]
    out = [
        ("user", "Buy me an espresso at Gecko Coffee. I only have USDG, and no SOL."),
        ("tool", "list_stores  geckocoffee", f"Espresso {e['price_ui']} USDC  ·  mint {short(e['mint'])}  ·  classic SPL", "MCP"),
        ("bot", "The shop prices in USDC, classic SPL. You hold USDG, which is Token-2022: a different asset, not a different label. And with no SOL your wallet cannot pay a network fee on its own."),
    ]
    if "plan_refused" in f:
        out += [("tool", "plan_swap  USDG → USDC", f"refused: {f['plan_refused']}", "MCP"),
                ("bot", "No venue could be proven, so nothing was signed. That is the whole point.")]
        return out
    q = f["plan"]["quote"]
    out += [
        ("tool", "plan_swap  USDG → USDC", f"Orca pool {short(f['plan']['pool'])}  ·  {f['plan']['instruction']}  ·  accounts derived, nobody chose the venue", "MCP"),
        ("bot", "Two steps, one bill: convert, then buy. A fee relay pays the network, so you never need SOL. Running it on a local copy of mainnet."),
    ]
    c = f["convert"]
    if c["landed"]:
        out.append(("tool", "convert  USDG → USDC", f"landed {short(c['sig'], 8)}  ·  {c['cu']:,} CU  ·  relay paid the fee ({c['relay_sol']} lamports)", "fork"))
    else:
        out.append(("tool", "convert  USDG → USDC", f"not landed: {'; '.join(c['refusals'])[:80]}", "fork"))
        out.append(("bot", "The conversion did not land, so the purchase was not attempted. Nothing moved."))
        return out
    p = f["purchase"]
    if p and p["landed"]:
        out += [
            ("tool", "prepare + dry run  Espresso", f"receipt PASS  ·  predicted {p['predicted']:,} CU  ·  relay signs first, then the wallet", "fork"),
            ("tool", "land", f"landed {short(p['sig'], 8)}  ·  charged {p['charged']:,} CU  ·  relay paid the fee ({p['relay_sol']} lamports)", "fork"),
            ("bot", "Those were unsigned bytes and a receipt. The relay signed as fee payer, your wallet signed as the buyer. I never held a key."),
            ("aha", f"Predicted {p['predicted']:,} before signing. Charged {p['charged']:,} on chain. Your SOL: none, and none needed."),
            ("bot", f"Espresso paid. {e['price_ui']} USDC to the shop, converted from your USDG. The relay paid both fees: {-f['relay_sol']:,} lamports."),
        ]
    else:
        why = "; ".join(p["refusals"])[:80] if p else "not attempted"
        out.append(("tool", "prepare + dry run  Espresso", f"not landed: {why}", "fork"))
        out.append(("bot", "The purchase did not land. The refusal above is the reason, by name."))
    for line in f.get("objections", []):
        out.append(("tool", "objection", line[:90], "judge"))
    return out


# --- the renderer: a Claude-web-shaped chat, one frame at a time -------------------------
def render(script: list, out: Path, graph: Path | None) -> None:
    s = SCALE
    ui = ImageFont.truetype(f"{F}/DejaVuSans.ttf", 17 * s)
    ui_b = ImageFont.truetype(f"{F}/DejaVuSans-Bold.ttf", 17 * s)
    mono = ImageFont.truetype(f"{F}/DejaVuSansMono.ttf", 13 * s)
    mono_b = ImageFont.truetype(f"{F}/DejaVuSansMono-Bold.ttf", 13 * s)
    small = ImageFont.truetype(f"{F}/DejaVuSans-Bold.ttf", 12 * s)
    tag_f = ImageFont.truetype(f"{F}/DejaVuSansMono-Bold.ttf", 10 * s)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    def wrap(text, font, width):
        words, lines, cur = text.split(), [], ""
        for w_ in words:
            t = f"{cur} {w_}".strip()
            if probe.textlength(t, font=font) <= width:
                cur = t
            else:
                lines.append(cur)
                cur = w_
        if cur:
            lines.append(cur)
        return lines

    PAD, MAXW = 56 * s, (W - 200) * s

    def measure(upto, reveal):
        items = []
        for i, blk in enumerate(script[: upto + 1]):
            kind, partial = blk[0], i == upto
            if kind == "user":
                txt = blk[1][:reveal] if partial else blk[1]
                lines = wrap(txt, ui, MAXW - 40 * s) or [""]
                items.append((blk, lines, len(lines) * 26 * s + 24 * s + 22 * s))
            elif kind in ("tool", "aha"):
                if partial and reveal < 3:
                    continue
                items.append((blk, None, (62 if kind == "tool" else 56) * s + (18 if kind == "tool" else 20) * s))
            else:
                txt = blk[1][:reveal] if partial else blk[1]
                lines = wrap(txt, ui, MAXW) or [""]
                items.append((blk, lines, len(lines) * 27 * s + 22 * s))
        return items

    def draw_all(upto, reveal):
        img = Image.new("RGB", (W * s, H * s), BG)
        d = ImageDraw.Draw(img)
        items = measure(upto, reveal)
        total = sum(h for _, _, h in items)
        top, avail = 74 * s, (H - 92) * s
        y = top + min(0, avail - total)
        for blk, lines, h in items:
            kind = blk[0]
            if kind == "user":
                wpx = max(d.textlength(line, font=ui) for line in lines) + 40 * s
                x0 = W * s - PAD - wpx
                d.rounded_rectangle([x0, y, W * s - PAD, y + h - 22 * s], 14 * s, fill=USER_BG)
                for j, line in enumerate(lines):
                    d.text((x0 + 20 * s, y + 12 * s + j * 26 * s), line, font=ui, fill=INK)
            elif kind == "tool":
                bad = blk[2].startswith(("not landed", "refused")) or blk[1] == "objection"
                d.rounded_rectangle([PAD, y, W * s - PAD, y + 62 * s], 10 * s, fill=PANEL, outline=RULE, width=s)
                d.text((PAD + 18 * s, y + 12 * s), blk[1], font=mono_b, fill=BAD if bad else ACCENT)
                d.text((PAD + 18 * s, y + 34 * s), blk[2], font=mono, fill=MUTED)
                tag = blk[3] if len(blk) > 3 else ""
                if tag:
                    tw = d.textlength(tag, font=tag_f) + 16 * s
                    d.rounded_rectangle([W * s - PAD - tw - 14 * s, y + 12 * s, W * s - PAD - 14 * s, y + 30 * s], 5 * s, outline=RULE, width=s)
                    d.text((W * s - PAD - tw - 6 * s, y + 15 * s), tag, font=tag_f, fill=MUTED)
            elif kind == "aha":
                d.rounded_rectangle([PAD, y, W * s - PAD, y + 56 * s], 10 * s, fill="#eef5f1")
                d.rectangle([PAD, y + 6 * s, PAD + 4 * s, y + 50 * s], fill=GOOD)
                d.text((PAD + 22 * s, y + 17 * s), blk[1], font=ui_b, fill=GOOD)
            else:
                for j, line in enumerate(lines):
                    d.text((PAD, y + j * 27 * s), line, font=ui, fill=INK)
            y += h
        d.rectangle([0, 0, W * s, 44 * s], fill=PANEL)
        d.line([(0, 44 * s), (W * s, 44 * s)], fill=RULE, width=s)
        d.text((PAD, 15 * s), "Gecko", font=small, fill=ACCENT)
        d.text((PAD + 60 * s, 15 * s), "buy me an espresso  ·  gecko-store MCP, local  ·  fork of mainnet", font=small, fill=MUTED)
        return img

    frames_dir = Path(tempfile.mkdtemp())
    n = 0
    for i, blk in enumerate(script):
        text = blk[1] if blk[0] in ("user", "bot", "aha") else ""
        steps = max(1, len(text) // 2) if blk[0] in ("user", "bot") else 8
        for k in range(steps + 1):
            draw_all(i, k * 2 if blk[0] in ("user", "bot") else k).save(frames_dir / f"{n:05d}.png")
            n += 1
        for _ in range({"aha": 75, "tool": 42, "user": 24}.get(blk[0], 33)):
            draw_all(i, 10**6).save(frames_dir / f"{n:05d}.png")
            n += 1
    for _ in range(75):
        draw_all(len(script) - 1, 10**6).save(frames_dir / f"{n:05d}.png")
        n += 1
    Image.open(frames_dir / f"{n - 1:05d}.png").save(out.with_suffix(".thumb.png"))

    chat_mp4 = out.with_suffix(".chat.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(frames_dir / "%05d.png"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(chat_mp4)], check=True)

    # the tail: the run's own sequence diagram, captured full-page and scrolled through
    if graph and graph.exists():
        png = out.with_suffix(".run.png")
        try:
            subprocess.run(["agent-browser", "--args", "--no-sandbox", "open", f"file://{graph}"], check=True, capture_output=True, timeout=60)
            subprocess.run(["agent-browser", "set", "viewport", str(W), str(H)], check=True, capture_output=True, timeout=30)
            subprocess.run(["agent-browser", "screenshot", "--full", str(png)], check=True, capture_output=True, timeout=60)
            subprocess.run(["agent-browser", "close"], capture_output=True, timeout=30)
            ih = Image.open(png).size[1]
            travel = max(0, ih - H)
            tail = out.with_suffix(".tail.mp4")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(png), "-t", "10",
                            "-vf", f"crop={W}:{H}:0:'min({travel},max(0,(t-1.5)*{max(1, travel / 6):.0f}))',scale={W * s}:{H * s}:flags=lanczos,format=yuv420p",
                            "-r", str(FPS), str(tail)], check=True)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(chat_mp4), "-i", str(tail), "-filter_complex",
                            "[0:v]format=yuv420p,setsar=1[a];[1:v]format=yuv420p,setsar=1[b];[a][b]concat=n=2:v=1:a=0[v]",
                            "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-movflags", "+faststart", str(out)], check=True)
            tail.unlink(missing_ok=True)
            chat_mp4.unlink(missing_ok=True)
            print(f"wrote {out}  (chat {n / FPS:.0f}s + diagram 10s)")
            return
        except Exception as exc:  # noqa: BLE001 - ship the chat alone, say why
            print(f"diagram tail skipped: {type(exc).__name__}: {str(exc)[:120]}")
    chat_mp4.rename(out)
    print(f"wrote {out}  ({n / FPS:.0f}s, {n} frames)")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "demo/kit/chat_gasless_route.mp4").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    facts = live_run(out)
    script = blocks(facts)
    for blk in script:
        print("  ", blk[0], "|", blk[1][:70], "|", (blk[2][:70] if len(blk) > 2 else ""))
    render(script, out, facts.get("graph"))
    return 0 if facts.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

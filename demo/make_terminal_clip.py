"""Render the demo terminal clip — real `gecko` output on the Swagger Petstore.

Two steps (the second pads the GIF onto a 1920x1080 canvas for video editors):

    uv run --with pillow python demo/make_terminal_clip.py demo/terminal.gif
    ffmpeg -i demo/terminal.gif \
      -vf "fps=30,pad=1920:1080:(1920-iw)/2:(1080-ih)/2:color=0x0d1117,format=yuv420p" \
      -c:v libx264 -preset medium -crf 18 demo/gecko-terminal.mp4

The committed `demo/gecko-terminal.mp4` is the ready artifact; regenerate only if
the printed output changes. Every line below mirrors the real CLI output.
"""

import glob
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1500, 720
BG = (13, 17, 23)
FG = (201, 209, 217)
GREEN = (63, 185, 80)
CYAN = (88, 166, 255)
MUTED = (125, 133, 144)
WHITE = (240, 246, 252)


def font(pats, size):
    for p in pats:
        h = glob.glob(p, recursive=True)
        if h:
            return ImageFont.truetype(h[0], size)
    return ImageFont.load_default()


MONO = font(
    [
        "/usr/share/fonts/**/DejaVuSansMono.ttf",
        "/usr/share/fonts/**/LiberationMono-Regular.ttf",
    ],
    23,
)
_m = ImageDraw.Draw(Image.new("RGB", (1, 1)))
PAD, LH, TOP = 30, 38, 64

LINES = [
    [("$ ", GREEN), ("gecko https://petstore3.swagger.io/api/v3/openapi.json", WHITE)],
    None,
    [("Gecko — make any API agent-usable (gecko-surf)", CYAN)],
    [("=" * 46, MUTED)],
    [
        ("comprehended ", FG),
        ("19", GREEN),
        (" operations -> ", FG),
        ("10", GREEN),
        (" usable as tools", FG),
    ],
    [("(9 auth-gated hidden from the agent)", MUTED)],
    [("Control plane: stores only the API surface — never your data.", MUTED)],
    None,
    [("MCP URL:  ", FG), ("http://127.0.0.1:8000/mcp", CYAN)],
    None,
    [("Add it to your agent (one step):", FG)],
    [
        ("  Claude Code:  ", FG),
        ("claude mcp add --transport http swagger-petstore …", GREEN),
    ],
    [
        ("  Cursor:       ", FG),
        ("cursor://…/mcp/install?name=swagger-petstore…", MUTED),
    ],
    [("  VS Code:      ", FG), ("vscode:mcp/install?…", MUTED)],
]


def render(n):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 44], fill=(22, 27, 34))
    for i, c in enumerate([(237, 106, 94), (245, 191, 79), (98, 197, 84)]):
        d.ellipse([22 + i * 28, 15, 40 + i * 28, 33], fill=c)
    d.text((W // 2 - 30, 12), "gecko", font=MONO, fill=MUTED)
    y = TOP
    for line in LINES[:n]:
        if line is None:
            y += LH
            continue
        x = PAD
        for text, color in line:
            d.text((x, y), text, font=MONO, fill=color)
            x += int(_m.textlength(text, font=MONO))
        y += LH
    return img


# typed command holds, then output reveals line-by-line, then a final hold
frames, durs = [render(1)], [1300]
for n in range(3, len(LINES) + 1):
    frames.append(render(n))
    durs.append(360)
frames.append(render(len(LINES)))
durs.append(6000)

out = sys.argv[1] if len(sys.argv) > 1 else "demo/terminal.gif"
frames[0].save(
    out, save_all=True, append_images=frames[1:], duration=durs, loop=0, optimize=True
)
print("wrote", out, "frames", len(frames), "total_ms", sum(durs))

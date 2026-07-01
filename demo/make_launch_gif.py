"""Launch GIF — the one-command story, rendered from REAL captured `gecko` output.

    uv run --with pillow python make_launch_gif.py out.gif

Every line mirrors real CLI output:
- install line = the shipped one-liner; "gecko-surf installed" = install.sh's echo
- comprehension numbers (19 -> 10, 9 auth-gated) = a real `gecko` run on the public
  Swagger Petstore (captured 2026-07-01 via PTY).
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
    ["/usr/share/fonts/**/DejaVuSansMono.ttf", "/usr/share/fonts/**/LiberationMono-Regular.ttf"],
    22,
)
_m = ImageDraw.Draw(Image.new("RGB", (1, 1)))
PAD, LH, TOP = 30, 37, 62

LINES = [
    [("$ ", GREEN), ("curl -fsSL https://get.geckovision.tech/install.sh | bash", WHITE)],
    [("  ✓ ", GREEN), ("gecko-surf installed", FG)],
    None,
    [("# point it at an OpenAPI:", MUTED)],
    [("$ ", GREEN), ("gecko https://petstore3.swagger.io/api/v3/openapi.json", WHITE)],
    [("  comprehended ", FG), ("19", GREEN), (" ops -> ", FG), ("10", GREEN), (" first-call-correct tools", FG)],
    [("  MCP ready", CYAN), ("  ·  one-click add to Claude / Cursor", MUTED)],
    None,
    [("# no OpenAPI? comprehend the docs:", MUTED)],
    [("$ ", GREEN), ("gecko from-docs https://docs.some-api.com", WHITE)],
    [("  recovered ", FG), ("3", GREEN), (" ops -> comprehended ", FG), ("3", GREEN), (" agent tools", FG)],
    [("  + honest review notes for the fields to confirm", MUTED)],
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


frames, durs = [render(1)], [1200]
for n in range(2, len(LINES) + 1):
    frames.append(render(n))
    durs.append(320)
frames.append(render(len(LINES)))
durs.append(5000)

out = sys.argv[1] if len(sys.argv) > 1 else "launch.gif"
frames[0].save(out, save_all=True, append_images=frames[1:], duration=durs, loop=0, optimize=True)
print("wrote", out, "frames", len(frames), "total_ms", sum(durs))

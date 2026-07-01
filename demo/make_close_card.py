"""Render the demo closing card — Gecko name + tagline + install line, 1920x1080.

    uv run --with pillow python demo/make_close_card.py demo/close-card.png

Turn it into a held clip with:

    ffmpeg -loop 1 -i demo/close-card.png -t 6.6 \
      -vf "fps=30,fade=t=in:st=0:d=0.6,format=yuv420p" -c:v libx264 -crf 18 demo/close.mp4
"""

import glob
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
BG = (13, 17, 23)
WHITE = (240, 246, 252)
CYAN = (88, 166, 255)
GREEN = (63, 185, 80)
MUTED = (125, 133, 144)


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    h = glob.glob(f"/usr/share/fonts/**/{name}", recursive=True)
    return ImageFont.truetype(h[0], size) if h else ImageFont.load_default()


def mono(size):
    h = glob.glob("/usr/share/fonts/**/DejaVuSansMono.ttf", recursive=True)
    return ImageFont.truetype(h[0], size) if h else ImageFont.load_default()


img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)


def center(y, text, f, fill):
    w = d.textlength(text, font=f)
    d.text(((W - w) / 2, y), text, font=f, fill=fill)


center(360, "Gecko", font(120, bold=True), WHITE)
center(530, "Make any API agent-usable.", font(54), CYAN)
center(600, "No integration code.", font(54), MUTED)

pip = "pip install gecko-surf"
pf = mono(46)
pw = d.textlength(pip, font=pf)
px, py = (W - pw) / 2, 730
d.rounded_rectangle(
    [px - 36, py - 20, px + pw + 36, py + 70], radius=18, fill=(22, 27, 34)
)
d.text((px, py), pip, font=pf, fill=GREEN)

center(880, "github.com/GeckoVision/gecko-surf", font(30), MUTED)

img.save(sys.argv[1] if len(sys.argv) > 1 else "demo/close-card.png")
print("wrote", sys.argv[1] if len(sys.argv) > 1 else "demo/close-card.png")

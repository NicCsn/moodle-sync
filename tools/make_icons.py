"""Erzeugt die App-Symbole in assets/ (braucht Pillow). Aufruf: python tools/make_icons.py"""
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets"
BLUE = (15, 108, 191, 255)
WHITE = (255, 255, 255, 255)


def glyph(d: ImageDraw.ImageDraw, box: tuple[float, float, float, float], color, w: float) -> None:
    """Pfeil nach unten in eine offene Ablage."""
    x0, y0, x1, y1 = box
    W, H = x1 - x0, y1 - y0
    cx = x0 + W / 2
    # Ablage
    ty = y0 + H * 0.62
    d.line([(x0 + W * 0.08, ty), (x0 + W * 0.08, y1 - H * 0.06), (x1 - W * 0.08, y1 - H * 0.06), (x1 - W * 0.08, ty)],
           fill=color, width=round(w), joint="curve")
    # Pfeil
    d.line([(cx, y0 + H * 0.06), (cx, y0 + H * 0.66)], fill=color, width=round(w))
    a = W * 0.2
    d.line([(cx - a, y0 + H * 0.46), (cx, y0 + H * 0.68), (cx + a, y0 + H * 0.46)], fill=color, width=round(w),
           joint="curve")


def app_icon(size: int = 1024) -> Image.Image:
    s = 4  # überabtasten für glatte Kanten
    S = size * s
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    m = S * 0.1  # macOS-Rand
    d.rounded_rectangle([m, m, S - m, S - m], radius=S * 0.18, fill=BLUE)
    p = S * 0.3
    glyph(d, (p, p, S - p, S - p), WHITE, S * 0.045)
    return im.resize((size, size), Image.LANCZOS)


def tray_icon(size: int, color) -> Image.Image:
    s = 8
    S = size * s
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    glyph(d, (S * 0.06, S * 0.04, S * 0.94, S * 0.96), color, S * 0.11)
    return im.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    big = app_icon()
    big.save(OUT / "icon.png")
    big.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    tray_icon(44, (0, 0, 0, 255)).save(OUT / "tray-mac.png")       # Vorlage: macOS färbt hell/dunkel selbst
    win = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(win).rounded_rectangle([2, 2, 61, 61], radius=12, fill=BLUE)
    win.alpha_composite(tray_icon(44, WHITE), (10, 10))
    win.save(OUT / "tray-win.png")
    if sys.platform == "darwin":
        iconset = OUT / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for px in (16, 32, 128, 256, 512):
            app_icon(px).save(iconset / f"icon_{px}x{px}.png")
            app_icon(px * 2).save(iconset / f"icon_{px}x{px}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT / "icon.icns")], check=True)
        for f in iconset.iterdir():
            f.unlink()
        iconset.rmdir()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate macOS menu-bar template icons from the existing app icon.

Produces:
  menubar-icon.png        — 22x22 (@1x for non-retina screens)
  menubar-icon@2x.png     — 44x44 (@2x for retina; what most users see)

These are 'template images' — black with alpha. macOS auto-inverts to white
on dark menu bars. The shape is derived from icon.icns by extracting the
cream-on-green app icon and converting the dark green G to black.
"""

import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

# Source: the 1024x1024 PNG we used to build icon.icns
SOURCE_PNG = Path("/tmp/granola-icons/single-D.png")
OUT_DIR = ROOT


def rgb_to_brightness(r: int, g: int, b: int) -> int:
    return int(0.299 * r + 0.587 * g + 0.114 * b)


def derive_template(src: Image.Image, size: int) -> Image.Image:
    """Convert the colored app icon to a black-with-alpha template at the
    given size. Pixels darker than a threshold become opaque black; lighter
    pixels become transparent. The amber accent gets included since it's
    darker than cream.
    """
    # Work at high resolution then downsample for crisp edges.
    work_size = max(size * 8, 256)
    big = src.convert("RGBA").resize((work_size, work_size), Image.LANCZOS)
    out = Image.new("RGBA", (work_size, work_size), (0, 0, 0, 0))

    px_in = big.load()
    px_out = out.load()
    for y in range(work_size):
        for x in range(work_size):
            r, g, b, a = px_in[x, y]
            if a == 0:
                continue
            # Cream background ~ #F7F4EE. Anything darker than ~#D0D0D0 is "ink".
            brightness = rgb_to_brightness(r, g, b)
            if brightness < 200:
                # Map brightness 0..199 → alpha 255..0 (smooth edges)
                alpha = max(0, min(255, int((200 - brightness) * 255 / 120)))
                px_out[x, y] = (0, 0, 0, alpha)

    return out.resize((size, size), Image.LANCZOS)


def main():
    if not SOURCE_PNG.exists():
        # Fallback: render a clean G with PIL drawing
        print(f"Source {SOURCE_PNG} missing — drawing a fresh G instead")
        for size, suffix in [(22, ""), (44, "@2x")]:
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            font_paths = [
                "/System/Library/Fonts/SFNSDisplay-Bold.otf",
                "/System/Library/Fonts/Helvetica.ttc",
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            ]
            font = None
            for fp in font_paths:
                if Path(fp).exists():
                    try:
                        font = ImageFont.truetype(fp, int(size * 0.85))
                        break
                    except Exception:
                        continue
            if font is None:
                font = ImageFont.load_default()
            # centred draw
            bbox = draw.textbbox((0, 0), "G", font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1] - 1),
                      "G", font=font, fill=(0, 0, 0, 255))
            out = OUT_DIR / f"menubar-icon{suffix}.png"
            img.save(out)
            print(f"Wrote {out}")
        return 0

    src = Image.open(SOURCE_PNG)
    for size, suffix in [(22, ""), (44, "@2x")]:
        out_img = derive_template(src, size)
        out = OUT_DIR / f"menubar-icon{suffix}.png"
        out_img.save(out)
        print(f"Wrote {out}  ({size}x{size})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

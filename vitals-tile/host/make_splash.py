#!/usr/bin/env python3
"""Generate the tile's boot splash: preview PNG + splash.bin for the Pico.

    ./.venv/bin/python host/make_splash.py

Writes preview/splash.png and firmware/splash.bin (1024 bytes, page format).
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from render import HEIGHT, WIDTH, preview, to_ssd1306

ASSETS = Path(__file__).resolve().parent.parent / "assets"
OUT = Path(__file__).resolve().parent.parent


def main() -> None:
    img = Image.new("1", (WIDTH, HEIGHT), 0)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=1)

    small = ImageFont.truetype(str(ASSETS / "Silkscreen-Regular.ttf"), 8)
    big = ImageFont.truetype(str(ASSETS / "Silkscreen-Bold.ttf"), 16)

    def center(text: str, font, y: int) -> None:
        bbox = font.getbbox(text)
        d.text((round((WIDTH - font.getlength(text)) / 2), y - bbox[1]), text, font=font, fill=1)

    center("VITALS TILE", small, 6)
    center("WAITING", big, 19)
    center("FOR HOST", big, 33)
    center("run the host script", small, 51)

    preview(img, 4).save(OUT / "preview" / "splash.png")
    (OUT / "firmware" / "splash.bin").write_bytes(to_ssd1306(img))
    print("Wrote preview/splash.png and firmware/splash.bin")


if __name__ == "__main__":
    main()

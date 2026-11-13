"""Render a Reading as a 128x64 1-bit frame, SSD1306 ready.

Layout (pixels):

    0                                       127
    +------------------------------------------+
    | MAXED OUT                        . . .   |  status word + activity pulse
    | CPU  [###############.......]      78%   |
    | GPU  [#####.................]      21%   |
    | RAM  [##############........]      74%   |
    | 62°C            38W              1180RPM |
    +------------------------------------------+
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from vitals import Reading

WIDTH, HEIGHT = 128, 64
ASSETS = Path(__file__).resolve().parent.parent / "assets"


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(ASSETS / name), size)


class Renderer:
    def __init__(self) -> None:
        self.small = _font("Silkscreen-Regular.ttf", 8)
        self.bold = _font("Silkscreen-Bold.ttf", 8)
        self._bbox_cache: dict[tuple[str, int], tuple[int, int, int, int]] = {}

    # -- text helpers ------------------------------------------------------
    def _bbox(self, text: str, font) -> tuple[int, int, int, int]:
        key = (text, id(font))
        box = self._bbox_cache.get(key)
        if box is None:
            box = font.getbbox(text)
            self._bbox_cache[key] = box
        return box

    def text(self, draw: ImageDraw.ImageDraw, x: int, y: int, text: str, font, fill=1):
        """Draw text with the ink box's top-left at (x, y)."""
        draw.text((x, y - self._bbox(text, font)[1]), text, font=font, fill=fill)

    def text_right(self, draw: ImageDraw.ImageDraw, right: int, y: int, text: str, font, fill=1):
        self.text(draw, round(right - font.getlength(text)), y, text, font, fill)

    # -- widgets -----------------------------------------------------------
    @staticmethod
    def bar(draw, x: int, y: int, w: int, h: int, pct: float, fill=1):
        draw.rectangle((x, y, x + w - 1, y + h - 1), outline=fill)
        inner = w - 4
        filled = round(inner * max(0.0, min(100.0, pct)) / 100.0)
        if filled > 0:
            draw.rectangle((x + 2, y + 2, x + 1 + filled, y + h - 3), fill=fill)

    def pulse(self, draw, phase: float, x_right: int = 121, y: int = 3):
        """Three little squares, one lit at a time, to show data is live."""
        step = int(phase * 3) % 3
        for i in range(3):
            x = x_right - (2 - i) * 6
            if i == step:
                draw.rectangle((x, y, x + 3, y + 3), fill=1)
            else:
                draw.rectangle((x, y, x + 3, y + 3), outline=1)

    # -- frame -------------------------------------------------------------
    def render(self, r: Reading) -> Image.Image:
        img = Image.new("1", (WIDTH, HEIGHT), 0)
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=1)

        word, alarm = status_word(r.busiest)
        if alarm:
            w = round(self.bold.getlength(word))
            d.rectangle((3, 2, 5 + w, 10), fill=1)
            self.text(d, 4, 3, word, self.bold, fill=0)
        else:
            self.text(d, 4, 3, word, self.small, fill=1)
        self.pulse(d, r.phase)

        rows = (("CPU", r.cpu, 12), ("GPU", r.gpu, 27), ("RAM", r.mem, 42))
        for label, pct, y in rows:
            self.text(d, 4, y + 3, label, self.bold)
            self.bar(d, 24, y, 70, 11, pct)
            self.text_right(d, 123, y + 3, f"{round(pct):d}%", self.bold)

        temp = f"{round(r.cpu_temp):d}\u00b0C" if r.cpu_temp is not None else "--\u00b0C"
        watts = f"{round(r.watts):d}W" if r.watts is not None else "--W"
        rpm = f"{round(r.fan_rpm / 10) * 10:d}RPM" if r.fan_rpm is not None else "--RPM"
        self.text(d, 4, 54, temp, self.small)
        self.text(d, 48, 54, watts, self.small)
        self.text_right(d, 123, 54, rpm, self.small)
        return img


def status_word(load: float) -> tuple[str, bool]:
    """Plain-language verdict for people who don't read gauges."""
    if load < 8:
        return "IDLE", False
    if load < 35:
        return "LIGHT", False
    if load < 65:
        return "BUSY", False
    if load < 90:
        return "HEAVY", False
    return "MAXED OUT", True


def to_ssd1306(img: Image.Image) -> bytes:
    """PIL '1' image (row-major, MSB=left) -> SSD1306 page format (LSB=top)."""
    img = img.convert("1")
    px = img.load()
    out = bytearray(WIDTH * HEIGHT // 8)
    for page in range(HEIGHT // 8):
        base = page * 8
        offset = page * WIDTH
        for x in range(WIDTH):
            byte = 0
            for bit in range(8):
                if px[x, base + bit]:
                    byte |= 1 << bit
            out[offset + x] = byte
    return bytes(out)


def preview(img: Image.Image, scale: int = 4) -> Image.Image:
    return img.convert("L").resize((WIDTH * scale, HEIGHT * scale), Image.NEAREST)

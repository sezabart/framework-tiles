#!/usr/bin/env python3
"""Stream PC vitals to the SSD1306 tile on a Feather RP2040 USB Host.

Quick start:
    ./run.sh                      # live vitals (auto-detects the OLED board)
    ./run.sh --png preview.png    # still frame of this machine, no hardware
    ./run.sh --gif demo.gif       # scripted "local AI" animation, no hardware
    ./run.sh --simulate           # send a fake AI workload to the OLED
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import serial
from serial.tools import list_ports

from render import HEIGHT, WIDTH, Renderer, preview, to_ssd1306
from vitals import Reading, VitalsSampler

MAGIC = b"\xa5\x5a"
HANDSHAKE = b"VIT?\n"
HANDSHAKE_REPLY = b"VIT1"
BAUD = 921600


# --------------------------------------------------------------------------
# Simulated load: what an on-device LLM session feels like from the outside
# --------------------------------------------------------------------------
class SimulatedSampler:
    """10 second loop: idle -> prompt burst -> token generation -> cooldown."""

    def __init__(self) -> None:
        self._start = time.monotonic()
        self._phase = 0.0
        self._last = self._start

    @staticmethod
    def _smoothstep(a: float, b: float, t: float) -> float:
        t = max(0.0, min(1.0, (t - a) / (b - a)))
        return t * t * (3 - 2 * t)

    def sample(self) -> Reading:
        now = time.monotonic()
        self._phase += now - self._last
        self._last = now
        return self.at((now - self._start) % 10.0, self._phase)

    def at(self, t: float, phase: float | None = None) -> Reading:
        """Reading for a point in the 10 second loop (t in seconds)."""
        t %= 10.0

        idle = 1 - self._smoothstep(0.0, 2.0, t)
        prompt = self._smoothstep(2.0, 3.0, t) * (1 - self._smoothstep(4.0, 5.0, t))
        tokens = self._smoothstep(4.0, 5.5, t) * (1 - self._smoothstep(8.0, 9.5, t))
        token_jitter = 2.0 * math.sin(t * 9.0) * tokens

        cpu = 4 + 78 * prompt + 40 * tokens + idle * 1.0
        gpu = 2 + 40 * prompt + 93 * tokens + token_jitter
        watts = 18 + 70 * prompt + 100 * tokens
        temp = 41 + 10 * prompt + 32 * tokens
        fan = 700 + 400 * prompt + 2000 * tokens
        mem = 58 + 2.4 * t

        return Reading(
            cpu=max(0.0, min(100.0, cpu)),
            gpu=max(0.0, min(100.0, gpu)),
            mem=mem,
            cpu_temp=temp,
            watts=watts,
            fan_rpm=fan,
            phase=t if phase is None else phase,
        )


# --------------------------------------------------------------------------
# Serial transport
# --------------------------------------------------------------------------
def candidate_ports() -> list[str]:
    """Prefer the CDC data interface, never poke the console REPL first."""
    ports = list(list_ports.comports())
    circuitpython = [p for p in ports if p.vid == 0x2E8A]
    data = [p for p in circuitpython if p.interface and "CDC2" in p.interface]
    ordered = data + [p for p in circuitpython if p not in data]
    if not ordered:
        ordered = [p for p in ports if "ACM" in p.device or "USB" in p.device]
    seen: list[str] = []
    for port in ordered:
        if port.device not in seen and Path(port.device).exists():
            seen.append(port.device)
    return seen


def connect(explicit: str | None = None, verbose: bool = True) -> serial.Serial:
    candidates = [explicit] if explicit else candidate_ports()
    if not candidates:
        raise SystemExit(
            "No serial port found. Plug the Feather in and check `ls /dev/ttyACM*`."
        )
    for port in candidates:
        try:
            ser = serial.Serial(port, BAUD, timeout=0.4, write_timeout=1.0)
        except serial.SerialException:
            continue
        try:
            time.sleep(0.15)
            ser.reset_input_buffer()
            ser.write(HANDSHAKE)
            deadline = time.monotonic() + 0.8
            while time.monotonic() < deadline:
                if HANDSHAKE_REPLY in ser.read(32):
                    if verbose:
                        print(f"Connected to OLED tile on {port}")
                    return ser
            ser.close()
        except (serial.SerialException, OSError):
            try:
                ser.close()
            except Exception:
                pass
    raise SystemExit(
        "Found serial ports (%s) but none answered the handshake. "
        "Is the OLED firmware flashed and the display wired?" % ", ".join(candidates)
    )


def send_frame(ser: serial.Serial, frame: bytes) -> None:
    ser.write(MAGIC + frame)
    ser.flush()


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------
def stream(args, sampler, renderer: Renderer) -> None:
    ser = connect(args.port)
    period = 1.0 / args.fps
    next_tick = time.monotonic()
    frames = 0
    started = time.monotonic()
    try:
        while True:
            reading = sampler.sample()
            send_frame(ser, to_ssd1306(renderer.render(reading)))
            frames += 1
            if args.verbose and frames % int(args.fps * 5) == 0:
                print(
                    f"  CPU {reading.cpu:5.1f}%  GPU {reading.gpu:5.1f}%  "
                    f"RAM {reading.mem:4.1f}%  {reading.cpu_temp or 0:.0f}C  "
                    f"{reading.watts or 0:.0f}W  {reading.fan_rpm or 0}rpm"
                )
            next_tick += period
            time.sleep(max(0.0, next_tick - time.monotonic()))
    except KeyboardInterrupt:
        print(f"\nStopped after {time.monotonic() - started:.0f}s ({frames} frames).")
    except (serial.SerialException, OSError) as exc:
        raise SystemExit(f"OLED tile disconnected: {exc}")
    finally:
        ser.close()


def palette_image(img, scale: int, colors=((10, 12, 18), (205, 240, 255))):
    """Scale a 1-bit frame and give it an OLED-ish two-colour palette."""
    from PIL import Image

    size = (WIDTH * scale, HEIGHT * scale)
    mask = img.resize(size, Image.NEAREST).convert("L").point(lambda v: 255 if v else 0)
    out = Image.new("P", size)
    flat = [c for rgb in colors for c in rgb]
    out.putpalette(flat + [0, 0, 0] * (256 - len(colors)))
    out.paste(1, (0, 0), mask)
    return out


def write_png(args, sampler, renderer: Renderer) -> None:
    if isinstance(sampler, VitalsSampler):
        # prime the CPU delta so a one-shot capture is not a 1 ms snapshot
        sampler.sample()
        time.sleep(0.5)
        reading = sampler.sample(alpha=1.0)
    else:
        reading = sampler.sample()
    img = renderer.render(reading)
    out = Path(args.png)
    out.parent.mkdir(parents=True, exist_ok=True)
    preview(img, args.scale).save(out)
    print(
        f"Wrote {out} ({WIDTH}x{HEIGHT} frame, {args.scale}x preview) - "
        f"CPU {reading.cpu:.0f}% GPU {reading.gpu:.0f}% RAM {reading.mem:.0f}%"
    )


def write_gif(args, renderer: Renderer) -> None:
    sim = SimulatedSampler()
    frames = []
    steps = int(args.seconds * args.fps)
    for i in range(steps):
        t = i / args.fps
        reading = sim.at(t)
        frames.append(palette_image(renderer.render(reading), args.scale))

    out = Path(args.gif)
    out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=int(1000 / args.fps),
        loop=0,
        optimize=True,
    )
    print(f"Wrote {out} ({len(frames)} frames, {args.seconds:g}s loop, {args.scale}x)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="serial port of the tile (default: auto-detect)")
    parser.add_argument("--fps", type=float, default=2.0, help="frames per second (default 2)")
    parser.add_argument("--simulate", action="store_true", help="send a fake AI workload")
    parser.add_argument("--png", help="save one frame of real data as PNG and exit")
    parser.add_argument("--gif", help="save a scripted demo animation and exit")
    parser.add_argument("--seconds", type=float, default=10.0, help="GIF loop length (default 10)")
    parser.add_argument("--scale", type=int, default=4, help="preview scale factor (default 4)")
    parser.add_argument("--quiet", dest="verbose", action="store_false")
    args = parser.parse_args()

    renderer = Renderer()

    if args.gif:
        write_gif(args, renderer)
        return

    sampler = SimulatedSampler() if args.simulate else VitalsSampler()

    if args.png:
        write_png(args, sampler, renderer)
        return

    if isinstance(sampler, VitalsSampler):
        print(f"Sensors: {sampler.source_summary}")
    stream(args, sampler, renderer)


if __name__ == "__main__":
    main()

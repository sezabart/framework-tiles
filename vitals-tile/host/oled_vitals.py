#!/usr/bin/env python3
"""Stream PC vitals to the SSD1306 tile on a Feather RP2040 USB Host.

The tile is identified by its USB serial (the RP2040 chip UID) and by the
firmware's identity reply, so other serial devices - including other RP2040
boards - are never opened or written to.

Quick start:
    ./run.sh                      # live vitals, reconnects forever
    ./run.sh --list               # show serial ports and which one is the tile
    ./run.sh --id                 # print the detected tile identity
    ./run.sh --png preview.png    # still frame of this machine, no hardware
    ./run.sh --gif demo.gif       # scripted "local AI" animation, no hardware
    ./run.sh --simulate           # send a fake AI workload to the OLED
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import serial
from serial.tools import list_ports

from render import HEIGHT, WIDTH, Renderer, preview, to_ssd1306
from vitals import Reading, VitalsSampler

MAGIC = b"\xa5\x5a"
HANDSHAKE = b"VIT?\n"
HANDSHAKE_REPLY = b"VIT1"
BAUD = 921600
RASPBERRY_PI_VID = 0x2E8A

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "tile.json"

CONFIG_DEFAULTS = {
    "serial": None,      # USB serial / chip UID of the tile, e.g. "DF6160D2632A2926"
    "board_id": None,    # e.g. "adafruit_feather_rp2040_usb_host"
    "fps": 2.0,
}


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
# Tile discovery
# --------------------------------------------------------------------------
@dataclass
class TileInfo:
    port: str
    uid: str
    board_id: str


def load_config(path: Path) -> dict:
    cfg = dict(CONFIG_DEFAULTS)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as exc:
        print(f"Config {path}: {exc}", file=sys.stderr)
        return cfg
    for key, default in CONFIG_DEFAULTS.items():
        if key in data:
            cfg[key] = data[key]
    return cfg


def serial_ports():
    return sorted(list_ports.comports(), key=lambda p: p.device)


def is_circuitpython_data_port(port) -> bool:
    """CircuitPython's binary channel is the CDC interface named 'CDC2'.

    Match the interface name, not the VID: a Feather running CircuitPython
    enumerates as Adafruit (0x239a), while BOOTSEL and bare Pico boards use
    0x2e8a. Other USB serial devices never have this interface name.
    """
    iface = port.interface or ""
    return "CircuitPython" in iface and "CDC2" in iface


def tile_candidates(cfg: dict):
    expected = (cfg.get("serial") or "").upper()
    candidates = [p for p in serial_ports() if is_circuitpython_data_port(p)]
    if expected:
        candidates = [p for p in candidates if (p.serial_number or "").upper() == expected]
    return candidates


def probe(device: str, cfg: dict, verbose: bool = False):
    """Open a data port and require both the handshake and the identity reply."""
    expected_serial = (cfg.get("serial") or "").upper()
    expected_board = cfg.get("board_id") or ""

    try:
        ser = serial.Serial(device, BAUD, timeout=0.5, write_timeout=1.0)
    except (serial.SerialException, OSError):
        return None, None

    try:
        time.sleep(0.05)
        ser.reset_input_buffer()
        ser.write(HANDSHAKE)
        ser.flush()
        reply = ser.read_until(b"\n", 96).strip()
        if not reply.startswith(HANDSHAKE_REPLY):
            ser.close()
            return None, None

        parts = reply.decode("ascii", "replace").split()
        uid = parts[1].upper() if len(parts) > 1 else ""
        board = parts[2] if len(parts) > 2 else ""

        if expected_serial and uid != expected_serial:
            if verbose:
                detail = "no identity (outdated firmware?)" if not uid else f"serial {uid}"
                print(f"  {device}: ignored, {detail} != {expected_serial}")
            ser.close()
            return None, None
        if expected_board and board != expected_board:
            if verbose:
                print(f"  {device}: ignored, board {board or 'unknown'} != {expected_board}")
            ser.close()
            return None, None
        return ser, TileInfo(device, uid, board)
    except (serial.SerialException, OSError):
        try:
            ser.close()
        except Exception:
            pass
        return None, None


def find_tile(cfg: dict, explicit_port: str | None = None, verbose: bool = False):
    """Return (serial, TileInfo, reason). reason is set when nothing was found."""
    if explicit_port:
        ser, tile = probe(explicit_port, cfg, verbose)
        if ser:
            return ser, tile, None
        return None, None, f"{explicit_port}: no vitals tile answered/identity matched"

    candidates = tile_candidates(cfg)
    for port in candidates:
        ser, tile = probe(port.device, cfg, verbose)
        if ser:
            return ser, tile, None
    return None, None, None


def describe_missing(cfg: dict) -> str:
    ports = serial_ports()
    if any(p.vid == RASPBERRY_PI_VID and p.product and "RP2 Boot" in p.product for p in ports):
        return "Tile is in BOOTSEL mode - reflash CircuitPython and code.py"

    circuitpython = [p for p in ports if is_circuitpython_data_port(p)]
    expected = cfg.get("serial")
    if expected:
        matching = [p for p in circuitpython if (p.serial_number or "").upper() == expected]
        if matching:
            return (
                f"Tile {expected} is connected but not answering - is code.py running? "
                "(Ctrl-D in its REPL, or re-save code.py)"
            )
        if circuitpython:
            return (
                f"Found {len(circuitpython)} other CircuitPython board(s), none with serial "
                f"{expected}. Waiting for the tile..."
            )
        return f"Waiting for the vitals tile (USB serial {expected})..."

    if circuitpython:
        return "Found CircuitPython board(s), but none answered the handshake"
    return "Waiting for a vitals tile (CircuitPython data interface)..."

def print_ports(cfg: dict) -> None:
    expected = (cfg.get("serial") or "").upper()
    print(f"{'':1} {'DEVICE':<22} {'VID:PID':<10} {'SERIAL':<18} INTERFACE")
    for port in serial_ports():
        is_tile = is_circuitpython_data_port(port)
        matches = is_tile and (not expected or (port.serial_number or "").upper() == expected)
        mark = "*" if matches else " "
        print(
            f"{mark} {port.device:<22} {port.vid or 0:04x}:{port.pid or 0:04x} "
            f"{(port.serial_number or '-'):<18} {port.interface or port.product or ''}"
        )
    print("\n* = candidate for the tile (CDC2 data interface" + (", serial matches" if expected else "") + ")")


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------
def stream(ser: serial.Serial, sampler, renderer: Renderer, fps: float, verbose: bool) -> None:
    period = 1.0 / fps
    next_tick = time.monotonic()
    frames = 0
    while True:
        reading = sampler.sample()
        ser.write(MAGIC + to_ssd1306(renderer.render(reading)))
        ser.flush()
        frames += 1
        if verbose and frames % max(1, int(fps * 5)) == 0:
            print(
                f"  CPU {reading.cpu:5.1f}%  GPU {reading.gpu:5.1f}%  "
                f"RAM {reading.mem:4.1f}%  {reading.cpu_temp or 0:.0f}C  "
                f"{reading.watts or 0:.0f}W  {reading.fan_rpm or 0}rpm"
            )
        next_tick += period
        time.sleep(max(0.0, next_tick - time.monotonic()))


def acquire_lock(cfg: dict):
    """One streamer per tile, so a manual run cannot fight the service."""
    name = f"vitals-tile-{(cfg.get('serial') or 'any').lower()}.lock"
    handle = open(Path(tempfile.gettempdir()) / name, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def live(args, cfg: dict, renderer: Renderer) -> None:
    """Stream forever; wait for the tile and reconnect when it disappears."""
    lock = acquire_lock(cfg)
    if lock is None:
        raise SystemExit(
            "Another vitals-tile host is already streaming to this tile "
            "(systemd service?). Stop it with: systemctl --user stop vitals-tile"
        )
    missing = None
    while True:
        ser, tile, reason = find_tile(cfg, args.port, args.verbose)
        if ser is None:
            reason = reason or describe_missing(cfg)
            if reason != missing:
                print(reason)
                missing = reason
            time.sleep(2.0)
            continue

        missing = None
        print(f"Tile {tile.uid} ({tile.board_id}) on {tile.port}")
        sampler = SimulatedSampler() if args.simulate else VitalsSampler()
        if isinstance(sampler, VitalsSampler) and args.verbose:
            print(f"Sensors: {sampler.source_summary}")
        try:
            stream(ser, sampler, renderer, args.fps, args.verbose)
        except (serial.SerialException, OSError) as exc:
            print(f"Tile disconnected ({exc}) - waiting for it to come back")
        finally:
            try:
                ser.close()
            except Exception:
                pass
        time.sleep(1.0)


# --------------------------------------------------------------------------
# Offline previews
# --------------------------------------------------------------------------
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
    parser.add_argument("--port", help="explicit serial device (still identity-checked)")
    parser.add_argument("--serial", help="expected USB serial / chip UID (overrides tile.json)")
    parser.add_argument("--board", help="expected board id (overrides tile.json)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="tile.json path")
    parser.add_argument("--fps", type=float, help="frames per second (default 2)")
    parser.add_argument("--simulate", action="store_true", help="send a fake AI workload")
    parser.add_argument("--png", help="save one frame of real data as PNG and exit")
    parser.add_argument("--gif", help="save a scripted demo animation and exit")
    parser.add_argument("--seconds", type=float, default=10.0, help="GIF loop length (default 10)")
    parser.add_argument("--scale", type=int, default=4, help="preview scale factor (default 4)")
    parser.add_argument("--list", action="store_true", help="list serial ports and exit")
    parser.add_argument("--id", action="store_true", help="print the detected tile identity and exit")
    parser.add_argument("--quiet", dest="verbose", action="store_false")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    if args.serial:
        cfg["serial"] = args.serial
    if args.board:
        cfg["board_id"] = args.board
    if cfg["serial"]:
        cfg["serial"] = cfg["serial"].upper()
    args.fps = args.fps or float(cfg["fps"])

    if args.list:
        print_ports(cfg)
        return

    if args.id:
        ser, tile, reason = find_tile(cfg, args.port, verbose=True)
        if ser is None:
            print(reason or describe_missing(cfg))
            raise SystemExit(1)
        ser.close()
        print(f"{tile.uid} {tile.board_id} {tile.port}")
        return

    renderer = Renderer()

    if args.gif:
        write_gif(args, renderer)
        return

    if args.png:
        sampler = SimulatedSampler() if args.simulate else VitalsSampler()
        write_png(args, sampler, renderer)
        return

    try:
        live(args, cfg, renderer)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

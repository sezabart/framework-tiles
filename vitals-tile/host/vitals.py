"""Read PC vitals from Linux /proc and /sys.

Tuned for the Framework Desktop (AMD Ryzen AI Max + Radeon 8060S), but every
source is optional: anything that is missing simply reads as None so the OLED
can still show partial data.
"""

from __future__ import annotations

import glob
import os
import time
from dataclasses import dataclass, field


def _read(path: str, cast=float):
    try:
        with open(path) as fh:
            return cast(fh.read().strip())
    except (OSError, ValueError):
        return None


def _hwmon(name: str) -> str | None:
    for path in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(os.path.join(path, "name"), str) == name:
            return path
    return None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


@dataclass
class Reading:
    cpu: float = 0.0          # percent, busiest single core?  no: whole package
    gpu: float = 0.0          # percent
    mem: float = 0.0          # percent used
    mem_used_gb: float = 0.0
    mem_total_gb: float = 0.0
    cpu_temp: float | None = None
    gpu_temp: float | None = None
    watts: float | None = None
    fan_rpm: int | None = None
    phase: float = field(default=0.0, repr=False)  # animation clock

    @property
    def busiest(self) -> float:
        return max(self.cpu, self.gpu)


class VitalsSampler:
    """Stateful sampler: CPU load needs two /proc/stat reads to compute a delta."""

    def __init__(self) -> None:
        self._last_cpu = self._cpu_times()
        self._last_time = time.monotonic()
        self._cpu_smooth: float | None = None
        self._gpu_smooth: float | None = None
        self._phase = 0.0
        self._mem_total_kb = _read("/proc/meminfo", str)
        self._sources = self._discover()

    # -- discovery ---------------------------------------------------------
    def _discover(self) -> dict[str, str | None]:
        gpu_busy = None
        for path in sorted(glob.glob("/sys/class/drm/card[0-9]*/device/gpu_busy_percent")):
            gpu_busy = path
            break

        cpu_hw = _hwmon("k10temp") or _hwmon("coretemp")
        gpu_hw = _hwmon("amdgpu")
        ec_hw = _hwmon("cros_ec")

        sources = {
            "gpu_busy": gpu_busy,
            "cpu_temp": os.path.join(cpu_hw, "temp1_input") if cpu_hw else None,
            "gpu_temp": os.path.join(gpu_hw, "temp1_input") if gpu_hw else None,
            "gpu_power": os.path.join(gpu_hw, "power1_average") if gpu_hw else None,
            "gpu_power_input": os.path.join(gpu_hw, "power1_input") if gpu_hw else None,
            "fan": os.path.join(ec_hw, "fan1_input") if ec_hw else None,
            "fan2": os.path.join(ec_hw, "fan2_input") if ec_hw else None,
        }
        # Only advertise what actually exists, so a typo shows up as "missing"
        # instead of silently rendering 0%.
        return {name: (path if path and os.path.exists(path) else None) for name, path in sources.items()}

    @property
    def source_summary(self) -> str:
        found = [name for name, path in self._sources.items() if path]
        return ", ".join(found) if found else "none"

    # -- sampling ----------------------------------------------------------
    @staticmethod
    def _cpu_times():
        try:
            with open("/proc/stat") as fh:
                parts = fh.readline().split()
            values = [int(v) for v in parts[1:]]
        except (OSError, ValueError):
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    @staticmethod
    def _meminfo() -> tuple[float, float]:
        total = available = None
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        total = float(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        available = float(line.split()[1])
                    if total is not None and available is not None:
                        break
        except OSError:
            return 0.0, 0.0
        if total is None or available is None:
            return 0.0, 0.0
        return total / 1024 / 1024, (total - available) / 1024 / 1024

    @staticmethod
    def _ema(previous: float | None, sample: float, alpha: float) -> float:
        if previous is None:
            return sample
        return previous + alpha * (sample - previous)

    def sample(self, alpha: float = 0.45) -> Reading:
        now = time.monotonic()
        dt = max(0.001, now - self._last_time)

        cpu_now = self._cpu_times()
        # /proc/stat is jiffy-quantised: a delta over a few ms is noise, so keep
        # the previous value until the interval is long enough to mean something.
        cpu_pct = self._cpu_smooth if self._cpu_smooth is not None else 0.0
        if cpu_now and self._last_cpu and dt >= 0.15:
            total_delta = cpu_now[0] - self._last_cpu[0]
            idle_delta = cpu_now[1] - self._last_cpu[1]
            if total_delta > 0:
                cpu_pct = _clamp(100.0 * (total_delta - idle_delta) / total_delta)
        self._last_cpu, self._last_time = cpu_now, now

        gpu_pct = _read(self._sources["gpu_busy"] or "", float) or 0.0
        gpu_pct = _clamp(gpu_pct)

        self._cpu_smooth = self._ema(self._cpu_smooth, cpu_pct, alpha)
        self._gpu_smooth = self._ema(self._gpu_smooth, gpu_pct, alpha)

        total_gb, used_gb = self._meminfo()

        cpu_temp = _read(self._sources["cpu_temp"] or "")
        gpu_temp = _read(self._sources["gpu_temp"] or "")
        if cpu_temp is not None:
            cpu_temp /= 1000.0
        if gpu_temp is not None:
            gpu_temp /= 1000.0

        watts = _read(self._sources["gpu_power"] or "")
        if watts is None:
            watts = _read(self._sources["gpu_power_input"] or "")
        if watts is not None:
            watts /= 1_000_000.0

        fan = _read(self._sources["fan"] or "", int)
        fan2 = _read(self._sources["fan2"] or "", int)
        fans = [f for f in (fan, fan2) if f is not None]
        fan_rpm = max(fans) if fans else None

        self._phase += dt

        return Reading(
            cpu=round(self._cpu_smooth, 1),
            gpu=round(self._gpu_smooth, 1),
            mem=_clamp(100.0 * used_gb / total_gb) if total_gb else 0.0,
            mem_used_gb=used_gb,
            mem_total_gb=total_gb,
            cpu_temp=cpu_temp,
            gpu_temp=gpu_temp,
            watts=watts,
            fan_rpm=fan_rpm,
            phase=self._phase,
        )

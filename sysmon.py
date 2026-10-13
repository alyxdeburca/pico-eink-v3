#!/usr/bin/env python3
"""sysmon - push CPU / RAM / GPU stats to the e-ink panel every N seconds.

    python3 sysmon.py [--interval 10] [--once] [--port SERIAL]
                      [--x 20] [--y 20]

Renders a compact dashboard with the Pico's 5x8 font (uppercased) and
ASCII bar gauges ('#' filled, '-' empty). GPU usage comes from
nvidia-smi when available, otherwise from the drm fdinfo counters in
/proc (works for Intel/AMD without root; first cycle shows "--" while
it collects a delta). CPU temperature is shown when a sensor is found.

Note: this owns the serial port while running — stop the web UI or
einkctl first, and vice versa.
"""

import argparse
import glob
import subprocess
import sys
import time

import psutil

from einkctl import Eink, find_port, text_payload
BAR_CELLS = 12
MAX_WIDTH_FRAC = 0.9  # keep content off the damaged edge (right side @ rot 270)
TEMP_KEYS = ("coretemp", "k10temp", "zenpower", "cpu_thermal",
             "acpitz", "x86_pkg_temp", "soc_thermal")


def bar(pct):
    pct = 0 if pct < 0 else (100 if pct > 100 else pct)
    n = int(round(pct / 100 * BAR_CELLS))
    return "#" * n + "-" * (BAR_CELLS - n)


def gpu_nvidia():
    """nvidia-smi when present; None otherwise."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        v = [x.strip() for x in out.stdout.splitlines()[0].split(",")]
        return {"pct": float(v[0]), "mem": "%.1f/%.0fG" % (float(v[1]) / 1024,
                                                           float(v[2]) / 1024)}
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


class Gpu:
    """GPU sampler: nvidia-smi, else drm fdinfo deltas (Intel/AMD)."""

    def __init__(self):
        self.prev_ns = None
        self.prev_t = None

    def sample(self):
        nvidia = gpu_nvidia()
        if nvidia is not None:
            return nvidia

        totals, found = 0, False
        for path in glob.glob("/proc/[0-9]*/fdinfo/*"):
            try:
                with open(path) as f:
                    for line in f:
                        p = line.split()
                        if len(p) == 3 and p[0].startswith("drm-engine-") \
                                and p[2] == "ns":
                            totals += int(p[1])
                            found = True
            except (OSError, ValueError):
                continue
        if not found:
            self.prev_ns = None
            return None
        now = time.monotonic()
        pct = None
        if self.prev_ns is not None and now > self.prev_t:
            dt = now - self.prev_t
            pct = max(0.0, min(100.0, 100.0 * (totals - self.prev_ns)
                               / (dt * 1e9)))
        self.prev_ns, self.prev_t = totals, now
        if pct is None:
            return None
        out = {"pct": pct}
        for key in ("i915", "amdgpu"):  # GPU temp, when exposed
            try:
                t = psutil.sensors_temperatures().get(key)
            except (AttributeError, OSError):
                t = None
            if t and t[0].current:
                out["temp"] = round(t[0].current)
                break
        return out


def cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    for key in TEMP_KEYS:
        if key in temps and temps[key]:
            val = temps[key][0].current
            if val:
                return round(val)
    return None


def sample(state):
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "ram_pct": psutil.virtual_memory().percent,
        "ram_str": "%.1f/%.0fG" % (psutil.virtual_memory().used / 2**30,
                                   psutil.virtual_memory().total / 2**30),
        "gpu": state.sample(),
        "temp": cpu_temp(),
    }


def build_text(s, lw, x):
    """Dashboard text, clamped to 90% of the logical width."""
    maxc2 = int((lw * MAX_WIDTH_FRAC - x) // (6 * 2))  # stat lines (size 2)
    maxc3 = int((lw * MAX_WIDTH_FRAC - x) // (6 * 3))  # title (size 3)

    def fit(line, n):
        return line[:n]

    lines = ["[SIZE:3]" + fit("SYSTEM MONITOR", maxc3) + "[SIZE:3]", "[SIZE:2]"]
    lines.append(fit("CPU %d%% %s" % (round(s["cpu"]), bar(s["cpu"])), maxc2))
    lines.append(fit("RAM %d%% %s" % (round(s["ram_pct"]), bar(s["ram_pct"])),
                     maxc2))
    if s["gpu"] is None:
        lines.append(fit("GPU --", maxc2))
    elif s["gpu"].get("pct") is None:
        lines.append(fit("GPU -- %s" % s["gpu"].get("mem", ""), maxc2))
    else:
        line = "GPU %d%% %s" % (round(s["gpu"]["pct"]), bar(s["gpu"]["pct"]))
        if s["gpu"].get("mem"):
            line += " " + s["gpu"]["mem"]
        if s["gpu"].get("temp"):
            line += " %dC" % s["gpu"]["temp"]
        lines.append(fit(line, maxc2))
    extra = []
    if s["temp"] is not None:
        extra.append("TMP %dC" % s["temp"])
    extra.append("RAM " + s["ram_str"])
    lines.append(fit("  ".join(extra), maxc2))
    # current PC time, two lines below the last entry
    lines.append("")
    lines.append(fit("TIME " + time.strftime("%H:%M:%S"), maxc2))
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--interval", type=float, default=10,
                   help="seconds between updates (default 10)")
    p.add_argument("--once", action="store_true", help="send one update, exit")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    p.add_argument("--x", type=int, default=20)
    p.add_argument("--y", type=int, default=20)
    a = p.parse_args()

    psutil.cpu_percent(interval=None)  # prime the per-call delta
    gpu = Gpu()
    dev = None
    need_clear = True  # full screen clear after the first connection
    while True:
        t0 = time.monotonic()
        try:
            if dev is None:
                dev = Eink(find_port(a.port))
                dev.wake()
                if need_clear:
                    reply = dev.cmd("CLEAR", timeout=30)
                    print(time.strftime("%H:%M:%S"), reply)
                    need_clear = False
            text = build_text(sample(gpu), dev.lw, a.x)
            reply = dev.cmd(text_payload(text, None, a.x, a.y), timeout=60)
            print(time.strftime("%H:%M:%S"), reply)
            if not reply.startswith("OK"):
                print("device said: " + reply, file=sys.stderr)
        except KeyboardInterrupt:
            raise
        except (Exception, SystemExit) as ex:  # link hiccup: retry next cycle
            print("device error: %s" % ex, file=sys.stderr)
            try:
                if dev is not None:
                    dev.s.close()
            except Exception:
                pass
            dev = None
        if a.once:
            break
        delay = a.interval - (time.monotonic() - t0)
        if delay > 0:
            time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")

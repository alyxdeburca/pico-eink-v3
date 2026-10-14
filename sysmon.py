#!/usr/bin/env python3
"""sysmon - push CPU / RAM / GPU stats to the e-ink panel every N seconds.

    python3 sysmon.py [--interval 10] [--once] [--port SERIAL]
                      [--x 20] [--y 20]

Renders a compact dashboard with the Pico's 5x8 font (uppercased) and
ASCII bar gauges ('#' filled, '-' empty): CPU / RAM / GPU usage plus a
clock, local weather (Open-Meteo, no API key; located by IP unless
--lat/--lon are given), sunrise/sunset, moon phase, uptime and disk
usage. GPU usage comes from nvidia-smi when available, otherwise from
the drm fdinfo counters in /proc (works for Intel/AMD without root;
first cycle shows "--" while it collects a delta). CPU temperature is
shown when a sensor is found.

Note: this owns the serial port while running — stop the web UI or
einkctl first, and vice versa.
"""

import argparse
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import psutil

from einkctl import Eink, find_port, text_payload
BAR_CELLS = 12
MAX_WIDTH_FRAC = 0.9  # keep content off the damaged edge (right side @ rot 270)
TEMP_KEYS = ("coretemp", "k10temp", "zenpower", "cpu_thermal",
             "acpitz", "x86_pkg_temp", "soc_thermal")
CACHE_DIR = os.path.expanduser("~/.cache/pico-eink")
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
MONS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
        "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
MOON_NAMES = ("NEW", "WAX CRES", "FIRST QTR", "WAX GIB",
              "FULL", "WAN GIB", "LAST QTR", "WAN CRES")
WMO = {0: "CLEAR", 1: "FAIR", 2: "P.CLOUDY", 3: "CLOUDY", 45: "FOG",
       48: "FOG", 51: "DRIZZLE", 53: "DRIZZLE", 55: "DRIZZLE",
       61: "RAIN", 63: "RAIN", 65: "RAIN", 66: "SLEET", 67: "SLEET",
       71: "SNOW", 73: "SNOW", 75: "SNOW", 77: "SNOW", 80: "SHOWERS",
       81: "SHOWERS", 82: "SHOWERS", 85: "SNOW", 86: "SNOW",
       95: "STORM", 96: "STORM", 99: "STORM"}


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
            # no GPU clients right now: keep the last counters so the next
            # delta stays valid, unless the gap has gone stale, then re-prime
            now = time.monotonic()
            if self.prev_ns is not None and now - self.prev_t > 120:
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


def moon_phase():
    """(name, illumination %) from the lunar age. Works offline."""
    synodic = 29.53058867 * 86400
    ref = 947182440  # new moon 2000-01-06 18:14 UTC
    frac = ((time.time() - ref) % synodic) / synodic
    illum = round((1 - math.cos(2 * math.pi * frac)) / 2 * 100)
    return MOON_NAMES[int(frac * 8 + 0.5) % 8], illum


def uptime_str():
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return "--"
    if secs >= 86400:
        return "%dD %dH" % (secs // 86400, secs % 86400 // 3600)
    return "%dH %dM" % (secs // 3600, secs % 3600 // 60)


class Weather:
    """Ambient conditions from Open-Meteo (no API key), located by IP.

    Results are cached on disk so restarts are instant; failures back
    off (retry in 60 s) and never raise. Provide --lat/--lon to skip
    IP geolocation.
    """

    def __init__(self, lat=None, lon=None):
        self.lat, self.lon = lat, lon
        self.city = None
        self.data = None
        self.t = 0.0
        self.max_age = 1800  # refetch every 30 min

    def _load(self, name):
        try:
            with open(os.path.join(CACHE_DIR, name)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save(self, name, obj):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(os.path.join(CACHE_DIR, name), "w") as f:
                json.dump(obj, f)
        except OSError:
            pass

    def locate(self):
        c = self._load("location.json")
        if c and time.time() - c["t"] < 7 * 86400:
            self.lat, self.lon = c["lat"], c["lon"]
            self.city = c.get("city")
            return True
        sources = (
            ("https://ipapi.co/json/",
             lambda j: (j.get("latitude"), j.get("longitude"), j.get("city"))),
            ("http://ip-api.com/json/?fields=lat,lon,city",
             lambda j: (j.get("lat"), j.get("lon"), j.get("city"))))
        for url, pick in sources:
            try:
                with urllib.request.urlopen(url, timeout=8) as r:
                    j = json.loads(r.read().decode())
                lat, lon, city = pick(j)
                if lat is None or lon is None:
                    continue
                self.lat, self.lon = float(lat), float(lon)
                self.city = city
                self._save("location.json", {"lat": self.lat, "lon": self.lon,
                                             "city": self.city,
                                             "t": time.time()})
                return True
            except Exception:
                continue
        return False

    def fetch(self):
        url = ("https://api.open-meteo.com/v1/forecast"
               "?latitude=%f&longitude=%f"
               "&current=temperature_2m,apparent_temperature,weather_code"
               "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
               "&forecast_days=1&timezone=auto" % (self.lat, self.lon))
        with urllib.request.urlopen(url, timeout=8) as r:
            j = json.loads(r.read().decode())
        cur, d = j["current"], j["daily"]
        return {"temp": round(cur["temperature_2m"]),
                "feels": round(cur["apparent_temperature"]),
                "code": cur.get("weather_code"),
                "hi": round(d["temperature_2m_max"][0]),
                "lo": round(d["temperature_2m_min"][0]),
                "sunrise": d["sunrise"][0][-5:],
                "sunset": d["sunset"][0][-5:]}

    def get(self):
        """Current weather dict (with a 't' timestamp), or None."""
        now = time.monotonic()
        if self.data is not None and now - self.t < self.max_age:
            return self.data
        c = self._load("weather.json")
        if self.data is None and c and time.time() - c["t"] < self.max_age:
            self.data, self.t = c, now
            return self.data
        try:
            if self.lat is None and not self.locate():
                raise RuntimeError("location unavailable")
            self.data = self.fetch()
            self.data["city"] = self.city
            self.data["t"] = time.time()
            self._save("weather.json", self.data)
            self.t = now
        except Exception as ex:
            print("weather: %s" % ex, file=sys.stderr)
            if self.data is None:
                self.t = now - self.max_age + 60  # retry in 60 s
        return self.data


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
    du = shutil.disk_usage("/")
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "ram_pct": psutil.virtual_memory().percent,
        "ram_str": "%.1f/%.0fG" % (psutil.virtual_memory().used / 2**30,
                                   psutil.virtual_memory().total / 2**30),
        "gpu": state.sample(),
        "temp": cpu_temp(),
        "up": uptime_str(),
        "disk_pct": round(du.used / du.total * 100),
        "disk_g": round(du.used / 2**30),
        "disk_total": round(du.total / 2**30),
    }


def build_text(s, lw, x):
    """Dashboard text, clamped to 90% of the logical width."""
    maxc2 = int((lw * MAX_WIDTH_FRAC - x) // (6 * 2))  # stat lines (size 2)

    def fit(line, n):
        return line[:n]

    lines = ["[SIZE:2]"
             + fit("CPU %d%% %s" % (round(s["cpu"]), bar(s["cpu"])), maxc2)]
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

    lt = time.localtime()
    lines.append(fit("%s %02d %s %02d:%02d"
                     % (DAYS[lt[6]], lt[2], MONS[lt[1] - 1], lt[3], lt[4]),
                     maxc2))
    wx = s.get("wx")
    if wx:
        lines.append(fit("WEATHER %s %dC H%d L%d"
                         % (WMO.get(wx.get("code"), "--"), wx["temp"],
                            wx["hi"], wx["lo"]), maxc2))
        parts = [p for p in (wx.get("city"), "FEELS %dC" % wx["feels"]) if p]
        lines.append(fit(" ".join(parts), maxc2))
        lines.append(fit("SUN %s-%s MOON %d%%"
                         % (wx.get("sunrise", "--"), wx.get("sunset", "--"),
                            moon_phase()[1]), maxc2))
    else:
        name, illum = moon_phase()
        lines.append(fit("WEATHER --", maxc2))
        lines.append(fit("OFFLINE MOON %s %d%%" % (name, illum), maxc2))
        lines.append(fit("SUN --", maxc2))
    lines.append(fit("UP %s DISK %d%% %d/%dG"
                     % (s["up"], s["disk_pct"], s["disk_g"],
                        s["disk_total"]), maxc2))
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--interval", type=float, default=10,
                   help="seconds between updates (default 10)")
    p.add_argument("--refresh-every", type=float, default=30,
                   help="full refresh (ghost wipe) every N seconds "
                        "(default 30, 0=off)")
    p.add_argument("--once", action="store_true", help="send one update, exit")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    p.add_argument("--lat", type=float,
                   help="latitude for weather (default: IP geolocation)")
    p.add_argument("--lon", type=float, help="longitude for weather")
    p.add_argument("--x", type=int, default=20)
    p.add_argument("--y", type=int, default=20)
    a = p.parse_args()

    psutil.cpu_percent(interval=None)  # prime the per-call delta
    gpu = Gpu()
    weather = Weather(a.lat, a.lon)
    dev = None
    need_clear = True   # full screen clear after the first connection
    last_render = 0.0
    last_clear = 0.0
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
                last_clear = time.monotonic()
            elif a.refresh_every > 0 and t0 - last_clear >= a.refresh_every:
                # periodic ghost wipe: full refresh of the current dashboard
                # (one ~1 s flash, content stays visible — no white-out)
                reply = dev.cmd("FULL", timeout=30)
                print(time.strftime("%H:%M:%S"), reply)
                last_clear = time.monotonic()
                if not reply.startswith("OK"):
                    need_clear = True  # FULL failed: clear next cycle
            s = sample(gpu)
            s["wx"] = weather.get()
            text = build_text(s, dev.lw, a.x)
            if a.once:
                print(text)  # show what was rendered (debug aid)
            reply = dev.cmd(text_payload(text, None, a.x, a.y), timeout=60)
            print(time.strftime("%H:%M:%S"), reply)
            if not reply.startswith("OK"):
                print("device said: " + reply, file=sys.stderr)
            last_render = time.monotonic()
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
        # sleep until whichever comes first: next render or next full refresh
        now = time.monotonic()
        if dev is None:
            delay = 5  # device gone / link down: retry shortly
        else:
            target = last_render + a.interval
            if a.refresh_every > 0:
                target = min(target, last_clear + a.refresh_every)
            delay = target - now
        if delay > 0:
            time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")

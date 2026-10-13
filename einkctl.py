#!/usr/bin/env python3
"""einkctl - control a Pico W e-ink display from your PC over USB serial.

Pairs with pico/server.py running on the Pico. Examples:

    ./einkctl.py status
    ./einkctl.py text "HELLO WORLD" --size 3
    ./einkctl.py text "Desk total energy\\n0.462 kWh" --size 2
    ./einkctl.py file display.txt
    ./einkctl.py image photo.jpg            # auto-scaled + dithered
    ./einkctl.py image pic.png --invert --no-dither --rotate 90
    ./einkctl.py clear
    ./einkctl.py rotate 270                 # rotate content (persisted)
    ./einkctl.py save                       # show this after next reboot
    ./einkctl.py demo                       # test pattern
    ./einkctl.py sleep 3600                 # deep sleep, display keeps image
    ./einkctl.py reset
    ./einkctl.py repl                       # release port to mpremote/REPL

Commands run one at a time; each is answered by the Pico with OK/ERR.
"""

import argparse
import binascii
import glob
import re
import sys
import time

import serial
from serial.tools import list_ports

PANEL_W = 240
PANEL_H = 416
FRAME_BYTES = PANEL_W * PANEL_H // 8  # 12480
DEFAULT_X = 20
DEFAULT_Y = 50


def find_port(user_port):
    if user_port:
        return user_port
    for p in list_ports.comports():
        desc = " ".join(str(x) for x in (p.description, p.product, p.manufacturer) if x)
        if "micropython" in desc.lower() or "pico" in desc.lower():
            return p.device
    for pat in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    raise SystemExit("error: no serial port found (is the Pico plugged in?)")


class Eink:
    def __init__(self, port, verbose=False):
        self.verbose = verbose
        self.rot, self.lw, self.lh = 0, PANEL_W, PANEL_H
        self.open_port(port)

    def open_port(self, port):
        self.port = port
        self.s = serial.Serial(port, 115200, timeout=0.5, write_timeout=10)

    def reopen(self, port=None):
        self.s.close()
        time.sleep(0.2)
        self.open_port(port or self.port)

    def log(self, msg):
        if self.verbose:
            print("[einkctl] " + msg, file=sys.stderr)

    def drain(self, quiet=0.3):
        """Read and discard bytes until the line has been quiet a while."""
        end = time.time() + quiet
        while time.time() < end:
            n = self.s.in_waiting
            if n:
                data = self.s.read(n)
                self.log("rx: %r" % data)
                end = time.time() + quiet
            else:
                time.sleep(0.05)

    def send_line(self, line):
        self.log("tx: %s%s" % (line[:120], "..." if len(line) > 120 else ""))
        self.s.write(line.encode() + b"\n")
        self.s.flush()

    def read_reply(self, deadline):
        while time.time() < deadline:
            raw = self.s.readline()
            if raw:
                line = raw.decode(errors="replace").strip()
                if line.startswith(("OK", "ERR")):
                    return line
                if line:
                    self.log("noise: " + line)
        return "ERR timeout waiting for reply"

    def cmd(self, line, timeout=30):
        self.send_line(line)
        return self.read_reply(time.time() + timeout)

    def ping(self, timeout=6):
        """PING with reply validation: any OK line other than OK PONG is a
        stale reply from an earlier session — swallow it and retry."""
        for _ in range(4):
            reply = self.cmd("PING", timeout=timeout)
            if reply == "OK PONG":
                return True
            self.log("unexpected reply to PING (%s); draining and retrying"
                     % reply)
            self.drain(0.4)
        return False

    def wake(self):
        """Make sure the Pico-side server is up; recover into it if not."""
        self.drain()
        try:
            # long enough to cover a fresh boot's content render (~4s)
            if self.ping(timeout=6):
                self.refresh_info()
                return
        except serial.SerialException as ex:
            self.log("port error on first ping (%s); reopening" % ex)
            time.sleep(1.5)
            self.reopen()
        for attempt in range(4):
            if attempt:
                # a fresh open clears kernel-side link state after glitches
                try:
                    self.reopen()
                except serial.SerialException:
                    pass
                time.sleep(0.5)
            self.log("recovery attempt %d" % (attempt + 1))
            try:
                banner = self.recover()
            except serial.SerialException as ex:
                self.log("port error (%s); reopening" % ex)
                time.sleep(1.5)
                try:
                    self.reopen()
                except serial.SerialException:
                    pass
                banner = False
            if banner:
                try:
                    ok = self.ping(timeout=5)
                except serial.SerialException:
                    ok = False
                if ok:
                    self.log("server is up")
                    self.refresh_info()
                    return
        raise SystemExit(
            "error: could not start the e-ink server on %s. Is pico/server.py "
            "installed? (see README)" % self.port
        )

    def recover(self):
        """Interrupt whatever runs, soft-reboot, wait for the server banner.

        Returns True once "EINK SERVER ... READY" was seen; False if the
        board fell back to the REPL (server crashed) or the port glitched.
        """
        self.s.write(b"\x03\x03")  # Ctrl-C: interrupt whatever is running
        time.sleep(0.3)
        self.s.write(b"\x04")  # Ctrl-D: soft reboot -> main.py -> server
        deadline = time.time() + 12  # boot render can take ~5s
        while time.time() < deadline:
            try:
                raw = self.s.readline()
            except serial.SerialException:
                return False
            if raw:
                line = raw.decode(errors="replace").strip()
                self.log("rx: %s" % line)
                if "EINK SERVER" in line:
                    return True
                if line.endswith(">>>"):
                    return False
        return False

    def refresh_info(self):
        """Learn the device's rotation and logical content size (STAT)."""
        for _ in range(3):
            reply = self.cmd("STAT", timeout=10)
            m = re.search(r"ROT=(\d+) SIZE=(\d+)x(\d+)", reply)
            if m:
                self.rot = int(m.group(1))
                self.lw, self.lh = int(m.group(2)), int(m.group(3))
                self.log("device: rot=%d logical=%dx%d"
                         % (self.rot, self.lw, self.lh))
                return
            self.log("unparseable STAT reply (%s); draining and retrying"
                     % reply)
            self.drain(0.4)


# ---------------------------------------------------------------- payloads

def hexify(data):
    return binascii.hexlify(data).decode()


def text_payload(text, size=None, x=DEFAULT_X, y=DEFAULT_Y):
    if size:
        text = "[SIZE:%d]%s" % (size, text)
    text = text.upper()
    return "TEXT %d %d %s" % (x, y, hexify(text.encode()))


def pil_to_frame(img, lw, lh, dither=True, scale="fit"):
    """Convert a PIL image to a packed 1-bit LOGICAL frame (lw x lh).

    The Pico applies the rotation; this just packs content in logical
    coordinates: 1 bit/pixel, MSB first, row-major, 0 bit = black.
    """
    from PIL import Image

    if img.mode == "RGBA" or img.mode == "LA" or (
            img.mode == "P" and "transparency" in img.info):
        # composite transparency over white instead of dropping alpha
        # (dropping it paints transparent regions with their raw RGB,
        # which is black for most PNGs)
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    elif img.mode == "CMYK":
        img = img.convert("RGB")  # avoid channel-inverted grayscale
    elif img.mode in ("I;16", "I", "F"):
        lo, hi = img.getextrema()
        if hi > 255:  # scale down to 0-255 instead of clipping to white
            img = img.point(lambda v, hi=hi: v * 255.0 / hi)
    img = img.convert("L")
    w, h = img.size
    if scale == "stretch":
        img = img.resize((lw, lh))
        canvas = img
    elif scale == "none":
        if w > lw or h > lh:
            img = img.crop((0, 0, min(w, lw), min(h, lh)))
        canvas = Image.new("L", (lw, lh), 255)
        canvas.paste(img, ((lw - img.size[0]) // 2, (lh - img.size[1]) // 2))
    else:  # fit: preserve aspect ratio, pad with white
        s = min(lw / w, lh / h)
        nw, nh = max(1, round(w * s)), max(1, round(h * s))
        if (nw, nh) != (w, h):
            img = img.resize((nw, nh))
        canvas = Image.new("L", (lw, lh), 255)
        canvas.paste(img, ((lw - nw) // 2, (lh - nh) // 2))
    try:
        if dither:
            mono = canvas.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
        else:
            mono = canvas.point(lambda p: 255 if p >= 128 else 0).convert(
                "1", dither=Image.Dither.NONE
            )
    except (AttributeError, TypeError):  # older Pillow fallback
        mono = canvas.convert("1")

    data = bytearray([0xFF] * (lw * lh // 8))
    px = mono.load()
    lbpr = lw // 8
    for yy in range(lh):
        row = yy * lbpr
        for xx in range(lw):
            if px[xx, yy] == 0:  # black
                data[row + xx // 8] &= ~(0x80 >> (xx % 8)) & 0xFF
    return bytes(data)


def image_to_frame(path, lw, lh, dither=True, invert=False, rotate=0, scale="fit"):
    from PIL import Image, ImageOps

    img = Image.open(path)
    if rotate:
        img = img.rotate(-rotate, expand=True)  # clockwise, like --rotate 90
    if invert:
        img = ImageOps.invert(img.convert("L"))
    return pil_to_frame(img, lw, lh, dither=dither, scale=scale)


def frame_command(data):
    crc = binascii.crc32(data) & 0xFFFFFFFF
    return "IMG %d %08x %s" % (len(data), crc, hexify(data))


def build_demo(lw, lh):
    from PIL import Image, ImageDraw

    img = Image.new("L", (lw, lh), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([1, 1, lw - 2, lh - 2], outline=0, width=3)
    d.rectangle([6, 6, lw - 7, lh - 7], outline=0, width=1)
    d.line([10, 10, lw - 10, lh - 10], fill=0, width=1)
    d.line([lw - 10, 10, 10, lh - 10], fill=0, width=1)
    # 8-step grey ramp (shows dithering levels)
    for i in range(8):
        shade = round(255 * i / 7)
        d.rectangle([16 + i * 26, lh - 70, 16 + i * 26 + 22, lh - 16], fill=shade)
    # checkerboard corner
    for yy in range(0, 10):
        for xx in range(0, 10):
            if (xx + yy) % 2 == 0:
                d.rectangle([16 + xx * 12, 20 + yy * 12,
                             27 + xx * 12, 31 + yy * 12], fill=0)
    d.ellipse([lw // 2 - 70, 40, lw // 2 - 10, 160], outline=0, width=2)
    d.ellipse([lw // 2 + 10, 40, lw // 2 + 70, 160], fill=0)
    return pil_to_frame(img, lw, lh)


# ---------------------------------------------------------------- commands

def run(e, args):
    a = args.command

    if a == "status":
        print(e.cmd("STAT"))
    elif a == "ping":
        print(e.cmd("PING"))
    elif a == "clear":
        print(e.cmd("CLEAR", timeout=30))
    elif a == "init":
        print(e.cmd("INIT", timeout=30))
    elif a in ("text", "file"):
        text = args.text if a == "text" else ""
        if getattr(args, "file", None):
            with open(args.file, "r") as f:
                text = f.read()
        if a == "file":
            with open(args.path, "r") as f:
                text = f.read()
        if not text.strip():
            raise SystemExit("error: empty text")
        print(e.cmd(text_payload(text, args.size, args.x, args.y), timeout=60))
    elif a == "image":
        data = image_to_frame(args.path, e.lw, e.lh,
                              dither=not args.no_dither,
                              invert=args.invert, rotate=args.rotate,
                              scale=args.scale)
        print(e.cmd(frame_command(data), timeout=60))
    elif a == "demo":
        print(e.cmd(frame_command(build_demo(e.lw, e.lh)), timeout=60))
    elif a == "rotate":
        print(e.cmd("ROT %d" % args.degrees, timeout=60))
        e.refresh_info()
    elif a == "save":
        print(e.cmd("SAVE"))
    elif a == "reset":
        print(e.cmd("RESET"))
        print("rebooting; reopening port...")
        time.sleep(3)
        e.reopen()
        e.wake()
        print(e.cmd("STAT"))
    elif a == "sleep":
        secs = args.seconds
        print(e.cmd("SLEEP %d" % secs if secs > 0 else "SLEEP", timeout=10))
        print("Pico is in deep sleep; USB will drop. Wake = power cycle/reset.")
        if secs > 0:
            print("sleeping %ds, then waiting for it to wake..." % secs)
            time.sleep(secs + 4)
            e.reopen()
            e.wake()
            print(e.cmd("STAT"))
    elif a == "repl":
        print(e.cmd("EXIT"))
        print("server stopped; the REPL is yours (mpremote connect <port>)")
    else:  # unreachable
        raise AssertionError(a)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log raw serial traffic")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="query Pico memory/firmware/last content")
    sub.add_parser("ping", help="check the server is alive")
    sub.add_parser("clear", help="blank the panel to white")
    sub.add_parser("init", help="re-init the panel controller")
    sub.add_parser("save", help="persist last rendered content across reboots")
    sub.add_parser("demo", help="show a test pattern")
    sub.add_parser("repl", help="stop the server, free the port for the REPL")

    p = sub.add_parser("text", help='render text, e.g. text "HELLO" --size 3')
    p.add_argument("text", nargs="?", default="", help="text to display; "
                   "\\n = newline; [SIZE:n] markup allowed; uppercased")
    p.add_argument("--file", help="read text from a file instead")
    p.add_argument("--size", type=int, help="font scale (1-4); wraps in [SIZE:n]")
    p.add_argument("--x", type=int, default=DEFAULT_X)
    p.add_argument("--y", type=int, default=DEFAULT_Y)

    p = sub.add_parser("file", help="render a text file, e.g. file display.txt")
    p.add_argument("path")
    p.add_argument("--size", type=int)
    p.add_argument("--x", type=int, default=DEFAULT_X)
    p.add_argument("--y", type=int, default=DEFAULT_Y)

    p = sub.add_parser("image", help="convert + display an image")
    p.add_argument("path")
    p.add_argument("--no-dither", action="store_true", help="hard threshold")
    p.add_argument("--invert", action="store_true")
    p.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0)
    p.add_argument("--scale", choices=("fit", "stretch", "none"), default="fit",
                   help="fit=preserve aspect, stretch=fill panel, none=1:1 centered")

    p = sub.add_parser("rotate", help="rotate content 0/90/180/270 (persisted)")
    p.add_argument("degrees", type=int, choices=(0, 90, 180, 270))

    p = sub.add_parser("reset", help="reboot the Pico")
    p = sub.add_parser("sleep", help="deep sleep; display keeps its image")
    p.add_argument("seconds", type=int, help="0 = until power cycle")

    args = parser.parse_args()
    e = Eink(find_port(args.port), verbose=args.verbose)
    e.wake()
    try:
        run(e, args)
    except serial.SerialException as ex:
        raise SystemExit("error: USB serial failure (%s). Unplug/replug the "
                         "Pico and retry." % ex)
    finally:
        e.s.close()


if __name__ == "__main__":
    main()

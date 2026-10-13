#!/usr/bin/env python3
"""Local web UI for the e-ink display.

Serves webui/static and a small JSON API that drives the Pico through
einkctl (which owns the serial protocol). Text previews are rendered
with the device's actual 5x8 font; image conversion reuses the same
Pillow pipeline as the einkctl CLI, so the preview is exactly what the
panel will show.

    python3 webui/server.py [--port 8515] [--host 127.0.0.1]
                            [--serial /dev/ttyACM0] [--no-browser] [-v]

Server needs only the standard library; pyserial/Pillow come via
einkctl. Binds to localhost by default — don't expose it to untrusted
networks, and note the serial port can't be shared with the einkctl
CLI at the same time.
"""

import argparse
import base64
import binascii
import io
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                        # einkctl
sys.path.insert(0, os.path.join(ROOT, "pico"))  # device font module

import einkctl            # noqa: E402
from simple_font import draw_text  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

SERIAL_PORT = None
VERBOSE = False

LOCK = threading.Lock()
DEV = None  # einkctl.Eink or None
INFO = {"rot": 270, "lw": 416, "lh": 240}  # last known device state

STAT_RE = re.compile(r"MEM=(\d+) FW=(\S+) PROTO=(\S+) LAST=(\S+) "
                     r"ROT=(\d+) SIZE=(\d+)x(\d+)")


class DeviceError(Exception):
    """User-visible failure (device replied ERR or serial link broke)."""


def get_device():
    global DEV
    if DEV is None:
        DEV = einkctl.Eink(einkctl.find_port(SERIAL_PORT))
        DEV.wake()
    return DEV


def drop_device():
    global DEV
    try:
        if DEV is not None:
            DEV.s.close()
    except Exception:
        pass
    DEV = None


def device_call(fn):
    """Run fn(dev) under the lock; turn failures into DeviceError."""
    global DEV
    with LOCK:
        try:
            return fn(get_device())
        except einkctl.serial.SerialException as ex:
            drop_device()
            raise DeviceError("USB serial failure: %s — replug the Pico "
                              "and retry" % ex)
        except SystemExit as ex:  # Eink.wake() gives up this way
            drop_device()
            raise DeviceError(str(ex))


# ---------------------------------------------------------------- previews

class _FakePanel:
    """set_pixel() target that draws into a PIL image (logical coords)."""

    def __init__(self, img):
        self.img = img
        self.w, self.h = img.size

    def set_pixel(self, x, y, color):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.img.putpixel((x, y), 0 if color else 255)


def text_preview_image(text, size, x, y):
    from PIL import Image
    lw, lh = INFO["lw"], INFO["lh"]
    if size:  # mirror einkctl.text_payload so the preview matches exactly
        text = "[SIZE:%d]%s" % (size, text)
    img = Image.new("L", (lw, lh), 255)
    draw_text(_FakePanel(img), text.upper(), x, y, size=1, width=lw)
    return img


def frame_to_image(data, w, h):
    from PIL import Image
    return Image.frombytes("1", (w, h), bytes(data)).convert("L")


def png_dataurl(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- operations

def op_status():
    def f(dev):
        reply = dev.cmd("STAT", timeout=10)
        if not reply.startswith("OK"):
            raise DeviceError(reply)
        m = STAT_RE.search(reply)
        if not m:
            raise DeviceError("unexpected STAT reply: %s" % reply)
        INFO.update(rot=int(m.group(5)), lw=int(m.group(6)), lh=int(m.group(7)))
        return {"mem": int(m.group(1)), "fw": m.group(2), "last": m.group(4),
                "rot": INFO["rot"], "lw": INFO["lw"], "lh": INFO["lh"],
                "port": dev.port}
    return device_call(f)


def op_text(text, size, x, y):
    if not text.strip():
        raise DeviceError("empty text (use Clear to blank the panel)")
    payload = einkctl.text_payload(text, size if size else None, x, y)

    def f(dev):
        reply = dev.cmd(payload, timeout=60)
        if not reply.startswith("OK"):
            raise DeviceError(reply)
        return {"reply": reply}
    out = device_call(f)
    out["preview"] = png_dataurl(text_preview_image(text, size, x, y))
    return out


def op_text_preview(text, size, x, y):
    return {"preview": png_dataurl(text_preview_image(text, size, x, y))}


def op_image_convert(data_b64, scale, invert, rotate, dither):
    from PIL import Image, ImageOps
    raw = base64.b64decode(data_b64)
    img = Image.open(io.BytesIO(raw))
    if rotate:
        img = img.rotate(-rotate, expand=True)
    if invert:
        img = ImageOps.invert(img.convert("L"))
    lw, lh = INFO["lw"], INFO["lh"]
    frame = einkctl.pil_to_frame(img, lw, lh, dither=dither, scale=scale)
    return {"preview": png_dataurl(frame_to_image(frame, lw, lh)),
            "frame": base64.b64encode(frame).decode(),
            "w": lw, "h": lh}


def op_image_send(frame_b64):
    data = base64.b64decode(frame_b64)
    expect = INFO["lw"] * INFO["lh"] // 8
    if len(data) != expect:
        raise DeviceError("frame is %d bytes but the device expects %d "
                          "(rotation changed? convert again)"
                          % (len(data), expect))
    crc = binascii.crc32(data) & 0xFFFFFFFF
    line = "IMG %d %08x %s" % (len(data), crc, einkctl.hexify(data))

    def f(dev):
        reply = dev.cmd(line, timeout=60)
        if not reply.startswith("OK"):
            raise DeviceError(reply)
        return {"reply": reply}
    return device_call(f)


def op_rotate(deg):
    def f(dev):
        reply = dev.cmd("ROT %d" % deg, timeout=60)
        if not reply.startswith("OK"):
            raise DeviceError(reply)
        m = re.search(r"ROT=(\d+) SIZE=(\d+)x(\d+)", reply)
        if m:
            INFO.update(rot=int(m.group(1)), lw=int(m.group(2)),
                        lh=int(m.group(3)))
        return {"reply": reply, "rot": INFO["rot"], "lw": INFO["lw"],
                "lh": INFO["lh"]}
    return device_call(f)


def op_command(cmd, seconds=0):
    if cmd == "clear":
        line, tmo = "CLEAR", 30
    elif cmd == "demo":
        line, tmo = einkctl.frame_command(
            einkctl.build_demo(INFO["lw"], INFO["lh"])), 60
    elif cmd == "save":
        line, tmo = "SAVE", 15
    elif cmd == "repl":
        line, tmo = "EXIT", 10
    elif cmd == "reset":
        line, tmo = "RESET", 10
    elif cmd == "sleep":
        line, tmo = ("SLEEP %d" % seconds if seconds else "SLEEP"), 10
    else:
        raise DeviceError("unknown command %r" % cmd)

    def f(dev):
        reply = dev.cmd(line, timeout=tmo)
        if not reply.startswith("OK"):
            raise DeviceError(reply)
        return {"reply": reply}
    out = device_call(f)

    if cmd == "reset":  # USB drops; give it a moment, then come back
        time.sleep(3)

        def g(dev):
            dev.reopen()
            dev.wake()
        try:
            device_call(g)
        except DeviceError:
            pass  # next status refresh will retry
    elif cmd == "repl":
        drop_device()
    return out


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if VERBOSE:
            super().log_message(fmt, *args)

    def _reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, ctype):
        path = os.path.join(STATIC_DIR, name)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            self._reply({"ok": False, "error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._file("index.html", "text/html; charset=utf-8")
        elif self.path == "/app.js":
            self._file("app.js", "application/javascript; charset=utf-8")
        elif self.path == "/style.css":
            self._file("style.css", "text/css; charset=utf-8")
        elif self.path == "/api/status":
            try:
                self._reply({"ok": True, **op_status()})
            except DeviceError as ex:
                self._reply({"ok": False, "error": str(ex)})
        else:
            self._reply({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, json.JSONDecodeError):
            self._reply({"ok": False, "error": "bad JSON body"}, 400)
            return
        try:
            out = self._route(body)
            self._reply({"ok": True, **out})
        except DeviceError as ex:
            self._reply({"ok": False, "error": str(ex)})
        except Exception as ex:  # noqa: BLE001 - report, keep serving
            self._reply({"ok": False, "error": "server error: %s" % ex}, 500)

    def _route(self, body):
        route = self.path
        if route == "/api/text":
            return op_text(body.get("text", ""), int(body.get("size") or 0),
                           int(body.get("x", 20)), int(body.get("y", 50)))
        if route == "/api/text/preview":
            return op_text_preview(body.get("text", ""),
                                   int(body.get("size") or 0),
                                   int(body.get("x", 20)),
                                   int(body.get("y", 50)))
        if route == "/api/image/convert":
            data = body.get("data", "")
            if "," in data:  # strip data:...;base64, prefix
                data = data.split(",", 1)[1]
            return op_image_convert(data, body.get("scale", "fit"),
                                    bool(body.get("invert")),
                                    int(body.get("rotate") or 0),
                                    bool(body.get("dither", True)))
        if route == "/api/image/send":
            return op_image_send(body.get("frame", ""))
        if route == "/api/rotate":
            return op_rotate(int(body.get("deg", 0)))
        if route == "/api/command":
            return op_command(body.get("cmd", ""),
                              int(body.get("seconds") or 0))
        raise DeviceError("unknown endpoint %s" % route)


def main():
    global SERIAL_PORT, VERBOSE
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8515,
                   help="HTTP port (default 8515)")
    p.add_argument("--host", default="127.0.0.1", help="bind address")
    p.add_argument("--serial", help="serial port (default: auto-detect)")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    SERIAL_PORT, VERBOSE = a.serial, a.verbose

    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    url = "http://%s:%d/" % (a.host, a.port)
    print("e-ink web UI: %s  (Ctrl-C to stop)" % url)
    if not a.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

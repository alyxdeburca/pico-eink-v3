"""USB serial command server for the e-ink panel.

Run automatically at boot by main.py. Lets a PC drive the display over
the USB serial link without touching files on the Pico.

Rotation: content is authored in a LOGICAL coordinate space whose size
depends on the configured rotation (STAT replies with SIZE=WxH):

    ROT=0/180  -> logical 240x416 (portrait, as wired)
    ROT=90/270 -> logical 416x240 (landscape)

With ROT=270 the panel is meant to be physically turned 90 degrees
clockwise; the panel's physical top edge (its "TOP" in the wiring
orientation) then appears on the RIGHT side of the viewed image. With
ROT=90 turn the panel 90 degrees counter-clockwise and the physical top
edge appears on the LEFT. ROT=180 shows portrait upside down. The
setting is persisted to /rot.cfg and applies to boot rendering too.

Protocol: line-based ASCII, one reply line per command. Replies are
always a single line starting with ``OK`` or ``ERR``. Payloads are
hex-encoded so newlines/binary data can never corrupt the framing.

    PING                    -> OK PONG
    STAT                    -> OK MEM=<free> FW=<ver> PROTO=<n> LAST=<none|text|frame>
                               ROT=<deg> SIZE=<W>x<H>
    TEXT <x> <y> <hex>      -> draw hex-encoded text at logical (x, y), refresh
    IMG <n> <crc> <hex>     -> draw packed 1-bit LOGICAL frame (n bytes, crc32), refresh
    CLEAR                   -> blank the panel to white
    INIT                    -> re-init the panel controller (buffer kept)
    SAVE                    -> persist last rendered content for boot
    ROT <0|90|180|270>      -> set rotation, persist, re-render last content
    RESET                   -> soft-reset the Pico
    SLEEP [<seconds>]       -> deep sleep; display keeps its last image
    HELP                    -> one-line command list
    EXIT                    -> stop the server, drop back to the REPL

Text is drawn by simple_font.draw_text, which understands inline
``[SIZE:n]`` markup (n = pixel scale) and "\n" line breaks. IMG frames
are packed 1 bit per pixel, MSB first, row-major over the LOGICAL
size; a 0 bit is a black pixel, 1 is white.
"""

import binascii
import gc
import machine
import sys
import time

from epd_driver import EPD
from simple_font import draw_text

PROTO = "1"
PANEL_W = 240
PANEL_H = 416
FRAME_BYTES = PANEL_W * PANEL_H // 8  # 12480

TEXT_X = 20
TEXT_Y = 50
TEXT_FILE = "/display.txt"
FRAME_FILE = "/frame.raw"
PREF_FILE = "/boot.pref"
ROT_FILE = "/rot.cfg"
DEFAULT_ROT = 270  # damaged panel edge moved out of the way; see README
ROTATIONS = (0, 90, 180, 270)

epd = None
last = None  # ("text", str, x, y) or ("frame", bytes)
rot = DEFAULT_ROT
LW, LH = PANEL_W, PANEL_H  # logical (content) dimensions; set by set_rot()


def set_rot(n):
    global rot, LW, LH
    rot = n
    if n in (90, 270):
        LW, LH = PANEL_H, PANEL_W
    else:
        LW, LH = PANEL_W, PANEL_H


def load_rot():
    set_rot(DEFAULT_ROT)
    try:
        with open(ROT_FILE) as f:
            n = int(f.read().strip())
        if n in ROTATIONS:
            set_rot(n)
    except (OSError, ValueError):
        pass


def save_rot():
    try:
        with open(ROT_FILE, "w") as f:
            f.write(str(rot))
    except OSError:
        pass


class RotPainter:
    """set_pixel() shim: maps logical (x, y) onto the physical panel.

    Logical x is horizontal and y is vertical in the content space; the
    physical panel keeps its wired orientation (240 wide, 416 tall).
    """

    def __init__(self, target):
        self.target = target

    def set_pixel(self, x, y, color):
        if rot == 270:
            self.target.set_pixel(y, PANEL_H - 1 - x, color)
        elif rot == 90:
            self.target.set_pixel(PANEL_W - 1 - y, x, color)
        elif rot == 180:
            self.target.set_pixel(PANEL_W - 1 - x, PANEL_H - 1 - y, color)
        else:
            self.target.set_pixel(x, y, color)


def render_text(text, x=TEXT_X, y=TEXT_Y):
    global last
    text = text.upper()
    epd.clear()
    draw_text(RotPainter(epd), text, x, y, size=1, width=LW)
    epd.display()
    last = ("text", text, x, y)


def blit_frame(data):
    """Unpack a logical packed frame (0 bit = black) into the physical
    buffer, applying the current rotation."""
    if rot == 0:
        return bytearray(data)
    phys = bytearray(FRAME_BYTES)  # all white
    lbpr = LW >> 3
    w1, h1 = PANEL_W - 1, PANEL_H - 1
    for v in range(LH):
        lrow = v * lbpr
        for u8 in range(lbpr):
            b = data[lrow + u8]
            if b == 0xFF:
                continue
            u0 = u8 << 3
            for i in range(8):
                if b & (0x80 >> i):
                    continue
                u = u0 + i
                if rot == 270:
                    x, y = v, h1 - u
                elif rot == 90:
                    x, y = w1 - v, u
                else:  # 180
                    x, y = w1 - u, h1 - v
                phys[(y * PANEL_W + x) >> 3] &= (0x80 >> (x & 7)) ^ 0xFF
    return phys


def render_frame(data):
    global last
    epd.buffer = blit_frame(data)
    epd.display()
    last = ("frame", data)


def _try_boot_text():
    try:
        with open(TEXT_FILE) as f:
            content = f.read().strip()
    except OSError:
        return False
    size = 1
    if content.startswith("SIZE:"):
        parts = content.split("\n", 1)
        try:
            size = int(parts[0].split(":")[1])
        except (ValueError, IndexError):
            size = 1
        content = parts[1] if len(parts) > 1 else ""
    render_text(content, TEXT_X, TEXT_Y)
    return True


def _try_boot_frame():
    try:
        with open(FRAME_FILE, "rb") as f:
            data = f.read()
    except OSError:
        return False
    if len(data) != FRAME_BYTES:
        print("WARN %s wrong size: %d" % (FRAME_FILE, len(data)))
        return False
    render_frame(data)
    return True


def boot_render():
    """On boot: show whatever was last SAVEd (text preferred by default)."""
    pref = "text"
    try:
        with open(PREF_FILE) as f:
            pref = f.read().strip()
    except OSError:
        pass
    if pref == "img":
        if _try_boot_frame():
            return
        _try_boot_text()
    else:
        if _try_boot_text():
            return
        _try_boot_frame()


def cmd_text(args):
    x_s, y_s, hexdata = args.split(" ", 2)
    text = binascii.unhexlify(hexdata).decode("utf-8")
    render_text(text, int(x_s), int(y_s))


def cmd_img(args):
    n_s, crc_s, hexdata = args.split(" ", 2)
    data = binascii.unhexlify(hexdata)
    if len(data) != int(n_s):
        raise ValueError("length mismatch: header %d, got %d" % (int(n_s), len(data)))
    if binascii.crc32(data) & 0xFFFFFFFF != int(crc_s, 16):
        raise ValueError("crc mismatch")
    render_frame(data)


def cmd_save(args):
    if last is None:
        raise ValueError("nothing to save")
    if last[0] == "text":
        with open(TEXT_FILE, "w") as f:
            f.write(last[1])
        pref = "text"
    else:
        with open(FRAME_FILE, "wb") as f:
            f.write(last[1])
        pref = "img"
    with open(PREF_FILE, "w") as f:
        f.write(pref)
    print("OK SAVE %s" % (TEXT_FILE if pref == "text" else FRAME_FILE))


def cmd_rot(args):
    n = int(args)
    if n not in ROTATIONS:
        raise ValueError("rotation must be one of 0, 90, 180, 270")
    if n != rot:
        set_rot(n)
        save_rot()
        if last is not None:  # show the change immediately
            if last[0] == "text":
                render_text(last[1], last[2], last[3])
            else:
                render_frame(last[1])


def handle(line):
    """Run one command. Returns True to keep serving, False to exit."""
    global last
    cmd, _, args = line.partition(" ")
    cmd = cmd.upper()
    if cmd == "PING":
        print("OK PONG")
    elif cmd == "STAT":
        print("OK MEM=%d FW=%s PROTO=%s LAST=%s ROT=%d SIZE=%dx%d"
              % (gc.mem_free(), sys.version.split(";")[0].strip(), PROTO,
                 "none" if last is None else last[0], rot, LW, LH))
    elif cmd == "TEXT":
        cmd_text(args)
        print("OK TEXT")
    elif cmd == "IMG":
        cmd_img(args)
        print("OK IMG %dx%d" % (LW, LH))
    elif cmd == "CLEAR":
        epd.clear()
        epd.display()
        last = None
        print("OK CLEAR")
    elif cmd == "INIT":
        epd.init()
        print("OK INIT")
    elif cmd == "SAVE":
        cmd_save(args)
    elif cmd == "ROT":
        cmd_rot(args)
        print("OK ROT=%d SIZE=%dx%d" % (rot, LW, LH))
    elif cmd == "RESET":
        print("OK RESET")
        sys.stdout.flush()
        time.sleep_ms(200)
        machine.reset()
    elif cmd == "SLEEP":
        secs = int(args) if args.strip() else 0
        print("OK SLEEP %d" % secs)
        sys.stdout.flush()
        if secs > 0:
            machine.deepsleep(secs * 1000)
        machine.deepsleep()
    elif cmd == "HELP":
        print("OK PING STAT TEXT x y hex IMG n crc hex CLEAR INIT SAVE "
              "ROT 0|90|180|270 RESET SLEEP [sec] HELP EXIT")
    elif cmd == "EXIT":
        print("OK BYE")
        return False
    else:
        print("ERR unknown command %r (try HELP)" % cmd)
    return True


def serve():
    """Boot entry point: init panel, render saved content, serve commands."""
    global epd
    load_rot()
    epd = EPD()
    epd.init()
    boot_render()
    print("EINK SERVER %s READY ROT=%d SIZE=%dx%d" % (PROTO, rot, LW, LH))
    while True:
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            print("OK BYE")
            break
        if not line:
            # EOF (e.g. USB host gone): idle until data returns
            time.sleep_ms(20)
            continue
        line = line.strip()
        if not line:
            continue
        try:
            if not handle(line):
                break
        except KeyboardInterrupt:
            print("OK BYE")
            break
        except Exception as e:
            print("ERR %s" % e)

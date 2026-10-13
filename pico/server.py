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
FAST_FILE = "/fast.cfg"
DEFAULT_ROT = 270  # damaged panel edge moved out of the way; see README
ROTATIONS = (0, 90, 180, 270)
FULL_EVERY = 10    # partial renders between automatic full (ghost-clear) refreshes

epd = None
last = None  # ("text", str, x, y) or ("frame", bytes)
rot = DEFAULT_ROT
LW, LH = PANEL_W, PANEL_H  # logical (content) dimensions; set by set_rot()
fast = True      # partial (0.35s) refresh instead of full (~1.5s)
partials = 0     # partial renders since the last full refresh
rendered_once = False  # False until a full refresh has synced the panel RAM


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


def load_fast():
    global fast
    try:
        with open(FAST_FILE) as f:
            fast = f.read().strip().upper() != "OFF"
    except OSError:
        pass


def save_fast():
    try:
        with open(FAST_FILE, "w") as f:
            f.write("ON" if fast else "OFF")
    except OSError:
        pass


def show_buffer():
    """Refresh the panel with epd.buffer, choosing partial vs full.

    Partial (0.35s, no flash) whenever fast mode is on and the panel
    RAM is in sync; every FULL_EVERY partials (and after boot/INIT/
    CLEAR) a full refresh keeps ghosting in check.
    """
    global partials, rendered_once
    if fast and rendered_once and partials < FULL_EVERY:
        epd.display(partial=True)
        partials += 1
    else:
        epd.display(partial=False)
        partials = 0
        rendered_once = True
    gc.collect()


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
    show_buffer()
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
    show_buffer()
    last = ("frame", data)


def _hline(p, x0, x1, y):
    for x in range(x0, x1 + 1):
        p.set_pixel(x, y, 1)


def _vline(p, y0, y1, x):
    for y in range(y0, y1 + 1):
        p.set_pixel(x, y, 1)


def _center_x(text, size):
    return (LW - len(text) * 6 * size) // 2


def draw_splash():
    """Boot splash describing the project, shown when nothing was SAVEd."""
    p = RotPainter(epd)
    epd.clear()
    # double frame
    for i in range(3):
        _hline(p, 5 + i, LW - 6 - i, 5 + i)
        _hline(p, 5 + i, LW - 6 - i, LH - 6 - i)
        _vline(p, 5 + i, LH - 6 - i, 5 + i)
        _vline(p, 5 + i, LH - 6 - i, LW - 6 - i)
    _hline(p, 13, LW - 14, 13)
    _hline(p, 13, LW - 14, LH - 14)
    _vline(p, 13, LH - 14, 13)
    _vline(p, 13, LH - 14, LW - 14)
    # titles (shrinks on narrow/portrait orientations)
    ts = 4 if LW >= 380 else 2
    ss = 2 if LW >= 380 else 1
    draw_text(p, "PICO E-INK", _center_x("PICO E-INK", ts), 40,
              size=ts, width=LW)
    draw_text(p, "SYSTEM MONITOR", _center_x("SYSTEM MONITOR", ts), 40 + 44,
              size=ts, width=LW)
    # rule + tagline + features
    _hline(p, 70, LW - 71, 130)
    draw_text(p, "LIVE PC STATS ON E-PAPER",
              _center_x("LIVE PC STATS ON E-PAPER", ss), 142,
              size=ss, width=LW)
    draw_text(p, "CPU - RAM - GPU - TEMP",
              _center_x("CPU - RAM - GPU - TEMP", ss), 142 + 22,
              size=ss, width=LW)
    # little bar-chart motif
    heights = (10, 18, 26, 18, 10)
    bw, gap = 10, 8
    total = len(heights) * bw + (len(heights) - 1) * gap
    x0 = (LW - total) // 2
    base = LH - 22
    for i, h in enumerate(heights):
        bx = x0 + i * (bw + gap)
        for dx in range(bw):
            _vline(p, base - h, base, bx + dx)
    _hline(p, x0 - 6, x0 + total + 5, base + 2)
    show_buffer()


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
    """Boot screen: SAVEd content if any, else the project splash."""
    pref = None
    try:
        with open(PREF_FILE) as f:
            pref = f.read().strip()
    except OSError:
        pass
    if pref is not None:
        # some content was explicitly SAVEd: honour it (pref kind first)
        if pref == "img":
            if _try_boot_frame() or _try_boot_text():
                return
        else:
            if _try_boot_text() or _try_boot_frame():
                return
    draw_splash()


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
    global last, fast, partials, rendered_once
    cmd, _, args = line.partition(" ")
    cmd = cmd.upper()
    if cmd == "PING":
        print("OK PONG")
    elif cmd == "STAT":
        print("OK MEM=%d FW=%s PROTO=%s LAST=%s ROT=%d SIZE=%dx%d FAST=%s PARTIALS=%d/%d"
              % (gc.mem_free(), sys.version.split(";")[0].strip(), PROTO,
                 "none" if last is None else last[0], rot, LW, LH,
                 "on" if fast else "off", partials, FULL_EVERY))
    elif cmd == "TEXT":
        cmd_text(args)
        print("OK TEXT")
    elif cmd == "IMG":
        cmd_img(args)
        print("OK IMG %dx%d" % (LW, LH))
    elif cmd == "CLEAR":
        epd.clear()
        epd.display(partial=False)  # blanking: always full
        last = None
        partials = 0
        rendered_once = True
        print("OK CLEAR")
    elif cmd == "INIT":
        epd.init()
        rendered_once = False  # panel RAM state unknown: next render is full
        print("OK INIT")
    elif cmd == "SAVE":
        cmd_save(args)
    elif cmd == "ROT":
        cmd_rot(args)
        print("OK ROT=%d SIZE=%dx%d" % (rot, LW, LH))
    elif cmd == "FAST":
        arg = args.strip().upper()
        if arg in ("ON", "OFF"):
            fast = arg == "ON"
            save_fast()
        print("OK FAST=%s (full every %d partials)" % ("ON" if fast else "OFF", FULL_EVERY))
    elif cmd == "FULL":
        if last is None:
            raise ValueError("nothing rendered yet")
        saved, fast = fast, False  # force one full refresh
        try:
            if last[0] == "text":
                render_text(last[1], last[2], last[3])
            else:
                render_frame(last[1])
        finally:
            fast = saved
        partials = 0
        print("OK FULL")
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
              "ROT 0|90|180|270 FAST ON|OFF FULL RESET SLEEP [sec] HELP EXIT")
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
    load_fast()
    epd = EPD()
    epd.init()
    boot_render()  # full refresh: syncs the panel's previous-RAM baseline
    print("EINK SERVER %s READY ROT=%d SIZE=%dx%d FAST=%s" % (PROTO, rot, LW, LH, "on" if fast else "off"))
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

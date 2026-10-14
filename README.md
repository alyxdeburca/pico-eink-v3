# pico-eink-v3 — PC-controlled e-ink display

A Raspberry Pi Pico W drives a 240×416 monochrome e-ink panel over SPI.
The Pico runs a small serial command server, so the display can be driven
live from a PC with `einkctl.py` — no more editing files and rebooting.

## Hardware

| Panel pin | Pico GPIO |
|-----------|-----------|
| CS        | GP15      |
| DC        | GP10      |
| RST       | GP8       |
| BUSY      | GP7       |
| SCK       | GP18      |
| MOSI      | GP19      |

(SPI0 @ 4 MHz, mode 0 — unchanged from the original working setup.)

## Setup

**Pico** — hold BOOTSEL, plug in USB, drop `pico/` onto the drive, or:

    mpremote connect /dev/ttyACM0 fs cp pico/main.py :main.py
    mpremote connect /dev/ttyACM0 fs cp pico/server.py :server.py
    mpremote connect /dev/ttyACM0 fs cp pico/epd_driver.py :epd_driver.py
    mpremote connect /dev/ttyACM0 fs cp pico/simple_font.py :simple_font.py

**PC**:

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python3 einkctl.py demo

If you get a permission error on `/dev/ttyACM0`:

    sudo usermod -aG uucp,dialout $USER   # then log out/in
    # or ad hoc: sudo chmod a+rw /dev/ttyACM0
    # or without re-login: echo 'python3 einkctl.py demo' | newgrp uucp

## Usage

    einkctl.py status                       # memory, firmware, last content
    einkctl.py text "HELLO WORLD" --size 3  # instant text
    einkctl.py text "Line one\nLine two" --size 2 --x 10 --y 30
    einkctl.py file display.txt             # render a text file
    einkctl.py image photo.jpg              # auto-fit + Floyd-Steinberg dither
    einkctl.py image pic.png --invert --no-dither --rotate 90 --scale stretch
    einkctl.py clear
    einkctl.py demo                         # test pattern
    einkctl.py save                         # survive power cycles
    einkctl.py sleep 3600                   # deep sleep (image stays on panel)
    einkctl.py reset
    einkctl.py repl                         # release the Pico for mpremote/REPL

Notes:

- Text is uppercased: the built-in 5×8 font has no lowercase. Inline
  `[SIZE:n]` markup works anywhere in the text (`n` = integer scale).
- `--size` is a convenience that prepends one `[SIZE:n]` tag.
- Images are converted on the PC (grayscale → 1-bit) and streamed as
  packed rows, so any format PIL reads works (PNG/JPEG/BMP/…).
- `save` writes the last rendered content; a subsequent boot shows it
  (`/display.txt` for text, `/frame.raw` for images, choice in `/boot.pref`).
  **Without saved content the panel shows a built-in splash screen**
  ("PICO E-INK — SYSTEM MONITOR") at boot. With the sysmon service
  enabled, the splash is visible only briefly before the service clears
  the panel and draws the dashboard.
  See the rotation section below — the default orientation is rotated.
- The tool soft-reboots the Pico automatically if it finds the REPL
  instead of the server — you can freely mix `einkctl` and `mpremote`.

## Rotation (damaged-panel workaround)

The top edge of the panel (physical y≈0 in the wiring orientation) is
damaged, so content is rotated by default:

    einkctl.py rotate 270      # default; turn panel 90° clockwise to view
    einkctl.py rotate 90       # alternative; turn panel 90° counter-clockwise
    einkctl.py rotate 0        # original portrait orientation
    einkctl.py rotate 180      # portrait, upside down

- `rotate 270` → view the panel landscape, turned **90° clockwise**; the
  damaged edge ends up on the **right** side of the image.
- `rotate 90` → turned **90° counter-clockwise**; damaged edge on the
  **left**.
- Content is authored in landscape logical coordinates (416×240 for
  90/270; STAT reports `SIZE=WxH`, and `einkctl` picks it up
  automatically). Keep content away from the damaged edge: with the
  default 270 that's the right side of your text/images.
- The setting is persisted on the Pico (`/rot.cfg`) and applies to boot
  rendering, `TEXT` coordinates, `IMG` frames, and `demo`.
- `rotate` immediately re-renders the last displayed content so you can
  A/B the orientations live.

## Protocol (Pico ↔ PC, one ASCII line per command, one reply line)

    PING                    -> OK PONG
    STAT                    -> OK MEM=<free> FW=<ver> PROTO=<n> LAST=<none|text|frame>
                               ROT=<deg> SIZE=<W>x<H>   (logical size)
    TEXT <x> <y> <hex>      -> OK TEXT        (hex = utf-8 text; x,y logical)
    IMG <n> <crc32> <hex>   -> OK IMG <W>x<H> (hex = packed LOGICAL frame)
    CLEAR                   -> OK CLEAR
    INIT                    -> OK INIT
    SAVE                    -> OK SAVE /display.txt | /frame.raw
    ROT <0|90|180|270>      -> OK ROT=<deg> SIZE=<W>x<H>  (persists + re-renders)
    RESET                   -> OK RESET       (then reboots)
    SLEEP [<seconds>]       -> OK SLEEP <s>   (deepsleep; 0 = until reset)
    HELP                    -> OK <commands>
    EXIT                    -> OK BYE         (server stops, REPL takes over)

Errors reply `ERR <reason>`. Payloads are hex so `\n` or binary bytes can
never break framing. IMG frames are 1 bit/pixel, MSB first, row-major
over the **logical** size from `STAT` (416×240 when rotated); a **0 bit
is black**. Add a CRC to every frame — `binascii.crc32(data) & 0xFFFFFFFF`.
The Pico applies the rotation when blitting; senders don't rotate.

## System monitor (live dashboard)

    python3 sysmon.py                 # CPU / RAM / GPU usage every 10 s
    python3 sysmon.py --interval 5    # faster refresh
    python3 sysmon.py                    # CPU / RAM / GPU usage every 10 s
    python3 sysmon.py --interval 5       # faster refresh
    python3 sysmon.py --refresh-every 60 # ghost wipe less often
    python3 sysmon.py --refresh-every 0  # periodic full refresh off
    python3 sysmon.py --once             # single update and exit

Renders a bar-gauge dashboard (bars are '#' filled / '-' empty) sized to
stay within 90% of the panel width, clear of the damaged edge, with the
system stats plus an ambient block: clock + date, local weather
(Open-Meteo, **no API key** — located by IP; override with `--lat` /
`--lon`), sunrise/sunset, moon phase, uptime and disk usage. Weather
refreshes every 30 min and is cached in `~/.cache/pico-eink/`
(offline it falls back to "WEATHER --" and a locally-computed moon
phase). The panel is cleared once when the service starts. Updates use
fast partial refresh restricted to the rectangle that actually changed —
and when nothing changed, the panel is left alone entirely — so the
screen is static between updates. By default every 30 s a full refresh
wipes accumulated ghosting in a single flash with the dashboard staying
visible (no white-out; tune with `--refresh-every`, disable with
`--refresh-every 0` — the device also auto-full-refreshes every 10
partial updates as a safety net). GPU usage
comes from `nvidia-smi` when available, otherwise from the drm fdinfo
counters in /proc (Intel/AMD, no root needed — the first cycle shows
"--" until a delta exists). CPU temperature comes from psutil sensors.
Uses the serial port exclusively: stop the web UI / einkctl first.

Font note: `pico/simple_font.py` gained `%`, `/` and a solid block `#`
glyph for this (re-upload it if you haven't since).

## Run at boot (systemd)

`systemd/eink-sysmon.service` keeps the dashboard updating from boot
(`--interval 20` inside the unit). The repo's `.venv` is what
the service runs from — create it once:

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

Install and enable:

    sudo cp systemd/eink-sysmon.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now eink-sysmon.service
    journalctl -u eink-sysmon -f        # follow the log

The unit runs as your user with `SupplementaryGroups=uucp` (serial
access without depending on login groups), restarts on failure, and
tolerates the Pico being unplugged (it retries each cycle). Remember
the serial port is exclusive — `systemctl stop eink-sysmon` before
using the web UI or einkctl.

## Web UI (browser control)

Even easier than the CLI:

    python3 webui/server.py        # opens http://127.0.0.1:8515 in your browser

Features: live text preview rendered with the device's real 5×8 font,
drag-&-drop image conversion (same Pillow dithering pipeline as the
CLI, preview = exactly what the panel will show), clear/demo/save/
reset/REPL buttons, and a rotation selector. The server needs only the
Python standard library (pyserial/Pillow come via einkctl) and talks to
the Pico through the same serial protocol.

Notes:

- It binds to `127.0.0.1` by default (`--host` to change). Don't expose
  it to untrusted networks — there's no authentication.
- The serial port is exclusive: stop the web UI (or the CLI) before
  using the other.
- `--port 8515` is the HTTP port (8080 is often taken), `--serial`
  overrides port auto-detection, `--no-browser` skips auto-open.

## Repo layout

    einkctl.py      PC-side CLI (this is what you run)
    webui/          browser UI: server.py + static/ (html/js/css)
    pico/           files that live on the Pico (main.py boots server.py)
    pico-backup/    untouched copies of the pre-rewrite code, kept for reference

## Troubleshooting

- **No serial port**: check `ls /dev/ttyACM*`; use `--port` to override.
- **Pico did not answer PING**: `server.py` missing or crashed —
  `mpremote connect /dev/ttyACM0` and check; upload again.
- **Weird port errors after unplug/replug**: `einkctl` re-detects; if
  needed pass `--port` explicitly.
- **Garbled/rotated image**: try `--rotate 90/180/270` or `--invert`.

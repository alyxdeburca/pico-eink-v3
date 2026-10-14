import machine
import time

class EPD:
    def __init__(self):
        self.width = 240
        self.height = 416
        self.buffer_size = self.width * self.height // 8

        self.cs = machine.Pin(15, machine.Pin.OUT)
        self.dc = machine.Pin(10, machine.Pin.OUT)
        self.res = machine.Pin(8, machine.Pin.OUT)
        self.busy = machine.Pin(7, machine.Pin.IN, machine.Pin.PULL_UP)

        self.spi = machine.SPI(0, baudrate=4000000, polarity=0, phase=0,
                              sck=machine.Pin(18), mosi=machine.Pin(19))

        self.buffer = bytearray(self.buffer_size)

    def cmd(self, command):
        self.dc.value(0)
        self.cs.value(0)
        self.spi.write(bytes([command]))
        self.cs.value(1)

    def data(self, data):
        self.dc.value(1)
        self.cs.value(0)
        if isinstance(data, int):
            self.spi.write(bytes([data]))
        else:
            self.spi.write(bytes(data))
        self.cs.value(1)

    def wait(self):
        count = 0
        timeout = 500
        while self.busy.value() == 0 and count < timeout:
            time.sleep_ms(10)
            count += 1
        if count >= timeout:
            print(f"WARN: Busy timeout after {count*10}ms")

    def reset(self):
        self.res.value(1)
        time.sleep_ms(20)
        self.res.value(0)
        time.sleep_ms(2)
        self.res.value(1)
        time.sleep_ms(20)

    def init(self):
        # GxEPD2-style init for GDEY037T03 (UC8253): reset + panel setting.
        # Power/timing defaults come from OTP; power is applied per refresh.
        self.reset()
        self.wait()
        self.cmd(0x00)   # panel setting
        self.data(0x1f)  # KW: 3f, KWR: 2F, BWROTP: 0f, BWOTP: 1f
        self.data(0x0d)

    def clear(self):
        # bytes multiply (1 byte/slot) instead of [0xFF]*n: a list on the
        # 32-bit RP2040 port costs 4 bytes/slot and can fail to find one
        # contiguous 50 KB block once the heap is fragmented
        self.buffer = bytearray(b"\xff" * self.buffer_size)

    def _write_frame(self, cmd, buf):
        """Stream a full frame buffer after a data command."""
        self.cmd(cmd)
        for i in range(0, len(buf), 4096):
            self.data(buf[i:i + 4096])

    def wait_refresh(self, floor_ms, timeout_ms=5000):
        """Wait for a display refresh to finish, tolerating a flaky or
        disconnected BUSY line.

        Once BUSY asserts (panel drives the line low) it is trusted:
        wait until it has stayed released for ~40ms. If it never asserts
        within floor_ms the panel is not responding — warn (the floor has
        then covered the waveform time, so the refresh is not cut short).
        """
        t0 = time.ticks_ms()
        highs = 0
        saw_low = False
        while True:
            el = time.ticks_diff(time.ticks_ms(), t0)
            if self.busy.value() == 0:
                saw_low = True
                highs = 0
            else:
                highs += 1
                if saw_low and el >= 300 and highs >= 4:
                    return
            if not saw_low and el >= floor_ms:
                print("WARN: panel BUSY not asserted (wiring / panel power?)")
                return
            if el >= timeout_ms:
                print("WARN: busy timeout after %dms" % el)
                return
            time.sleep_ms(10)

    def _refresh(self, tsset, cdi):
        """Run one refresh cycle with the UC8253 fast-waveform settings.

        tsset: 0xE5 forced-temperature/timing code (0x5A full, ~0.5s busy;
        0x6E partial, ~0.35s). cdi: 0x50 VCOM/data-interval value (0x97
        full, 0xD7 partial). TSFIX is undone afterwards by re-sending the
        panel setting, exactly like GxEPD2 does.
        """
        full = tsset == 0x5A
        self.cmd(0xE0)   # cascade setting (CCSET)
        self.data(0x02)  # TSFIX
        self.cmd(0xE5)   # force temperature (TSSET)
        self.data(tsset)
        self.cmd(0x50)   # VCOM and data interval
        self.data(cdi)
        self.cmd(0x04)   # power on
        time.sleep_ms(60)  # min pump-up even if BUSY reads flaky
        self.wait()
        self.cmd(0x12)   # display refresh
        self.wait_refresh(1000 if full else 600)
        self.cmd(0x02)   # power off
        time.sleep_ms(100)  # min pump-down
        self.wait()
        self.cmd(0x00)   # panel setting: undo TSFIX
        self.data(0x1f)
        self.data(0x0d)

    def _partial_window(self):
        """0x90 partial window covering the full panel (GxEPD2-style)."""
        x, y, w, h = 0, 0, self.width, self.height
        xe = (x + w - 1) | 0x07
        ye = y + h - 1
        x &= 0xFFF8
        self.cmd(0x90)
        self.data(x)
        self.data(xe)
        self.data(y >> 8)
        self.data(y & 0xFF)
        self.data(ye >> 8)
        self.data(ye & 0xFF)
        self.data(0x01)

    def _win_write(self, cmd, buf):
        """Write a frame inside a partial-in/partial-out window."""
        self.cmd(0x91)  # partial in
        self._partial_window()
        self._write_frame(cmd, buf)
        self.cmd(0x92)  # partial out

    def display(self, partial=False):
        """Show self.buffer. partial=True uses the 0.35s differential
        waveform (requires the previous frame still in the panel's
        'previous' RAM, which every refresh here keeps in sync)."""
        if partial:
            self._win_write(0x13, self.buffer)    # set current
            self.cmd(0x91)                        # partial in
            self._partial_window()
            self._refresh(0x6E, 0xD7)
            self.cmd(0x92)                        # partial out
            self._win_write(0x10, self.buffer)    # sync previous RAM
        else:
            self._write_frame(0x13, self.buffer)  # set current
            self._write_frame(0x10, self.buffer)  # old = new: clean full
            self._refresh(0x5A, 0x97)

    def fill_rect(self, x, y, w, h, color):
        """Fill a physical-space rectangle. color: 1=black, 0=white."""
        w = max(0, min(w, self.width - x))
        h = max(0, min(h, self.height - y))
        if w == 0 or h == 0 or x < 0 or y < 0:
            return
        x0b, x1b = x >> 3, (x + w - 1) >> 3
        lead = 0xFF >> (x & 7)
        trail = (0xFF >> (((x + w - 1) & 7) + 1)) ^ 0xFF
        rowbytes = self.width >> 3
        for yy in range(y, y + h):
            base = yy * rowbytes
            for bi in range(x0b, x1b + 1):
                if bi == x0b and bi == x1b:
                    m = lead & trail
                elif bi == x0b:
                    m = lead
                elif bi == x1b:
                    m = trail
                else:
                    m = 0xFF
                if color:
                    self.buffer[base + bi] &= m ^ 0xFF
                else:
                    self.buffer[base + bi] |= m

    def _window(self, x0, xe, y0, ye):
        """0x90 partial window from pre-aligned coordinates."""
        self.cmd(0x90)
        self.data(x0)
        self.data(xe)
        self.data(y0 >> 8)
        self.data(y0 & 0xFF)
        self.data(ye >> 8)
        self.data(ye & 0xFF)
        self.data(0x01)

    def _stream(self, cmd, chunks):
        """Send a data command with CS held low across all chunks."""
        self.cmd(cmd)
        self.dc.value(1)
        self.cs.value(0)
        for c in chunks:
            self.spi.write(bytes(c))
        self.cs.value(1)

    def _window_rows(self, x0, wb, y, h):
        """Frame-buffer slices covering a window, one tuple of bytes per row."""
        rowbytes = self.width >> 3
        base = y * rowbytes + (x0 >> 3)
        return [self.buffer[base + i * rowbytes: base + i * rowbytes + wb]
                for i in range(h)]

    def display_window(self, x, y, w, h, partial=True):
        """Refresh only a physical-space rectangle of the panel.

        Writes the window's pixels to the current RAM, runs the refresh
        with the window active, then syncs the same window into the
        previous RAM so later full-window partials stay correct.
        """
        x0 = x & 0xFFF8
        xe = (x + w - 1) | 0x07
        wb = (xe - x0 + 1) >> 3
        y1 = max(0, y)
        h = min(h, self.height - y1)
        if wb <= 0 or h <= 0:
            return
        rows = self._window_rows(x0, wb, y1, h)
        self.cmd(0x91)  # partial in
        self._window(x0, xe, y1, y1 + h - 1)
        self._stream(0x13, rows)
        self.cmd(0x92)  # partial out
        self.cmd(0x91)
        self._window(x0, xe, y1, y1 + h - 1)
        self._refresh(0x6E if partial else 0x5A,
                      0xD7 if partial else 0x97)
        self.cmd(0x92)
        self.cmd(0x91)
        self._window(x0, xe, y1, y1 + h - 1)
        self._stream(0x10, rows)
        self.cmd(0x92)

    def dirty_window(self, prev, cur):
        """Tight byte-aligned physical rect of differences between two
        frame buffers, or None when they are identical."""
        rowbytes = self.width >> 3
        ymin = -1
        ymax = -1
        xb0 = rowbytes
        xb1 = -1
        for y in range(self.height):
            base = y * rowbytes
            if prev[base:base + rowbytes] == cur[base:base + rowbytes]:
                continue
            if ymin < 0:
                ymin = y
            ymax = y
            for xb in range(rowbytes):
                if prev[base + xb] != cur[base + xb]:
                    if xb < xb0:
                        xb0 = xb
                    if xb > xb1:
                        xb1 = xb
        if ymin < 0:
            return None
        return (xb0 << 3, ymin, (xb1 - xb0 + 1) << 3, ymax - ymin + 1)

    def set_pixel(self, x, y, color):
        if x < 0 or x >= self.width or y < 0 or y >= self.height:
            return
        byte_index = (y * self.width + x) // 8
        bit_index = 7 - (x % 8)
        if color:
            self.buffer[byte_index] &= ~(1 << bit_index)
        else:
            self.buffer[byte_index] |= (1 << bit_index)

    def line(self, x0, y0, x1, y1, color=1):
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1

        if dx > dy:
            err = dx / 2
            while x0 != x1:
                self.set_pixel(x0, y0, color)
                err -= dy
                if err < 0:
                    y0 += sy
                    err += dx
                x0 += sx
        else:
            err = dy / 2
            while y0 != y1:
                self.set_pixel(x0, y0, color)
                err -= dx
                if err < 0:
                    x0 += sx
                    err += dy
                y0 += sy
        self.set_pixel(x0, y0, color)

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
        self.busy = machine.Pin(7, machine.Pin.IN)

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
        self.buffer = bytearray([0xFF] * self.buffer_size)

    def _write_frame(self, cmd, buf):
        """Stream a full frame buffer after a data command."""
        self.cmd(cmd)
        for i in range(0, len(buf), 4096):
            self.data(buf[i:i + 4096])

    def _refresh(self, tsset, cdi):
        """Run one refresh cycle with the UC8253 fast-waveform settings.

        tsset: 0xE5 forced-temperature/timing code (0x5A ~1.0s full,
        0x6E partial). cdi: 0x50 VCOM/data-interval value (0x97 full,
        0xD7 partial). TSFIX is undone afterwards by re-sending the
        panel setting, exactly like GxEPD2 does.
        """
        self.cmd(0xE0)   # cascade setting (CCSET)
        self.data(0x02)  # TSFIX
        self.cmd(0xE5)   # force temperature (TSSET)
        self.data(tsset)
        self.cmd(0x50)   # VCOM and data interval
        self.data(cdi)
        self.cmd(0x04)   # power on
        self.wait()
        self.cmd(0x12)   # display refresh
        time.sleep_ms(100)
        self.wait()
        self.cmd(0x02)   # power off
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

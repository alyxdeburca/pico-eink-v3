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
        self.reset()
        self.wait()

        self.cmd(0x00)
        self.data(0x1f)

        self.cmd(0x01)
        self.data(0x03)
        self.data(0x00)
        self.data(0x2b)
        self.data(0x2b)

        self.cmd(0x06)
        self.data(0x27)
        self.data(0x27)
        self.data(0x27)

        self.cmd(0x04)
        self.wait()

        self.cmd(0x00)
        self.data(0x1f)

        self.cmd(0x61)
        self.data(0xf0)
        self.data(0x01)
        self.data(0xa0)

        self.cmd(0x50)
        self.data(0x97)

        self.cmd(0x60)
        self.data(0x22)

    def clear(self):
        self.buffer = bytearray([0xFF] * self.buffer_size)

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

    def display(self):
        self.cmd(0x10)
        self.data(0x00)
        self.data(0x00)
        self.data(0x00)
        self.data(0x00)
        self.cmd(0x11)
        self.cmd(0x13)

        for i in range(0, len(self.buffer), 4096):
            chunk = self.buffer[i:i+4096]
            self.data(chunk)

        self.cmd(0x12)
        time.sleep_ms(100)
        self.wait()

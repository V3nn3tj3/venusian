import os
import struct
import threading
import time

from pymodbus.client import *
from pymodbus.client.sync import ModbusTcpClient, ModbusUdpClient, ModbusSerialClient
try:
    from pymodbus.utilities import computeCRC
except ImportError:
    try:
        from pymodbus.framer.rtu import FramerRTU as RTU
    except ImportError:
        from pymodbus.message.rtu import MessageRTU as RTU
    computeCRC = RTU.compute_CRC
try:
    from pymodbus.framer.rtu_framer import ModbusRtuFramer
    from pymodbus.framer.ascii_framer import ModbusAsciiFramer
except ImportError:
    from pymodbus.framer.rtu import FramerRTU as ModbusRtuFramer
    from pymodbus.framer.ascii import FramerAscii as ModbusAsciiFramer


class RefCount:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.refcount = 1
        self.in_transaction = False

    def get(self):
        self.refcount += 1
        return self

    def put(self):
        if self.refcount > 0:
            self.refcount -= 1
        if self.refcount == 0:
            self.close()

    def close(self):
        if self.refcount == 0 or self.in_transaction:
            super().close()

    def execute(self, *args):
        try:
            self.in_transaction = True
            return super().execute(*args)
        finally:
            self.in_transaction = False

class TcpClient(RefCount, ModbusTcpClient):
    method = 'tcp'

class UdpClient(RefCount, ModbusUdpClient):
    method = 'udp'

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, t):
        self._timeout = t
        if self.socket:
            self.socket.settimeout(t)

class SerialClient(RefCount, ModbusSerialClient):
    def __init__(self, *args, method = None, **kwargs):
        if method == "rtu":
            framer = ModbusRtuFramer
        elif method == "ascii":
            framer = ModbusAsciiFramer
        else:
            raise ValueError("RTU or ASCII only")
        self.method = method
        super().__init__(*args, framer=framer, **kwargs)
        self.lock = threading.RLock()

    @property
    def timeout(self):
        return self.params.timeout

    @timeout.setter
    def timeout(self, t):
        self.params.timeout = t

    def put(self):
        super().put()
        if self.refcount == 0:
            del serial_ports[os.path.basename(self.params.port)]

    def execute(self, request=None):
        with self.lock:
            return super().execute(request)

    def __enter__(self):
        self.lock.acquire()
        return super().__enter__()

    def __exit__(self, *args):
        super().__exit__(*args)
        self.lock.release()

serial_ports = {}

def make_client(m):
    if m.method == 'tcp':
        return TcpClient(m.target, port=m.port)

    if m.method == 'udp':
        return UdpClient(m.target, port=m.port)

    tty = m.target

    if tty in serial_ports:
        client = serial_ports[tty]
        if client.baudrate != m.rate:
            raise Exception('rate mismatch on %s' % tty)
        return client.get()

    dev = '/dev/%s' % tty
    client = SerialClient(port=dev, baudrate=m.rate, method=m.method)
    if not client.connect():
        client.put()
        return None

    serial_ports[tty] = client

    # send some harmless messages to the broadcast address to
    # let rate detection in devices adapt
    packet = bytes([0x00, 0x08, 0x00, 0x00, 0x55, 0x55])
    packet += struct.pack('>H', computeCRC(packet))

    for i in range(12):
        client.socket.write(packet)
        time.sleep(0.1)

    return client

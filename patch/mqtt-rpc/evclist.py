import logging
import socket
import struct
from urllib.parse import urlunparse
from time import monotonic
import dbus
from dnslib.dns import DNSRecord, DNSQuestion, QTYPE
from pymodbus.client.sync import ModbusTcpClient as ModbusClient
from pymodbus.register_read_message import ReadHoldingRegistersResponse
from lxml import etree as ElementTree
from dbusutil import dbusConnection
from streamcommand import BaseCommand

MDNS_IP = '224.0.0.251'
MDNS_PORT = 5353
svc = '_victron-car-charger._tcp.local.'

logger = logging.getLogger(__name__)

REGISTERS = (
	(5000, 1, 'id', lambda x: '{:X}'.format(x[0])),
	(5001, 6, 'serial', lambda x: struct.pack('<6H', *x).rstrip(b'\0').decode('utf-8')),
	(5007, 2, 'versionAsDecimal', lambda x: x[0]<<16 | x[1]),
	(5027, 22, 'customname', lambda x: struct.pack('<22H', *x).rstrip(b'\0').decode('utf-8'))
)

def text_version(x):
	if x & 0xFF == 0xFF:
		return '{:x}.{:02x}'.format(x >> 16, (x >> 8) & 0xFF)
	return '{:x}.{:02x}~{:x}'.format(x >> 16, (x >> 8) & 0xFF, x & 0xFF)

def mreqn(maddr):
	return struct.pack("4sii", socket.inet_aton(maddr), socket.INADDR_ANY, 0)

def parse_record(rec):
	ptr = []
	srv = {}
	for rr in rec.auth + rec.rr + rec.ar:
		rname = str(rr.rname)
		if rr.rtype == QTYPE.PTR:
			if rname == svc:
				ptr.append(str(rr.rdata.label))
		if rr.rtype == QTYPE.SRV:
			srv[rname] = str(rr.rdata.target)
	return [srv[p] for p in ptr if p in srv]


def mdns_lookup_evchargers():
	s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	s.bind(('', MDNS_PORT))
	s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreqn(MDNS_IP))
	s.settimeout(2)

	q = DNSRecord()
	q.add_question(DNSQuestion(svc, QTYPE.PTR))
	s.sendto(q.pack(), (MDNS_IP, MDNS_PORT))

	li = []
	start = monotonic()
	try:
		while (monotonic() - start < 3) and (reply := s.recv(65536)):
			li.extend(parse_record(DNSRecord.parse(reply)))
	except socket.timeout:
		pass
	return [(h, 502, 1) for h in li] # EV-chargers usually on unitid=1

def localsettings_lookup_evchargers():
	dbusSession = dbusConnection()
	li = []
	try:
		devices = dbusSession.call_blocking('com.victronenergy.settings',
			'/Settings/ModbusClient/tcp/Devices', None, 'GetValue', '', [])
	except dbus.exceptions.DBusException:
		logger.info('Cannot get modbus tcp devices from localsettings')
	else:
		for d in devices.split(','):
			try:
				proto, host, port, unitid = d.split(':')
			except ValueError:
				continue
			else:
				if proto == 'tcp':
					li.append((host, int(port), int(unitid)))
	return li

def dbus_lookup_evchargers():
	dbusSession = dbusConnection()
	li = []
	for service in dbusSession.list_names():
		if not service.startswith("com.victronenergy.evcharger."): continue
		try:
			connection = dbusSession.call_blocking(service,
				'/Mgmt/Connection', None, 'GetValue', '', [])
		except dbus.exceptions.DBusException:
			logger.info('Cannot get connection details from evcharger service')
		else:
			if connection.startswith('Modbus TCP '):
				li.append((connection[11:], 502, 1))

	return li

def lookup_evchargers(fast=False):
	# Look for EV-chargers in 3 places. MDNS-query, localsettings, and on dbus.
	li = set()
	if not fast:
		li.update(mdns_lookup_evchargers())
		li.update(localsettings_lookup_evchargers())
	li.update(dbus_lookup_evchargers())

	# Connect to each and pull data directly from modbus
	chargers = {}
	for host, port, unitid in li:
		data = {'type': 'http', 'updatable': 'True'}
		try:
			modbus = ModbusClient(host, port)
			for register, size, target, tform in REGISTERS:
				r = modbus.read_holding_registers(register, size, unit=unitid)
				if isinstance(r, ReadHoldingRegistersResponse):
					data[target] = tform(r.registers)
				else:
					raise Exception(r)
		except:
			logger.exception("fetching modbus registers")
			continue # Next device
		else:
			data['connection'] = connection = urlunparse(('http',
				modbus.socket.getpeername()[0], 'firmware.bin', '', '', ''))
			data['version'] = text_version(data['versionAsDecimal'])
			data['versionAsDecimal'] = str(data['versionAsDecimal'])

			# Only if we know the productid, just in case some other device
			# has something at register 5000.
			if data['id'] in ('C023', 'C024', 'C025', 'C026', 'C027'):
				chargers[connection] = data
		finally:
			modbus.close()

	return list(chargers.values())

class EvcScanCommand(BaseCommand):
	""" Wraps EV scan, for parallel scanning with other tasks. """
	def __init__(self):
		super().__init__()

	def _element(self, li):
		for dev in li:
			el = ElementTree.Element('device')
			for k, v in dev.items():
				el.set(k, v)
			yield el

	def start(self):
		try:
			evcs = lookup_evchargers()
		except Exception:
			logger.exception("lookup_evchargers")
		else:
			return None, list(self._element(evcs))

if __name__ == "__main__":
	from pprint import pprint
	pprint (lookup_evchargers())

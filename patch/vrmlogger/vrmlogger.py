#!/usr/bin/python3 -u
# -*- coding: utf-8 -*-

## @package dbus_vrm
# Run this with:
#	export PYTHONPATH="../velib_python"; python vrmlogger.py -d

# TODO: make some script that puts values on dbus which makes it easy to test the different whentolog options
# And see more todo's in code.

from dbus.mainloop.glib import DBusGMainLoop
import argparse
import logging
import datetime
import dbus
from enum import IntEnum
import os
import os.path
import signal
import sys
import time
from random import randint
from functools import partial

logger = logging.getLogger(__name__)

# Victron imports
sys.path.insert(1, os.path.join(os.path.dirname(__file__), './ext/velib_python'))
from gi.repository import GLib
import datalist
from vedbus import VeDbusService
from vedbus import VeDbusItemImport
from vrmhttp import VrmCommandType
from http_endpoint import HttpEndpoint, NullEndpoint
from settingsdevice import SettingsDevice
from monitor import DbusMonitor
from kwhdeltas import KwhDeltas
import hungprocs
import filemonitor
from ve_utils import exit_on_error
from ve_utils import get_free_space
from ve_utils import get_machine_name
from utils import exit_on_error_d, get_product_id
import constants

softwareversion = '2.390'

# contains the gobject timer source id for the hourly timer
hourlytimer = None

# contains the gobject timer source id for the interval timer
senddataintervaltimer = None

# our own representation on the dbus
dbusservice = None

# this is where all the data is going
endpoint = NullEndpoint()

# KwhDeltas instance
kwhdeltas = None

buffer = None

# For vrmlogger it only matters if VRM is completely disabled.
class VrmPortal(IntEnum):
	Off = 0
	ReadOnly = 1
	Full = 2

class State(object):
	__slots__ = ['changedValues', 'settings', 'rtt']
	def __init__(self):
		self.changedValues = None
		self.settings = None
		self.rtt = 0

def get_announce():
	try:
		# one dir up from the dir where this script is, we expect a file called version
		fileObject = open(os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, 'version')), 'r')
		lines = fileObject.readlines()
		version = lines[0].rstrip('\n')
		build = lines[2].rstrip('\n')
		fileObject.close()
	except Exception as ex:
		logger.error('get_announce() => exception while reading ccgx-version information: %s. We expect file ../version' % ex)
		version = '?'
		build = '?'

	try:
		with open('/tmp/last_boot_type', 'r+') as fileObject:
			line = fileObject.readline()
			boot_type = line.rstrip('\n')
			if boot_type != '-3':
				fileObject.seek(0,0)
				fileObject.truncate()
				fileObject.write('-3\n')  # Write to the file that we have already read it.
	except Exception as ex:
		logger.error('get_announce() => exception while opening /tmp/last_boot_type: %s.' % ex)
		boot_type = '-2'

	try:
		with open('/data/venus/serial-number', 'r') as fileObject:
			serial = fileObject.readline().strip()
	except IOError:
		serial = None

	# System uptime when vrmlogger starts
	uptime = None
	try:
		with open('/proc/uptime', 'r') as fp:
			uptime = int(float(fp.readline().strip().split()[0]))
	except Exception:
		logger.error('get_announce() => Could not determine uptime')


	free_space_rootfs = get_free_space("/")
	free_space_data = get_free_space("/data")

	# Definition of capabilities
	#    0b00000000000001   has-mqtt-rpc (used by VRM to hide or show the Remote Fw update & Remote VEConfigure options)
	#    0b00000000000010   mqtt-rpc has the firmware-update command (used by VRM to switch between old and new fw
	#                       update command)
	#    0b00000000000100   has-vnc-ssh-authentication (used by VRM Remote Console to switch between single ssh & two
	#                       ssh tunnel-mode).
	#    0b00000000001000   has-vreglink (used by VictronConnect to know if it supports mqtt-rpc & friends).
	#    0b00000000010000   Multiple generator start-stop services is supported. Generic- and specific types are separate
	#                       services.
	#    0b00000000100000   Accumulated generator runtime is available on the Tga attribute.
	#    0b00000001000000   Has DynamicESS support
	#    0b00000010000000   has-gui-v2-webassembly (and vrm can show that in its menus)
	#    0b00000100000000   Needs active MQTT topic refreshing with keep-alives instead of using retained messages.
	#    0b00001000000000   Supports dynamic SSH permitopen directives for proxy target in vnctunnel's authorized_keys.
	#    0b00010000000000   Uses new vrm config data attributes, part of 2024 security project
	#    0b00100000000000   Supports vrm EV integrations
	#    0b01000000000000   gui-v2-webassembly can accept command-line arguments (older versions would crash)
	#    0b10000000000000   Can do firmware updates on external devices in parallel
	cp = 0b11111101111111

	# Add GUI-v2 capability if we have the wasm
	cp |= (os.path.exists('/opt/venus/var/www/venus/gui-v2/venus-gui-v2.wasm.gz') << 7)

	announce = {'v': version, 'build': build, 'w': boot_type, 'fr': free_space_rootfs, 'fd': free_space_data,
		'cp': cp}

	announce.update({'mi': get_product_id()})
	name = get_machine_name()
	if name is not None:
		announce.update({'mn': name})
	if serial is not None:
		announce.update({'ms': serial})
	if uptime is not None:
		announce.update({'mu': uptime})

	return announce


def get_configchange():
	a = dbusMonitor.get_values(['configChange'])

	# Get the ipaddesses, but don't make ourselves dependent on connman, hence the try/except.
	addresses = ""
	try:
		manager = dbus.Interface(dbusservice.dbusconn.get_object("net.connman", "/"), "net.connman.Manager")
		for path, properties in manager.GetServices():
			if "IPv4" in properties:
				ipv4 = properties["IPv4"]
				if "Address" in ipv4:
					addresses += ipv4["Address"] + ";"
			if "IPv6" in properties:
				ipv6 = properties["IPv6"]
				if "Address" in ipv6:
					addresses += ipv6["Address"] + ";"
	except Exception as ex:
		logger.error("Failed to get local ip addresses; %s" % ex)

	a['ip'] = addresses.rstrip(";")

	return a

def get_priorityConfigChange():
	b = dbusMonitor.get_values(['priorityConfigChange'])
	return b

def calculate_rtt(load, rtt):
	""" This is an exponentially decaying function for working out the average
	    rount-trip time over some window similar to what you'd use for load
	    averages. Here we use it to get a clear picture of the round trip time
	    of messages over dbus. For the chosen values you get a 4-minute picture
	    when using 10-second spaced probes."""
	return int(load * 0.8 + rtt * 0.2)

#	dbus-servicename, for example com.victronenergy.dbus.ttyO1
#	dbus-path, for example /Ac/ActiveIn/L1/V
#   options: the dict containing the properties from the vrmTree
#	changes: the changes, a tuple with GetText() and GetValue()
#	deviceInstance: the device instance.
def value_changed_on_dbus(state, dbusServiceName, dbusPath, options, changes, deviceInstance):
	convert = options.get('convert', None)
	value = changes['Value'] if convert is None else convert(changes['Value'],
		changes['Text'], dbusMonitor.get_service(dbusServiceName))
	if options['whenToLog'] in ['configChange', 'priorityConfigChange']:
		if changes['Value'] is not None:
			priority = VrmCommandType.CONFIGCHANGE
			if options['whenToLog'] == 'priorityConfigChange':
				priority = VrmCommandType.PRIORITY_CONFIGCHANGE
			send_config({
				options['code'] + '[' + str(deviceInstance) + ']': value}, priority)

	elif options['whenToLog'] == 'onIntervalAlwaysAndOnEvent':
		send_data(state)

	elif options['whenToLog'] == 'onIntervalOnlyWhenChanged':
		# TODO regarding below None exception: does it make sense to send an empty string when a value is
		# being invalidated, instead of just sending nothing? We cannot guarantee that sending an empty
		# string is always sent. CCGX could be switched of at an unfortunate time for example. Or a device
		# is disconnected completely. One reason why it would make sense is to deleted it from the
		# cache in table lastLogData (?) while the rest of the system keeps running and sending data.
		# Anyway, at this moment the vrm db api can probably not cope with empty strings (?), and sending an
		# empty string will not trigger a delete from lastLogData. Therefore for now just hide it, to prevent
		# raising an error (unsupported operand type(s) for /: 'NoneType' and 'int')

		# Most common situation for values, type onIntervalOnlyWhenChanged, coming in as None, is when a
		# device is being disconnected: the D-Bus service then invalidates all values before going offline.
		if value is not None:
			state.changedValues[options['code'] + '[' + str(deviceInstance) + ']'] = value

	# If the AC-input config, or position of a PV-inverter changes,
	# we need to tell kwhdeltas to put the PV-inverters, which depends
	# on this, in the right block.
	if options['code'] in ('si1', 'si2', 'pL'):
		kwhdeltas.handle_acinput_config_changed()

# Called from the dbusmonitor
def handle_new_service(state, servicename, deviceinstance):
	kwhdeltas.handle_new_service(servicename, deviceinstance)
	send_config(get_priorityConfigChange(), VrmCommandType.PRIORITY_CONFIGCHANGE)
	send_config(get_configchange(), VrmCommandType.CONFIGCHANGE)

	# Add all values that are normally only sent on change
	state.changedValues.update(dbusMonitor.get_values(['onIntervalOnlyWhenChanged']))
	send_data(state)

# Called from the dbusmonitor
def handle_removed_service(servicename, deviceinstance):
	kwhdeltas.handle_removed_service(servicename, deviceinstance)
	send_config(get_priorityConfigChange(), VrmCommandType.PRIORITY_CONFIGCHANGE)
	send_config(get_configchange(), VrmCommandType.CONFIGCHANGE)

# Often there are several of configChange value changes that follow each other.
# Therefore don't send a configmessage on the first change, but wait a few more
# seconds (5) to get the rest in as well.
class SendConfig(object):
	""" Wrap and hold state for configChange values to VRM. """
	def __init__(self, timeout):
		self.timeout = timeout
		self.timer = None
		self.data = {}
		self.data[VrmCommandType.CONFIGCHANGE] = {}
		self.data[VrmCommandType.PRIORITY_CONFIGCHANGE] = {}

	def immediately(self, data, configType):
		""" Skip the timer and log immediately. """
		self.data[configType].update(data)
		self.callback()

	def __call__(self, data, configType):
		self.data[configType].update(data)
		if self.timer is None:
			self.timer = GLib.timeout_add(self.timeout, self.callback)

	@exit_on_error_d
	def callback(self):
		if self.timer:
			GLib.source_remove(self.timer)
			self.timer = None
		if self.data[VrmCommandType.PRIORITY_CONFIGCHANGE]:
			endpoint.send(VrmCommandType.PRIORITY_CONFIGCHANGE, self.data[VrmCommandType.PRIORITY_CONFIGCHANGE])
		if self.data[VrmCommandType.CONFIGCHANGE]:
			endpoint.send(VrmCommandType.CONFIGCHANGE, self.data[VrmCommandType.CONFIGCHANGE])
		for d in self.data.values():
			d.clear()
		return False # run once only
send_config = SendConfig(5000)

class AddRoReason(object):
	def __init__(self):
		self.data_partition_last_polled = 0
		self.data_partition_state_previous = None

	@staticmethod
	def text_to_number(inp):
		if inp == "fine":
			return "0"
		if inp == "failed-once":
			return "1"
		if inp == "recovered":
			return "2"
		if inp == "failed":
			return "3"
		if inp == "failed-to-mount":
			return "4"

		return "99"

	def __call__(self, data):
		"""Decided to poll instead of inotify, to not introduce (platform dependent) dependencies.
		And polling takes less memory.

		This works in conjunction with the init script that uses curl to post. The primary reason is that
		the state will change to 'recovered' at runtime.
		"""
		STATE_FILE="/run/data-partition-state"
		try:
			now = int(time.time())
			if self.data_partition_last_polled < now - 60:
				self.data_partition_last_polled = now
				with open(STATE_FILE, "r") as f:
					data_partition_state = self.text_to_number(f.read().strip())
					if data_partition_state != self.data_partition_state_previous:
						self.data_partition_state_previous = data_partition_state
						data.update({"dps": data_partition_state })
		except Exception:
			pass
add_ro_reason = AddRoReason()

def gps_data(monitor):
	gps = monitor.get_value('com.victronenergy.system', '/GpsService')
	if gps is not None:
		items = ((code + '[0]', monitor.get_value(gps, path), precision)
			for code, path, precision in (('lt', '/Position/Latitude', 5),
				('lg', '/Position/Longitude', 5), ('lc', '/Course', 0),
				('ls', '/Speed', 2), ('la', '/Altitude', 1)))
		return { k: round(v, p) for k, v, p in items if v is not None }
	return {}

def send_data(state, **kwargs):
	global senddataintervaltimer

	if state.settings['vrmportal'] != VrmPortal.Off and not senddataintervaltimer:
		senddataintervaltimer = GLib.timeout_add(
			state.settings['interval'] * 1000, exit_on_error, partial(send_data, state))

	data = dbusMonitor.get_values(['onIntervalAlwaysAndOnEvent', 'onIntervalAlways'])
	data.update(state.changedValues)
	state.changedValues.clear()

	data.update(kwhdeltas.getdeltas(refreshbaseline=False))

	data.update({'rtt': state.rtt, 'hp': hungprocs.hung_count, 'zp': hungprocs.zombie_count})
	add_ro_reason(data)

	# GPS data
	data.update(gps_data(dbusMonitor))

	# Add additional data we are requested to send
	data.update(**kwargs)

	endpoint.send(VrmCommandType.SENDDATA, data)

	# Making sure the interval timer keeps running
	return True


def send_hourlydeltas():
	global hourlytimer

	data = kwhdeltas.getdeltas(refreshbaseline=True)

	# Since all gateways out there will send their hourly update, randomly delay sending the data a
	# little, so that the load on the vrm-backend server is distributed a little.
	# The lamda is used to make sure that the time_out is stopped after one call
	GLib.timeout_add(
		randint(0, 120000), exit_on_error,
		lambda: endpoint.send(VrmCommandType.HOURLYDELTAS, data) and False)

	GLib.source_remove(hourlytimer)
	hourlytimer = GLib.timeout_add(seconds_until_next_kwh_delta_upload() * 1000, exit_on_error, send_hourlydeltas)


def gettext_fromtimestamp(path, value):
	return datetime.datetime.fromtimestamp(int(value)).strftime('%Y-%m-%d %H:%M:%S')


def flush_back_log(path, value):
	logger.warning('No longer implemented')

	# By returning False we prevent the value from changing, allowing it to be set over and over.
	return False


def handle_changed_setting(state, setting, oldvalue, newvalue):
	global endpoint
	global senddataintervaltimer

	if setting == 'vrmportal':
		if newvalue == VrmPortal.Off:
			logger.debug("Someone disabled the logger: stop")
			stop_logging()

		if oldvalue == VrmPortal.Off:
			logger.debug("Someone enabled the logger: start")
			start_logging(state)

	elif setting == 'interval':
		stop_logging()
		start_logging(state, False)

	elif setting == 'url' or setting == 'httpsenabled':
		if state.settings['vrmportal'] != VrmPortal.Off:
			stop_logging()
			start_logging(state, False)

	elif setting == 'externalstoragedir':
		logger.debug("ExternalStorageDir stored, set to {0}".format(state.settings['externalstoragedir']))

	elif setting == 'ramdiskmode':
		logger.debug("Ram disk mode setting changed from {0} to {1}".format(oldvalue, newvalue))

	else:
		raise Exception("ERROR: SOME SETTINGS CONDITION CHANGED THAT WE DON'T KNOW? (%s, %s)" % (setting, newvalue))


def stop_logging():
	global senddataintervaltimer
	assert senddataintervaltimer is not None, \
		"Stopping to log, so asserted that senddataintervaltimer is not None"
	GLib.source_remove(senddataintervaltimer)
	senddataintervaltimer = None

	global hourlytimer
	assert hourlytimer is not None, \
		"Stopping to log, se asserted that hourlytimer is not None"
	GLib.source_remove(hourlytimer)
	hourlytimer = None

	global endpoint
	if hasattr(endpoint, 'stop'):
		endpoint.stop()  # blocking, it waits until the vrm worker is stopped. max time is same
						  # as vrm http timeout
	endpoint.release()
	endpoint = NullEndpoint()

	logger.info("logger stopped")


def start_logging(state, doinitialsend=True):
	global senddataintervaltimer
	assert senddataintervaltimer is None, \
		"Starting to log, so asserted that senddataintervaltimer is None"

	url = state.settings['url']
	if url == '':
		url = constants.logurl_http if not state.settings['httpsenabled'] else constants.logurl_https

	logger.info("Starting to log, vrmportal = %d, url = %s" % (state.settings['vrmportal'], url))

	global endpoint
	if state.settings['vrmportal'] != VrmPortal.Off:
		endpoint = HttpEndpoint(
			settings=state.settings,
			logurl=url,
			update_last_contact=lambda t: dbusservice.__setitem__('/Vrm/TimeLastContact', t),
			update_last_network_result=lambda c, t: (dbusservice.__setitem__(
				'/Vrm/ConnectionError', c), dbusservice.__setitem__(
				'/Vrm/ConnectionErrorMessage', t)),
			dbusconn=dbusMonitor.dbusConn,
			update_count_f=lambda c: dbusservice.__setitem__('/Buffer/Count', c),
			update_oldest_timestamp_f=lambda t: dbusservice.__setitem__('/Buffer/OldestTimestamp', t),
			update_mount_state_f=lambda c: dbusservice.__setitem__('/Storage/MountState', c),
			update_buffer_location_f=lambda c: dbusservice.__setitem__('/Buffer/Location', c),
			update_free_disk_space_f=lambda c: dbusservice.__setitem__('/Buffer/FreeDiskSpace', c),
			update_error_state_f=lambda c: dbusservice.__setitem__('/Buffer/ErrorState', c)
			)

	if doinitialsend:
		# we booted up, send the announce message.
		endpoint.send(VrmCommandType.ANNOUNCE, get_announce())

		# send a configchange
		send_config.immediately(get_priorityConfigChange(), VrmCommandType.PRIORITY_CONFIGCHANGE)
		send_config.immediately(get_configchange(), VrmCommandType.CONFIGCHANGE)

		# Add all values that are normally only sent on change
		state.changedValues.update(dbusMonitor.get_values(['onIntervalOnlyWhenChanged']))

	assert senddataintervaltimer is None, \
		"Starting to log, asserted that senddataintervaltimer is None"

	# Send data, which will automatically start the interval timer
	send_data(state)

	global hourlytimer
	assert hourlytimer is None, \
		"Starting to log, asserted that hourlytimer is None"
	hourlytimer = GLib.timeout_add(seconds_until_next_kwh_delta_upload() * 1000, exit_on_error, send_hourlydeltas)

def on_mount_state_changed(path, new_value):
	logger.info('{} - {}'.format(path, new_value))
	assert path == '/Storage/MountState'
	if new_value == 2:
		global endpoint
		# Ugly: from an object oriented point of view, we should add an eject function to all endpoint
		# classes, but it is storage specific, so we swallow the AttributeError instead.
		try:
			endpoint.eject()
		except AttributeError:
			pass

def on_event_injected(path, new_value):
	global endpoint
	# Events look like this: eventType\tseverity\tapp\tmessage
	# eventType: int (0 = Venus OS core, 1 = Venus OS app)
	# severity: int (0 = WARNING, 1 = CRITICAL, 2 = INFO)
	# app: string
	# message: string
	new_value = new_value.replace(r'\t', '\t') # easier string writes
	try:
		typ, severity, app, msg = new_value.split('\t', 3)
		typ = int(typ)
		severity = int(severity)
	except (AttributeError, ValueError):
		logger.exception("Failed to log event")
	else:
		endpoint.send(VrmCommandType.EVENT, {
			'type': typ,
			'severity': severity,
			'app': app,
			'message': msg
		})

	return False

def seconds_until_next_kwh_delta_upload():
	return 900 - (int(time.time()) % 900)

def sig_handler(mainloop, signum, frame):
	mainloop.quit()

def main():
	global dbusMonitor
	global dbusservice

	state = State()

	# dictionary, containing the values that are only logged on change, and
	# that have changed since the last log. It is emptied everytime the values
	# are sent.
	state.changedValues = {}

	# Argument parsing
	parser = argparse.ArgumentParser(
		description='vrmlogger v%s: communication to VRM Portal database' % softwareversion
	)

	parser.add_argument("-d", "--debug", help="set logging level to debug",
					action="store_true")

	args = parser.parse_args()

	# Init logging
	FORMAT = '%(threadName)s-%(module)s: %(message)s'

	logging.basicConfig(level=(logging.DEBUG if args.debug else logging.INFO), format=FORMAT)
	logging.info("%s v%s is starting up" % (__file__, softwareversion))
	logLevel = {0: 'NOTSET', 10: 'DEBUG', 20: 'INFO', 30: 'WARNING', 40: 'ERROR'}
	logging.info('Loglevel set to ' + logLevel[logging.getLogger().getEffectiveLevel()])

	# Have a mainloop, so we can send/receive asynchronous calls to and from dbus
	DBusGMainLoop(set_as_default=True)

	# Put ourselves onto the dbus as a service
	dbusservice = VeDbusService("com.victronenergy.logger", register=False)
	dbusservice.add_path('/Vrm/TimeLastContact', value=None,
							description="Unixtimestamp of last successfull contact with VRM DB API",
							writeable=False, gettextcallback=gettext_fromtimestamp)

	dbusservice.add_path('/Vrm/ConnectionError', value=None,
							description="Last error encountered when sending data to VRM",
							writeable=False)

	dbusservice.add_path('/Vrm/ConnectionErrorMessage', value=None,
							description="Exception text related to last error encountered when sending to VRM",
							writeable=False)

	dbusservice.add_path('/Buffer/Count', value=None,
							description="The number of entries in the sqlite buffer, internal or external",
							writeable=False)

	dbusservice.add_path('/Buffer/OldestTimestamp', value=None,
							description="Unixtimestamp of the oldest item pending in the buffer",
							writeable=False, gettextcallback=gettext_fromtimestamp)

	dbusservice.add_path('/Buffer/Location', value=0,
							description="Where the sqlite file is stored (0 = internal, 1 = being transferred to SD card/USB, 2 = SD card/USB)",
							writeable=False)

	dbusservice.add_path('/Buffer/FreeDiskSpace', value=0,
							description="Free disk space on the device where the sqlite file is stored",
							writeable=False)

	dbusservice.add_path('/Buffer/ErrorState', value=0,
							description="Error state of writing to sqlite file (0 = no error, 1 = OutOfSpace, IOError = 2, MountError = 3, UnknownError = 4",
							writeable=False)

	dbusservice.add_path('/Storage/MountState', value=0,
							description="State of external storage device (0 not mounted, 1 mounted, 2 unmount desired, 3 busy unmounting)",
							writeable=True, onchangecallback=on_mount_state_changed)

	dbusservice.add_path('/EventLogging/Inject', value=None,
							description="Sends an event to the VRM Event logs tab",
							writeable=True, onchangecallback=on_event_injected)
	dbusservice.register()

	global kwhdeltas
	kwhdeltas = KwhDeltas()

	# add the dbus-data that kwhdeltas need to datalist.vrmtree without overwriting it
	kwhtree = kwhdeltas.get_dbusmonitortree()
	for serviceclass, newpaths in kwhtree.items():
		if serviceclass in datalist.vrmtree:
			# the class is already in there, check if all paths are as well.
			existingpaths = datalist.vrmtree[serviceclass]
			for newpath, newoptions in newpaths.items():
				if newpath not in existingpaths:
					existingpaths[newpath] = newoptions
		else:
			# class is not yet there, simply add it
			datalist.vrmtree[serviceclass] = newpaths

	bus = dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()
	serviceName = 'com.victronenergy.settings'

	# link to com.victronenergy.settings, where our config is stored
	# FIXME: the VrmPortal should not be added here!
	state.settings = SettingsDevice(
		bus,
		supportedSettings={
			'vrmportal': ['/Settings/Network/VrmPortal', 2, 0, 2],
			'interval': ['/Settings/Vrmlogger/LogInterval', 900, 0, 0],
			'url': ['/Settings/Vrmlogger/Url', '', 0, 0],  # When empty, the default url will be used.
			'externalstoragedir': ['/Settings/Vrmlogger/ExternalStorageDir', '', 0, 0],
			'httpsenabled' : ['/Settings/Vrmlogger/HttpsEnabled', 1, 0, 1],
			'ramdiskmode' : ['/Settings/Vrmlogger/RamDiskMode', 0, 0, 1],
			},
		eventCallback=lambda *args: handle_changed_setting(state, *args))

	dbusMonitor = DbusMonitor(
		datalist.vrmtree,
		partial(value_changed_on_dbus, state),
		deviceAddedCallback=partial(handle_new_service, state),
		deviceRemovedCallback=handle_removed_service
	)

	# Measure RTT (ping round trip) to systemcalc and time the response.
	# TODO: Use monotonic time when we upgrade to python3.
	def _handle_pong(then, *args):
		# Maintain dbus response time.
		delta = max(0, int((datetime.datetime.now() - then).total_seconds() * 1e3))
		state.rtt = calculate_rtt(state.rtt, delta)

	def _send_ping():
		# Ideally we should call Ping on the .Peer interface, but that
		# is not available on Venus.
		dbusMonitor.dbusConn.call_async('org.freedesktop.DBus', '/',
			'org.freedesktop.DBus', 'GetId', None, (),
			partial(_handle_pong, datetime.datetime.now()), None)
		return True

	GLib.timeout_add(10000, _send_ping)

	kwhdeltas.start(dbusMonitor)

	# Replace the old default url with a better default value: empty string
	if state.settings['url'] == 'http://vrm.victronenergy.com/log/log.php':
		state.settings['url'] = ''

	if state.settings['vrmportal'] != VrmPortal.Off:
		start_logging(state)
	else:
		logger.info('Init finished, but not starting: vrmportal = %s' % state.settings['vrmportal'])

	# Monitor hung processes
	hungprocs.start()

	# Monitor files, pass send_data across so it can call it when a file changes.
	# For any other files that need monitoring, add to filemonitor module.
	filemonitor.start(partial(send_data, state))

	# Start and run the mainloop
	logger.info("Starting mainloop, responding on only events")
	mainloop = GLib.MainLoop()
	signal.signal(signal.SIGTERM, partial(sig_handler, mainloop))
	signal.signal(signal.SIGINT, partial(sig_handler, mainloop))
	mainloop.run()
	if state.settings['vrmportal'] != VrmPortal.Off:
		stop_logging()

if __name__ == "__main__":
	main()

# vim: noexpandtab:shiftwidth=4:tabstop=4:softtabstop=0

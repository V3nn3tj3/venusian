#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
import os
import subprocess
import re

def get_default_interface():
    try:
        output = subprocess.check_output("ip route show default", shell=True).decode()
        m = re.search(r"dev\s+(\S+)", output)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "eth0"

def get_interface_info(iface):
    ip_addr = "127.0.0.1"
    netmask = "255.255.255.0"
    mac_addr = "00:11:22:33:44:55"
    
    try:
        output = subprocess.check_output(f"ip -4 addr show dev {iface}", shell=True).decode()
        m = re.search(r"inet\s+([\d.]+)/(\d+)", output)
        if m:
            ip_addr = m.group(1)
            prefix = int(m.group(2))
            mask_bin = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
            netmask = ".".join(str((mask_bin >> i) & 0xFF) for i in (24, 16, 8, 0))
    except Exception:
        pass

    try:
        with open(f"/sys/class/net/{iface}/address", "r") as f:
            mac_addr = f.read().strip()
    except Exception:
        pass

    return ip_addr, netmask, mac_addr

def get_gateway():
    try:
        output = subprocess.check_output("ip route show default", shell=True).decode()
        m = re.search(r"default via\s+([\d.]+)", output)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "192.168.17.1"

def get_nameservers():
    servers = []
    try:
        with open("/etc/resolv.conf", "r") as f:
            for line in f:
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) > 1:
                        servers.append(parts[1])
    except Exception:
        pass
    return servers if servers else ["8.8.8.8"]


class ConnmanService(dbus.service.Object):
    def __init__(self, bus, path):
        super().__init__(bus, path)

    def _build_properties(self):
        # Dynamically fetch real network parameters
        iface = get_default_interface()
        ip_addr, netmask, mac_addr = get_interface_info(iface)
        gateway = get_gateway()
        nameservers = get_nameservers()

        # 1. IPv4 sub-dictionary
        ipv4_dict = dbus.Dictionary({
            "Address": dbus.String(ip_addr, variant_level=1), 
            "Method": dbus.String("dhcp", variant_level=1),
            "Netmask": dbus.String(netmask, variant_level=1),
            "Gateway": dbus.String(gateway, variant_level=1)
        }, signature='sv')

        # 2. Ethernet sub-dictionary
        ethernet_dict = dbus.Dictionary({
            "Interface": dbus.String("Wired", variant_level=1),
            "Address": dbus.String(mac_addr, variant_level=1),
            "MTU": dbus.UInt16(1500, variant_level=1),
            "Plugged": dbus.Boolean(True, variant_level=1),
            "Link": dbus.Boolean(True, variant_level=1)
        }, signature='sv')

        # 3. Main properties
        return dbus.Dictionary({
            "Type": dbus.String("ethernet", variant_level=1),
            "State": dbus.String("ready", variant_level=1),
            "Name": dbus.String("Wired", variant_level=1),
            "Favorite": dbus.Boolean(True, variant_level=1),
            "Immutable": dbus.Boolean(True, variant_level=1),
            "AutoConnect": dbus.Boolean(True, variant_level=1),
            "Interface": dbus.String(iface, variant_level=1),
            "Strength": dbus.Byte(100, variant_level=1),
            "Ethernet": dbus.Dictionary(ethernet_dict, signature='sv', variant_level=1),
            "IPv4": dbus.Dictionary(ipv4_dict, signature='sv', variant_level=1),
            "IPv4.Configuration": dbus.Dictionary(ipv4_dict, signature='sv', variant_level=1),
            "Nameservers": dbus.Array(nameservers, signature='s', variant_level=1),
            "Nameservers.Configuration": dbus.Array(nameservers, signature='s', variant_level=1)
        }, signature='sv')

    @dbus.service.method('net.connman.Service', in_signature='', out_signature='a{sv}')
    def GetProperties(self):
        return self._build_properties()

    @dbus.service.method('org.freedesktop.DBus.Properties', in_signature='ss', out_signature='v')
    def Get(self, interface_name, property_name):
        props = self._build_properties()
        if property_name in props:
            return props[property_name]
        raise dbus.exceptions.DBusException('org.freedesktop.DBus.Error.InvalidArgs')

    @dbus.service.method('org.freedesktop.DBus.Properties', in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface_name):
        if interface_name in ['net.connman.Service', '']:
            return self._build_properties()
        return dbus.Dictionary({}, signature='sv')


class ConnmanManager(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, '/')

    @dbus.service.method('net.connman.Manager', in_signature='', out_signature='a{sv}')
    def GetProperties(self):
        return dbus.Dictionary({
            "State": dbus.String("ready", variant_level=1),
            "OfflineMode": dbus.Boolean(False, variant_level=1)
        }, signature='sv')

    @dbus.service.method('net.connman.Manager', in_signature='', out_signature='a(oa{sv})')
    def GetServices(self):
        path = dbus.ObjectPath('/net/connman/service/ethernet_wired')
        props = service._build_properties()
        service_struct = dbus.Struct((path, props), signature='oa{sv}')
        return dbus.Array([service_struct], signature='(oa{sv})')

    @dbus.service.method('net.connman.Manager', in_signature='', out_signature='a(oa{sv})')
    def GetTechnologies(self):
        tech_props = dbus.Dictionary({
            "Type": dbus.String("ethernet", variant_level=1), 
            "Name": dbus.String("Ethernet", variant_level=1), 
            "Powered": dbus.Boolean(True, variant_level=1), 
            "Connected": dbus.Boolean(True, variant_level=1)
        }, signature='sv')
        tech_struct = dbus.Struct((dbus.ObjectPath('/net/connman/technology/ethernet'), tech_props), signature='oa{sv}')
        return dbus.Array([tech_struct], signature='(oa{sv})')


if __name__ == '__main__':
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    
    os.system("killall -9 connman_mock 2>/dev/null")
    
    service = ConnmanService(bus, '/net/connman/service/ethernet_wired')
    manager = ConnmanManager(bus)
    
    try:
        bus.request_name('net.connman', dbus.bus.NAME_FLAG_REPLACE_EXISTING)
    except:
        pass
        
    loop = GLib.MainLoop()
    loop.run()
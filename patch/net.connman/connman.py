#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
import os

class ConnmanService(dbus.service.Object):
    def __init__(self, bus, path):
        super().__init__(bus, path)

    def _build_properties(self):
        # 1. IPv4 sub-dictionary conform de werkende C++ parameters
        ipv4_dict = dbus.Dictionary({
            "Address": dbus.String("192.168.17.136", variant_level=1), 
            "Method": dbus.String("dhcp", variant_level=1),
            "Netmask": dbus.String("255.255.255.0", variant_level=1),
            "Gateway": dbus.String("192.168.17.1", variant_level=1)
        }, signature='sv')

        # 2. Ethernet sub-dictionary conform de werkende C++ parameters
        ethernet_dict = dbus.Dictionary({
            "Interface": dbus.String("Wired", variant_level=1),
            "Address": dbus.String("00:11:22:33:44:55", variant_level=1),
            "MTU": dbus.UInt16(1500, variant_level=1),
            "Plugged": dbus.Boolean(True, variant_level=1),
            "Link": dbus.Boolean(True, variant_level=1)
        }, signature='sv')

        # 3. Hoofd-properties met de magische "Wired" naamstelling
        return dbus.Dictionary({
            "Type": dbus.String("ethernet", variant_level=1),
            "State": dbus.String("ready", variant_level=1),
            "Name": dbus.String("Wired", variant_level=1), # FIX: Matcht nu 1-op-1 met de werkende C++ code!
            "Favorite": dbus.Boolean(True, variant_level=1),
            "Immutable": dbus.Boolean(True, variant_level=1),
            "AutoConnect": dbus.Boolean(True, variant_level=1),
            "Interface": dbus.String("eth0", variant_level=1),
            "Strength": dbus.Byte(100, variant_level=1),
            "Ethernet": dbus.Dictionary(ethernet_dict, signature='sv', variant_level=1),
            "IPv4": dbus.Dictionary(ipv4_dict, signature='sv', variant_level=1),
            "IPv4.Configuration": dbus.Dictionary(ipv4_dict, signature='sv', variant_level=1),
	    "Nameservers": dbus.Array(["8.8.8.8"], signature='s', variant_level=1),
            "Nameservers.Configuration": dbus.Array(["8.8.8.8"], signature='s', variant_level=1)
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
        # FIX: ObjectPath hernoemd naar het exact werkende wired pad!
        path = dbus.ObjectPath('/net/connman/service/ethernet_wired')
        props = service._build_properties()
        
        # We dwingen de loepzuivere C-handtekening 'a(oa{sv})' af op bus-niveau
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
    
    # Ruim eventuele actieve achtergrond-processen op van de C++ mock
    os.system("killall -9 connman_mock 2>/dev/null")
    
    # Registreer de objecten op exact de juiste wired paden
    service = ConnmanService(bus, '/net/connman/service/ethernet_wired')
    manager = ConnmanManager(bus)
    
    try:
        bus.request_name('net.connman', dbus.bus.NAME_FLAG_REPLACE_EXISTING)
    except:
        pass
        
    loop = GLib.MainLoop()
    loop.run()

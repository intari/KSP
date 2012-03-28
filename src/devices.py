import logging, uuid

import devices_db, certificate
from devices_db import DeviceInfo


def _update(device, ip_address = None, cookie = None):
	if ip_address == device.last_ip \
			and (not cookie or cookie == device.last_cookie or cookie.startswith(device.last_cookie)):
			# only update of any of them changed
		return device
	device.last_ip = ip_address
	if fiona_id:
		device.fiona_id = fiona_id
	if cookie:
		device.last_cookie = cookie[:64]
	if p12:
		device.p12 = p12
	devices_db.update(device)
	return device

def _make_context(device):
	if not device.p12:
		device.p12 = certificate.load_p12(device.serial)
	device.context = certificate.make_context(device.serial, device.p12)
	if not device.context:
		device.mark_context_failed()
		return False
	return True

def get(serial):
	"""gets a device by serial"""
	global _devices
	return _devices.get(serial)

def update(device, fiona_id = None, pkcs12_bytes = None):
	if fiona_id:
		device.fiona_id = fiona_id
	if pkcs12_bytes:
		device.p12 = pkcs12_bytes
	devices_db.update(device)

def detect(ip_address, cookie = None):
	"""
	guess the device that made a request
	if no matching device exists in our database, one may be created on the spot
	"""
	global _devices
	for d in _devices.values():
		if (cookie and (cookie == d.last_cookie or cookie.startswith(d.last_cookie))) \
				or ip_address == d.last_ip: # match from most specific to less specific field
			if d.context_failed():
				# let's give it another chance... maybe the user put the proper .p12 into place
				if not _make_context(d):
					return d
				devices_db.insert(d)
			return _update(d, ip_address, cookie)
	# create new provisional device
	d = DeviceInfo(last_ip = ip_address, last_cookie = cookie)
	_devices[d.serial] = d
	return d

def confirm_device(device, serial):
	"""we've found the serial of a provisional device"""
	if not device.is_provisional():
		raise Exception("tried to confirm an already known device, wtf", device, serial)

	# first we check if the device has been previously seen, but we just could not identify it
	# (for example, the device might connect from a different IP and may have changed its cookie in the meantime)
	already_registered = devices_db.find(serial)
	if already_registered:
		# yay, update ip and serial
		_update(already_registered, device.last_ip, device.last_cookie)
		return already_registered

	device.serial = serial
	if not _make_context(device):
		return None

	logging.warn("registered device %s", device)
	devices_db.insert(device)
	return device

def save_all():
	global _devices
	devices_db.update_all(_devices.values())


### module initialization
_devices = {}
for d in devices_db.load_all():
	_devices[d.serial] = d
	_make_context(d)
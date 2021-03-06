import logging
import xml.dom.minidom as minidom

from handlers.upstream import Upstream
from handlers.dummy import DummyResponse
from handlers.ksp import _servers_config, _first_contact
from handlers import is_uuid, TODO, TODO_PATH
import calibre, qxml
import config, features


def _rewrite_url(url):
	"""
	certain responses from the server contain urls pointing to amazon services
	we rewrite them to point to our proxy
	"""
	if url and config.rewrite_rules:
		for pattern, replacement in config.rewrite_rules.items():
			m = pattern.search(url)
			if m:
				url = url[:m.start()] + m.expand(replacement) + url[m.end():]
	return url

def _add_item(x_items, action, item_type, key = 'NONE', text = None, priority = 600, url = None, forced = False):
	item = qxml.add_child(x_items, 'item')
	item.setAttribute('action', str(action))
	item.setAttribute('is_incremental', 'false')
	item.setAttribute('key', str(key))
	item.setAttribute('priority', str(priority))
	item.setAttribute('sequence', '0')
	item.setAttribute('type', str(item_type))
	if url:
		item.setAttribute('url', url)
	if text:
		if forced:
			qxml.add_child(item, 'title', text)
			qxml.add_child(item, 'forced', 'true')
		else:
			qxml.set_text(item, text)
	return item

def _filter_item(x_items, x_item):
	action = x_item.getAttribute('action')
	item_type = x_item.getAttribute('type')

	if action == 'UPLOAD':
		if item_type in ['MESG', 'LOGS'] and not features.allow_logs_upload:
			x_items.removeChild(x_item)
			return True
		item_url = x_item.getAttribute('url')
		new_url = _rewrite_url(item_url)
		if new_url != item_url:
			logging.warn("rewrote url %s => %s", item_url, new_url)
			x_item.setAttribute('url', new_url)
			return True
		return False

	if action == 'DOWNLOAD':
		item_key = x_item.getAttribute('key')
		item_url = x_item.getAttribute('url')
		if item_url and (item_type == 'CRED' or is_uuid(item_key)):
			new_url = _rewrite_url(item_url)
			if new_url != item_url:
				logging.warn("rewrote url for %s: %s => %s", item_key, item_url, new_url)
				x_item.setAttribute('url', new_url)
				return True
		logging.warn("not rewriting url %s for %s", item_url, item_key)
		return False

	if action == 'GET':
		if item_type == 'FWUP' and not features.allow_firmware_updates:
			x_items.removeChild(x_items)
			return True

	if action == 'SND' and item_type == 'CMND':
		item_key = x_item.getAttribute('key')
		if item_key and item_key.endswith(':SYSLOG:UPLOAD') and not features.allow_logs_upload:
			# not sure if this is smart, ignoring these items appears to queue them up at amazon
			x_items.removeChild(x_item)
			return True

	# very unlikely for these to change upstream for books not downloaded from Amazon...
	# if action == 'UPD_ANOT' or action == 'UPD_LPRD':
	# 	# annotations and LPRD (last position read?)
	# 	item_key = x_item.getAttribute('key')
	# 	if is_uuid(item_key):
	# 		x_items.removeChild(x_item)
	# 		return True

	return False

def _process_xml(doc, device, reason):
	x_response = qxml.get_child(doc, 'response')
	x_items = qxml.get_child(x_response, 'items')
	if not x_items:
		return False

	was_updated = False

	# rewrite urls
	for x_item in qxml.list_children(x_items, 'item'):
		was_updated |= _filter_item(x_items, x_item)

	if features.download_updated_books:
		for book in calibre.books().values():
			if book.needs_update_on(device) and book.cde_content_type in ('EBOK', ): # PDOC updates are not supported ATM
				logging.warn("book %s updated in library, telling device %s to download it again", book, device)
				# <item action="GET" is_incremental="false" key="asin" priority="600" sequence="0" type="EBOK">title</item>
				_add_item(x_items, 'GET', book.cde_content_type, key = book.asin, text = book.title, forced = True) # book.title)
				was_updated = True

	while device.actions_queue:
		action = device.actions_queue.pop()
		# logging.debug("checking action %s", action)
		if list(qxml.filter(x_items, 'item', action = action[0], type = action[1])):
			# logging.debug("action %s already found in %s, skipping", action, x_items)
			continue
		if action == ('SET', 'SCFG'):
			_add_item(x_items, 'SET', 'SCFG', text = _servers_config(device), key = 'KSP.set.scfg', priority = 100)
			was_updated = True
		elif action == ('UPLOAD', 'SNAP'):
			_add_item(x_items, 'UPLOAD', 'SNAP', key = 'KSP.upload.snap', priority = 1000, url = config.server_url + 'FionaCDEServiceEngine/UploadSnapshot')
			was_updated = True
		# elif action == ('GET', 'NAMS'):
		# 	_add_item(x_items, 'GET', 'NAMS', key = 'NameChange' if device.is_kindle() else 'AliasChange')
		# 	was_updated = True
		elif action == ('UPLOAD', 'SCFG'):
			_add_item(x_items, 'UPLOAD', 'SCFG', key = 'KSP.upload.scfg', priority = 50, url = config.server_url + 'ksp/scfg')
			was_updated = True
		else:
			logging.warn("unknown action %s", action)

	if was_updated:
		x_total_count = qxml.get_child(x_response, 'total_count')
		qxml.set_text(x_total_count, len(x_items.childNodes))

	return was_updated


_POLL_RESPONSE = b'<?xml version="1.0" encoding="UTF-8"?><response><total_count>0</total_count><next_pull_time>0</next_pull_time></response>'

class TODO_GetItems (Upstream):
	def __init__(self):
		Upstream.__init__(self, TODO, TODO_PATH + 'getItems')

	def call(self, request, device):
		q = request.get_query_params()
		if q.get('reason') == 'Poll':
			return DummyResponse(data = _POLL_RESPONSE)

		if device.is_provisional():
			# tell the device to do a full snapshot upload, so that we can get the device serial and identify it
			return DummyResponse(headers = { 'Content-Type': 'text/xml;charset=UTF-8' }, data = _first_contact(device))

		lto = q.get('device_lto', -1)
		if lto != -1:
			try: device.lto = int(lto)
			except: pass

		response = self.call_upstream(request, device)
		if response.status == 200:
			# use default UTF-8 encoding
			with minidom.parseString(response.body_text()) as doc:
				if _process_xml(doc, device, q.get('reason')):
					xml = doc.toxml('UTF-8')
					response.update_body(xml)

		return response

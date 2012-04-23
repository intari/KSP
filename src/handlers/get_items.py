import logging
import xml.dom.minidom as minidom

from handlers.upstream import Upstream
from handlers.dummy import DummyResponse
from handlers import is_uuid, TODO, TODO_PATH, CDE_PATH, FIRS_PATH, DET_PATH
import calibre, qxml
import config, features


class TODO_GetItems (Upstream):
	_DUMMY_HEADERS = { 'Content-Type': 'text/xml;charset=UTF-8' }
	__DUMMY_STR = '''
			<?xml version="1.0" encoding="UTF-8"?>
			<response>
				<total_count>1</total_count>
				<items>
					<item action="UPLOAD" is_incremental="false" key="NONE" priority="1600" sequence="0" type="SNAP" url="$SERVER_URL$FionaCDEServiceEngine/UploadSnapshot"/>
				</items>
			</response>
	'''.replace('\t', '').replace('\n', '').replace('$SERVER_URL$', config.server_url)
	_DUMMY_BODY = bytes(__DUMMY_STR, 'UTF-8')

	def __init__(self):
		Upstream.__init__(self, TODO, TODO_PATH + 'getItems?', 'GET')

	def call(self, request, device):
		if device.is_provisional():
			# tell the device to do a full snapshot upload, so that we can get the device serial and identify it
			return DummyResponse(headers = self._DUMMY_HEADERS, data = self._DUMMY_BODY)

		response = self.call_upstream(request, device)
		if response.status == 200:
			# use default UTF-8 encoding
			with minidom.parseString(response.body) as doc:
				q = request.get_query_params()
				if self.process_xml(doc, device, q.get('reason')):
					xml = doc.toxml('UTF-8')
					response.update_body(xml)

		return response

	def process_xml(self, doc, device, reason):
		x_response = qxml.get_child(doc, 'response')
		x_items = qxml.get_child(x_response, 'items')
		if not x_items:
			return False

		was_updated = False

		# rewrite urls
		for x_item in qxml.iter_children(x_items, 'item'):
			was_updated |= self.filter_item(x_items, x_item)

		if features.download_updated_books:
			for book in calibre.books().values():
				if book.needs_update_on(device) and book.cde_content_type in ('EBOK', ): # PDOC updates are not supported ATM
					logging.warn("book %s updated in library, telling device %s to download it again", book, device)
					# <item action="GET" is_incremental="false" key="asin" priority="600" sequence="0" type="EBOK">title</item>
					self.add_item(x_items, 'GET', book.cde_content_type, key = book.asin, text = book.title, forced = True) # book.title)
					was_updated = True

		while device.actions_queue:
			action = device.actions_queue.pop()
			if action == ('SET', 'SCFG'):
				self.add_item(x_items, 'SET', 'SCFG', text = self._servers_config(), key = 'KSP.servers.configuration', priority = 100)
				device.configuration_updated = True
				was_updated = True
			else:
				logging.warn("unknown action %s", action)

		if was_updated:
			x_total_count = qxml.get_child(x_response, 'total_count')
			qxml.set_text(x_total_count, len(x_items.childNodes))

		return was_updated

	def add_item(self, x_items, action, item_type, key = 'NONE', text = None, priority = 600, url = None, forced = False):
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

	def filter_item(self, x_items, x_item):
		action = x_item.getAttribute('action')
		item_type = x_item.getAttribute('type')

		if action == 'UPLOAD':
			if item_type in ['MESG', 'LOGS'] and not features.allow_logs_upload:
				x_items.removeChild(x_item)
				return True
			item_url = x_item.getAttribute('url')
			new_url = self.rewrite_url(item_url)
			if new_url != item_url:
				logging.warn("rewrote url %s => %s", item_url, new_url)
				x_item.setAttribute('url', new_url)
				return True
			return False

		if action == 'DOWNLOAD':
			item_key = x_item.getAttribute('key')
			item_url = x_item.getAttribute('url')
			if item_url and (item_type == 'CRED' or is_uuid(item_key)):
				new_url = self.rewrite_url(item_url)
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
		# very unlikely for these to change upstream for books not downloaded from Amazon...
		# if action == 'UPD_ANOT' or action == 'UPD_LPRD':
		# 	# annotations and LPRD (last position read?)
		# 	item_key = x_item.getAttribute('key')
		# 	if is_uuid(item_key):
		# 		x_items.removeChild(x_item)
		# 		return True
		return False

	def _servers_config(self):
		servers_config = (
				'url.todo=' + config.server_url + TODO_PATH.strip('/'),
				'url.cde=' + config.server_url + CDE_PATH.strip('/'),
				'url.firs=' + config.server_url + FIRS_PATH.strip('/'),
				'url.firs.unauth=' + config.server_url + FIRS_PATH.strip('/'),
			)
		if not features.allow_logs_upload:
			servers_config += (
				'url.messaging.post=' + config.server_url,
				'url.det=' + config.server_url + DET_PATH.strip('/'),
				'url.det.unauth=' + config.server_url + DET_PATH.strip('/')
			)
		return '\n'.join(servers_config)

	def rewrite_url(self, url):
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

import os
import requests
import logging
import urllib3

#from common.models import InvenTreeSetting
from plugin import InvenTreePlugin
from plugin.mixins import EventMixin, SettingsMixin
from django.apps import apps

logger = logging.getLogger('inventree')

class QuickBarcode(SettingsMixin, EventMixin, InvenTreePlugin):
    NAME = "Quick Barcode"
    SLUG = "quickbarcode"
    TITLE = "Quick Barcode Linker"
    DESCRIPTION = "Automatically links serial as external barcode stockitems"
    VERSION = "1.0"
    AUTHOR = "Leo Schelvis"

    SETTINGS = {
        'LOG_LEVEL': {
            'name': 'log level',
            'description': 'Select log level',
            'choices': [('info','Info (only shows informational messages, warnings and errors)'),('warning', 'Warn (only shows warnings and errors)'),('debug','Debug (show all debugging messages)')],
            'default': 'warning',
            'validator': str,
        },
        'API_KEY': {
            'name': 'Your API key',
            'description': 'Your Inventree API key to link barcodes (may also be set in .env for docker setups. restart needed!)',
            'validator': str,
        },
        'API_URL': {
            'name': 'Your Inventree site url',
            'description': 'Your Inventree site url to link barcodes (may also be set in .env for docker setups. restart needed!)',
            'validator': str,
        }
    }

    def get_event_handlers(self):
        return {
            "plugins_loaded": self.load_settings,
            "stock_stockitem.created": self.process_stock_stockitem_saved,
            "stock_stockitem.saved": self.process_stock_stockitem_saved,
        }

    # not available in 0.17.13 can be used instead of event in future
    #def plugin_ready(self):
    #    self.load_settings()
    #    logger.debug(f"[QBC] plugin is ready - loading settings...")

    def load_settings(self, event):
        level = self.get_setting("LOG_LEVEL")
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        logger.debug(f"[QBC] logging started with level {level}")

    def process_event(self, event, *args, **kwargs):
        logger.debug(f"[QBC] Event empfangen: {event}, kwargs={kwargs}")

        handler = self.get_event_handlers().get(event)
        if handler:
            try:
                handler(event, *args, **kwargs)
                logger.debug(f"[QBC] Handled event: {event}, kwargs={kwargs} in function {handler}")
            except Exception as e:
                logger.error(f"[QBC] Error during handerling event '{event}': {e}")

    def process_stock_stockitem_saved(self, event, *args, **kwargs):
        StockItem = apps.get_model("stock", "StockItem")
        item_id = kwargs.get("id")
        if not item_id:
            return

        try:
            item = StockItem.objects.get(id=item_id)
        except StockItem.DoesNotExist:
            logger.warning(f"[QBC] StockItem with ID {item_id} does not exist anymore.")
            return

        logger.debug(f"[QBC] Info: {getattr(item, "barcode", "leeg")}")

        serial = getattr(item, "serial", None)
        if not serial:
            logger.warning(f"[QBC] No serial for StockItem {item_id}.")
            return

        api_token = self.get_setting("API_KEY")
        site_url = self.get_setting("API_URL")
        if not api_token or not site_url:
            logger.error("[QBC] API-TOKEN or SITE_URL not set.")
            return

        headers = {
            "Authorization": f"Token {api_token}",
            "Content-Type": "application/json",
        }
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        link_url = f"{site_url}/api/barcode/link/"
        payload = {"barcode": serial, "stockitem": item.pk}
        resp = requests.post(link_url, headers=headers, json=payload, verify=False)

        try:
            data = resp.json()
        except Exception:
            data = {}

        if resp.status_code in [200, 201] and "success" in data:
            logger.info(f"[QBC] Barcode '{serial}' linked to StockItem {item.pk}.")
        else:
            logger.error(f"[QBC] Error during linking barcode to StockItem: Status {resp.status_code} - {resp.text}")

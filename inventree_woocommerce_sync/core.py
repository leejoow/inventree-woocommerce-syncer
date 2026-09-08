"""Plugin entry point and event handling for WooCommerce order synchronization."""

import base64
import json
import logging
import re
from urllib.parse import urlencode

from plugin import InvenTreePlugin
from plugin.mixins import APICallMixin, EventMixin, SettingsMixin


logger = logging.getLogger(__name__)


class WooCommerceOrderSyncPlugin(
    APICallMixin, SettingsMixin, EventMixin, InvenTreePlugin
):
    """Prepare a WooCommerce update when an InvenTree sales order ships."""

    NAME = "WooCommerce Order Sync"
    SLUG = "woocommerce-order-sync"
    TITLE = "WooCommerce Order Sync"
    DESCRIPTION = "Triggers WooCommerce synchronization for shipped sales orders."
    VERSION = "0.1.2"
    AUTHOR = "Leo Schelvis"
    LICENSE = "MIT"

    SETTINGS = {
        "WOOCOMMERCE_URL": {
            "name": "WooCommerce REST API URL",
            "description": "Host and path, without https://",
            "default": "example.com/wp-json/wc/v3",
            "required": True,
        },
        "WOOCOMMERCE_CONSUMER_KEY": {
            "name": "WooCommerce consumer key",
            "protected": True,
            "required": True,
        },
        "WOOCOMMERCE_CONSUMER_SECRET": {
            "name": "WooCommerce consumer secret",
            "protected": True,
            "required": True,
        },
    }

    API_URL_SETTING = "WOOCOMMERCE_URL"
    API_TOKEN_SETTING = "WOOCOMMERCE_CONSUMER_KEY"
    ORDER_REFERENCE_PATTERN = re.compile(r"^SO-(\d+)$", re.IGNORECASE)

    SHIPMENT_COMPLETED_EVENT = "salesordershipment.completed"

    def __init__(self):
        """Initialize the plugin and report that it is ready."""
        super().__init__()

    @property
    def api_headers(self):
        """Return WooCommerce REST headers using consumer-key Basic Auth."""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "InvenTree-WooCommerce-Sync/0.1.0",
        }

        credentials = (
            f"{self.get_setting('WOOCOMMERCE_CONSUMER_KEY')}"
            f":{self.get_setting('WOOCOMMERCE_CONSUMER_SECRET')}"
        )
        encoded_credentials = base64.b64encode(
            credentials.encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {encoded_credentials}"
        return headers

    def wants_process_event(self, event):
        """Return whether this plugin should receive the event."""
        return event == self.SHIPMENT_COMPLETED_EVENT

    def process_event(self, event, *args, **kwargs):
        """Handle a completed sales order shipment in the background worker."""
        if not self.wants_process_event(event):
            return

        self.handle_shipped_order(*args, **kwargs)

    def handle_shipped_order(self, *args, **kwargs):
        """Mark the order as shipped and synchronize it with WooCommerce."""
        from order.models import SalesOrderShipment
        from order.status_codes import SalesOrderStatus

        shipment_id = kwargs.get("id")
        if shipment_id is None:
            logger.warning(
                "Sales order shipment event has no shipment id; kwargs=%s",
                kwargs,
            )
            return

        shipment = SalesOrderShipment.objects.get(pk=shipment_id)
        order = shipment.order

        if order.status not in (
            SalesOrderStatus.SHIPPED.value,
            SalesOrderStatus.COMPLETE.value,
        ):
            order.ship_order(user=shipment.checked_by)

        self.sync_order_to_woocommerce(order, shipment)

        logger.warning(
            "Sales order shipment completed; order=%s status=%s; "
            "WooCommerce sync completed",
            order.pk,
            order.status,
        )

    def sync_order_to_woocommerce(self, order, shipment):
        """Set the WooCommerce order status and shipment tracking metadata."""
        match = self.ORDER_REFERENCE_PATTERN.fullmatch(order.reference)
        if match is None:
            logger.error(
                "Cannot sync sales order %s: reference must match SO-<number>",
                order.reference,
            )
            return

        woocommerce_order_id = match.group(1)
        payload = {
            "status": "completed"
        }
        response = self.api_call(
            f"orders/{woocommerce_order_id}",
            method="PUT",
            json=payload,
            simple_response=False,
        )
        if response.status_code >= 400:
            logger.error(
                "WooCommerce rejected order %s update with HTTP %s: %s",
                woocommerce_order_id,
                response.status_code,
                response.text,
            )
            response.raise_for_status()
        logger.info(
            "WooCommerce order %s synchronized with tracking number %s",
            woocommerce_order_id,
            shipment.tracking_number or "",
        )
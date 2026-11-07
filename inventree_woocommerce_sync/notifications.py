"""InvenTree UI notifications for WooCommerce import warnings."""

import hashlib
import json
import logging
from typing import Iterable

from .mapping import MetadataWarning

logger = logging.getLogger(__name__)

METADATA_WARNING_CATEGORY = "woocommerce.metadata_warning"
UI_NOTIFICATION_DELIVERY = "inventree-ui-notification"


def notify_metadata_warnings(reference: str, warnings: Iterable[MetadataWarning]) -> None:
    """Notify all active users once per order and unsupported metadata value."""
    from django.contrib.auth import get_user_model
    from common.models import NotificationEntry
    from common.notifications import trigger_notification

    for warning in warnings:
        identity = json.dumps(
            [reference, warning.sku, warning.key, warning.value],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        uid = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        entries = NotificationEntry.objects.filter(
            key=METADATA_WARNING_CATEGORY, uid=uid
        )
        if entries.exists():
            continue

        targets = list(get_user_model().objects.filter(is_active=True))
        if not targets:
            logger.warning(
                "Could not send metadata notification for order %s: no active users",
                reference,
            )
            continue

        trigger_notification(
            None,
            category=METADATA_WARNING_CATEGORY,
            notification_uid=uid,
            check_recent=False,
            targets=targets,
            delivery_methods=[UI_NOTIFICATION_DELIVERY],
            context={
                "name": "WooCommerce metadata warning",
                "message": (
                    f"Order {reference}: WooCommerce SKU '{warning.sku}' has "
                    f"unsupported metadata '{warning.key}' value '{warning.value}'."
                ),
            },
        )
        if not entries.exists():
            logger.error(
                "Metadata notification was not delivered for order %s, SKU '%s', "
                "metadata '%s', value '%s'",
                reference,
                warning.sku,
                warning.key,
                warning.value,
            )

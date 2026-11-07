"""Import open WooCommerce orders into InvenTree sales orders using the ORM."""

import logging
from typing import Dict, List, Optional, Tuple

from django.core.cache import cache
from django.db import transaction

from .mapping import (
    SHIPPING_EXTRA_LINE_DESCRIPTION,
    MetadataWarning,
    build_line_items,
    extract_shipping_total,
    normalize_text,
)

logger = logging.getLogger(__name__)

LOCK_KEY = "woocommerce_order_import_lock"
LOCK_TIMEOUT_SECONDS = 15 * 60
PAGE_SIZE = 100
MAX_PAGES = 50


class WooCommerceOrderImporter:
    """Fetch open WooCommerce orders and create matching InvenTree sales orders."""

    def __init__(self, plugin):
        """Store the plugin used for settings and WooCommerce API calls."""
        self.plugin = plugin

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self, trigger: str = "manual") -> Dict[str, int]:
        """Import all open WooCommerce orders and return a result summary."""
        summary = {"created": 0, "skipped": 0, "failed": 0}

        if not cache.add(LOCK_KEY, trigger, timeout=LOCK_TIMEOUT_SECONDS):
            logger.warning("WooCommerce import already running, skipping (%s)", trigger)
            return summary

        try:
            logger.warning("Starting WooCommerce order import (trigger=%s)", trigger)

            for woo_order in self.fetch_orders():
                number = woo_order.get("number")
                try:
                    outcome = self.process_order(woo_order)
                except Exception:
                    logger.exception("Unexpected error processing order #%s", number)
                    outcome = "failed"
                summary[outcome] += 1

            logger.warning("WooCommerce order import finished: %s", summary)
            return summary
        finally:
            cache.delete(LOCK_KEY)

    # ------------------------------------------------------------------
    # WooCommerce
    # ------------------------------------------------------------------

    def fetch_orders(self) -> List[Dict]:
        """Fetch all orders with the configured status, following pagination."""
        status = self.plugin.get_setting(
            "WOOCOMMERCE_IMPORT_STATUS", backup_value="processing"
        )
        orders: List[Dict] = []
        page = 1
        total_pages = 1

        while page <= total_pages and page <= MAX_PAGES:
            response = self.plugin.api_call(
                f"orders?status={status}&per_page={PAGE_SIZE}&page={page}",
                simple_response=False,
            )
            if response.status_code >= 400:
                logger.error(
                    "Fetching WooCommerce orders failed with HTTP %s: %s",
                    response.status_code,
                    response.text,
                )
                break

            orders.extend(response.json())
            try:
                total_pages = int(response.headers.get("X-WP-TotalPages", 1))
            except (TypeError, ValueError):
                total_pages = 1
            page += 1

        logger.warning("Fetched %s WooCommerce order(s) with status '%s'", len(orders), status)
        return orders

    # ------------------------------------------------------------------
    # Per-order processing
    # ------------------------------------------------------------------

    def process_order(self, woo_order: Dict) -> str:
        """Process one WooCommerce order; return 'created', 'skipped' or 'failed'."""
        from order.models import SalesOrder
        from order.status_codes import SalesOrderStatus

        number = woo_order.get("number")
        reference = f"SO-{number}"

        existing = SalesOrder.objects.filter(reference=reference).first()
        if existing is not None and existing.status != SalesOrderStatus.PENDING.value:
            logger.warning("Order %s already handled (status %s), skipping", reference, existing.status)
            return "skipped"

        billing = woo_order.get("billing", {}) or {}
        name = f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip()
        email = billing.get("email", "")
        street = billing.get("address_1", "")
        zipcode = billing.get("postcode", "")
        city = billing.get("city", "")
        country = billing.get("country", "")

        if not (email and name and zipcode and country):
            logger.warning("Order #%s is missing customer info, skipping", number)
            return "failed"

        metadata_warnings: List[MetadataWarning] = []
        try:
            with transaction.atomic():
                customer, address = self.find_or_create_customer(
                    email, name, street, zipcode, city, country
                )

                order = existing
                if order is None:
                    order = SalesOrder.objects.create(
                        reference=reference,
                        customer=customer,
                        address=address,
                    )
                    logger.warning("Created sales order %s", reference)

                self.add_shipping_line(order, woo_order)
                self.add_part_lines(order, woo_order, metadata_warnings)

                order.issue_order()
        finally:
            if metadata_warnings:
                try:
                    from .notifications import notify_metadata_warnings

                    notify_metadata_warnings(reference, metadata_warnings)
                except Exception:
                    logger.exception(
                        "Could not send metadata notifications for order %s", reference
                    )

        self.allocate_stock(order)
        logger.warning("Successfully processed order #%s", number)
        return "created"

    # ------------------------------------------------------------------
    # Customers and addresses
    # ------------------------------------------------------------------

    def find_or_create_customer(
        self, email: str, name: str, street: str, zipcode: str, city: str, country: str
    ) -> Tuple[object, object]:
        """Return (customer, address), creating either when missing."""
        from company.models import Address, Company

        customer = Company.objects.filter(name__iexact=name, is_customer=True).first()
        if customer is None:
            customer = Company.objects.create(
                name=name,
                email=email,
                is_customer=True,
                is_supplier=False,
                is_manufacturer=False,
            )
            logger.warning("Created customer %s", name)

        target = (
            normalize_text(street),
            normalize_text(zipcode),
            normalize_text(city),
            normalize_text(country),
        )
        for address in Address.objects.filter(company=customer):
            current = (
                normalize_text(address.line1),
                normalize_text(address.postal_code),
                normalize_text(address.postal_city),
                normalize_text(address.country),
            )
            if current == target:
                return customer, address

        address = Address.objects.create(
            company=customer,
            title="Shipping",
            line1=street,
            postal_code=zipcode,
            postal_city=city,
            country=country,
        )
        logger.warning("Created address %s for customer %s", address.pk, name)
        return customer, address

    # ------------------------------------------------------------------
    # Lines
    # ------------------------------------------------------------------

    def add_shipping_line(self, order, woo_order: Dict) -> None:
        """Add the shipping costs as an extra line when not yet present."""
        from order.models import SalesOrderExtraLine

        shipping_total = extract_shipping_total(woo_order)
        if shipping_total is None:
            logger.warning("No valid shipping total for order %s", order.reference)
            return

        try:
            if SalesOrderExtraLine.objects.filter(
                order=order, description__iexact=SHIPPING_EXTRA_LINE_DESCRIPTION
            ).exists():
                return

            SalesOrderExtraLine.objects.create(
                order=order,
                description=SHIPPING_EXTRA_LINE_DESCRIPTION,
                price=shipping_total,
                quantity=1,
            )
        except Exception:
            logger.warning(
                "Could not add extra line '%s' to order %s (quantity=1, price=%s)",
                SHIPPING_EXTRA_LINE_DESCRIPTION,
                order.reference,
                shipping_total,
                exc_info=True,
            )
            raise

    def add_part_lines(
        self,
        order,
        woo_order: Dict,
        metadata_warnings: Optional[List[MetadataWarning]] = None,
    ) -> None:
        """Add the mapped part lines, skipping parts already on the order."""
        from order.models import SalesOrderLineItem
        from part.models import Part

        if metadata_warnings is None:
            line_items = build_line_items(woo_order.get("line_items", []))
        else:
            line_items = build_line_items(
                woo_order.get("line_items", []), metadata_warnings.append
            )
        if not line_items:
            logger.warning("No supported line items for order %s", order.reference)
            return

        try:
            existing_part_ids = set(
                SalesOrderLineItem.objects.filter(order=order).values_list("part_id", flat=True)
            )
        except Exception:
            for item in line_items:
                logger.warning(
                    "Could not check existing lines for order %s, SKU '%s' (quantity=%s)",
                    order.reference,
                    item.get("part_sku", ""),
                    item.get("quantity", 1),
                    exc_info=True,
                )
            raise
        pending_lines: Dict[Tuple[int, object, object], Dict] = {}
        pending_skus: Dict[Tuple[int, object, object], set] = {}

        for item in line_items:
            part_sku = item.get("part_sku", "")
            quantity = item.get("quantity", 1)
            try:
                matches = list(
                    Part.objects.filter(
                        IPN__iexact=part_sku, salable=True, active=True
                    ).values_list("pk", flat=True)[:2]
                )
            except Exception:
                logger.warning(
                    "Could not look up InvenTree SKU '%s' for order %s (quantity=%s)",
                    part_sku,
                    order.reference,
                    quantity,
                    exc_info=True,
                )
                raise

            if not matches:
                logger.warning(
                    "InvenTree part with SKU '%s' not found for order %s "
                    "(quantity=%s), skipping line",
                    part_sku,
                    order.reference,
                    quantity,
                )
                continue
            if len(matches) > 1:
                logger.warning(
                    "Multiple salable InvenTree parts with SKU '%s' for order %s "
                    "(quantity=%s), skipping line",
                    part_sku,
                    order.reference,
                    quantity,
                )
                continue

            part_id = matches[0]
            if part_id in existing_part_ids:
                continue

            sale_price = item.get("sale_price")
            notes = item.get("notes")
            key = (part_id, sale_price, notes)
            if key not in pending_lines:
                pending_lines[key] = {
                    "order": order,
                    "part_id": part_id,
                    "quantity": 0,
                }
                pending_skus[key] = set()
                if notes:
                    pending_lines[key]["notes"] = notes
                if sale_price is not None:
                    pending_lines[key]["sale_price"] = sale_price

            pending_skus[key].add(part_sku)
            pending_lines[key]["quantity"] += quantity

        for key, data in pending_lines.items():
            try:
                SalesOrderLineItem.objects.create(**data)
            except Exception:
                logger.warning(
                    "Could not add InvenTree SKU(s) %s to order %s (quantity=%s)",
                    ", ".join(sorted(pending_skus[key])),
                    order.reference,
                    data["quantity"],
                    exc_info=True,
                )
                raise

    # ------------------------------------------------------------------
    # Shipment and allocation
    # ------------------------------------------------------------------

    def allocate_stock(self, order) -> None:
        """Allocate available stock for every line to the open shipment."""
        from order.models import SalesOrderAllocation, SalesOrderShipment
        from stock.models import StockItem

        shipment = SalesOrderShipment.objects.filter(
            order=order, shipment_date=None
        ).first()
        if shipment is None:
            logger.warning("No open shipment for order %s, skipping allocation", order.reference)
            return

        for line in order.lines.all():
            needed = line.quantity - line.allocated_quantity()
            if needed <= 0 or not line.part:
                continue

            items = StockItem.objects.filter(StockItem.IN_STOCK_FILTER, part=line.part)
            for stock_item in items:
                if needed <= 0:
                    break
                available = stock_item.unallocated_quantity()
                quantity = min(available, needed)
                if quantity <= 0:
                    continue

                try:
                    allocation = SalesOrderAllocation(
                        line=line,
                        shipment=shipment,
                        item=stock_item,
                        quantity=quantity,
                    )
                    allocation.full_clean()
                    allocation.save()
                    needed -= quantity
                except Exception as exc:
                    logger.warning(
                        "Could not allocate stock item %s to %s: %s",
                        stock_item.pk,
                        order.reference,
                        exc,
                    )

"""Pure mapping helpers that translate WooCommerce orders into InvenTree line items."""

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SHIPPING_EXTRA_LINE_DESCRIPTION = 'Verzendkosten'

SKU_PRODUCT_TRANSLATION_MAP = {
    'amber-control-module-4relay': 'Heatpump module - 4 relay',
    'heatpump-listener-4relay': 'Heatpump module - 4 relay',
    'amber-control-module-2relay': 'Heatpump module - 2 relay',
    'heatpump-listener-2relay': 'Heatpump module - 2 relay',
    'hpc-q': 'Heatpump Controller Q-Edition',
}

SKU_RELAY_COUNT_MAP = {
    'amber-control-module-4relay': 4,
    'heatpump-listener-4relay': 4,
    'amber-control-module-2relay': 2,
    'heatpump-listener-2relay': 2,
}


def normalize_text(value: object) -> str:
    """Normalize text values for case-insensitive comparisons."""
    return str(value or '').strip().lower()

def extract_shipping_total(woo_order: Dict) -> Optional[str]:
    """Extract shipping total from first WooCommerce shipping line as decimal string."""
    shipping_lines = woo_order.get('shipping_lines', [])

    if not shipping_lines:
        return None

    first_shipping_line = shipping_lines[0] if shipping_lines else {}
    total_raw = first_shipping_line.get('total')

    if total_raw is None:
        return None

    normalized_total = normalize_decimal_text(total_raw)
    if normalized_total is None:
        logger.warning(
            "Invalid shipping total '%s' for order #%s",
            total_raw,
            woo_order.get('number')
        )
        return None

    return normalized_total

def normalize_decimal_text(value: object) -> Optional[str]:
    """Normalize a decimal-like value to a string format accepted by InvenTree."""
    if value is None:
        return None

    value_text = str(value).strip()
    if not value_text:
        return None

    try:
        parsed = Decimal(value_text)
    except (InvalidOperation, ValueError):
        return None

    return format(parsed, 'f')

# ============================================================================
# CUSTOM FIELD MAPPING LOGIC
# ============================================================================

def get_line_item_meta_value(line_item: Dict, target_keys: List[str]) -> Optional[str]:
    """Get a line item meta value by key (case-insensitive)."""
    target_set = {key.lower() for key in target_keys}
    for meta in line_item.get('meta_data', []):
        if normalize_text(meta.get('key', '')) in target_set:
            value = meta.get('value')
            return '' if value is None else str(value)
    return None

def parse_int_from_text(value: Optional[str]) -> Optional[int]:
    """Extract first integer from a string value."""
    if value is None:
        return None
    match = re.search(r'\d+', str(value))
    if not match:
        return None
    return int(match.group(0))

def build_heatpump_controller_line_items(
    source_line_item: Dict,
    primary_sku_price: Optional[str] = None
) -> List[Dict]:
    """Build line items for heatpump controller orders."""
    # Determine Tweakers edition
    tweakers_raw = get_line_item_meta_value(source_line_item, ['Tweakers editie'])
    temperature_sensor_raw = get_line_item_meta_value(source_line_item, ['Extra temperatuursensor'])
    wall_holder_raw = get_line_item_meta_value(source_line_item, ['Bevestigingsbeugel'])
    usbcable_raw = get_line_item_meta_value(source_line_item, ['USB C kabel', 'USB C kabel (1 meter)'])

    if tweakers_raw and normalize_text(tweakers_raw) == 'ja':
        primary_line_name = "Heatpump Controller Q-Edition (Tweaker)"
        dupont = True
    else:
        primary_line_name = "Heatpump Controller Q-Edition"
        dupont = False

    line_items = [
        {
            'name': primary_line_name,
            'quantity': 1,
            'sale_price': primary_sku_price
        }
    ]

    if wall_holder_raw and normalize_text(wall_holder_raw) == 'ja':
        line_items.append(
            {
                'name': 'HPC-Q Wall holder',
                'quantity': 1
            }
        )

    if usbcable_raw and normalize_text(usbcable_raw) == 'ja':
        line_items.append(
            {
                'name': 'USB-C cable',
                'quantity': 1
            }
        )

    temperature_sensor_value = normalize_text(temperature_sensor_raw)
    if temperature_sensor_value.startswith('ja'):
        line_items.extend([
            {
                'name': 'Temperature sensor - 2M',
                'quantity': 1
            },
            {
                'name': 'HPC-Q Aansluitset 2',
                'quantity': 1
            }
        ])

        holder_size = parse_int_from_text(temperature_sensor_value[len('ja'):])
        if holder_size is not None:
            line_items.append(
                {
                    'name': f'Houder DS18B20 ({holder_size} mm)',
                    'quantity': 1
                }
            )
    else:
        line_items.append(
            {
                'name': 'HPC-Q Aansluitset 1',
                'quantity': 1
            }
        )

    if dupont:
        line_items.append(
            {
                'name': 'Dupont cable 20CM female-female (set of 10)',
                'quantity': 1
            }
        )

    line_items.append(
        {
            'name': 'Verzenddoos A6',
            'quantity': 1
        }
    )

    return line_items

def build_heatpump_module_line_items(
    primary_sku: str,
    source_line_items: List[Dict],
    primary_sku_price: Optional[str] = None
) -> List[Dict]:
    """Build line items for heatpump module (2/4 relay) orders."""
    includes_aansluitset = False
    module_notes: Optional[str] = None
    temperature_sensor_qty = 0

    if primary_sku.startswith('amber'):
        module_notes = 'Amber'

    for item in source_line_items:
        if primary_sku.startswith('heatpump-listener') and not module_notes:
            softwareversie = get_line_item_meta_value(item, ['softwareversie'])
            if softwareversie and softwareversie.strip():
                module_notes = softwareversie.strip()

        aansluitset_raw = get_line_item_meta_value(item, ['Aansluitset', 'aansluitset'])
        if aansluitset_raw and normalize_text(aansluitset_raw) in {'ja', 'yes', 'true', '1'}:
            includes_aansluitset = True

        temperatuur_sensoren_raw = get_line_item_meta_value(item, ['Temperatuursensoren', 'temperatuursensoren'])
        item_sensor_qty = parse_int_from_text(temperatuur_sensoren_raw)
        if item_sensor_qty is not None and item_sensor_qty > 0:
            temperature_sensor_qty += item_sensor_qty

    relay_count = SKU_RELAY_COUNT_MAP[primary_sku]

    module_product_name = SKU_PRODUCT_TRANSLATION_MAP.get(
        primary_sku,
        f"Heatpump module - {relay_count} relay"
    )

    connector_2p_qty = relay_count if includes_aansluitset else relay_count + 1
    connector_3p_qty = 4 - temperature_sensor_qty

    line_items = [
        {
            'name': module_product_name,
            'quantity': 1,
            'reason': f"module selected from SKU ({primary_sku or 'fallback'})",
            'notes': module_notes,
            'sale_price': primary_sku_price
        },
    ]

    if includes_aansluitset:
        line_items.append({
            'name': 'Amber connection set',
            'quantity': 1,
            'reason': 'Aansluitset is Ja'
        })
    else:
        line_items.append({
            'name': 'Gripzak 80x120',
            'quantity': 1,
            'reason': 'Aansluitset is not Ja for heatpump module'
        })

    if temperature_sensor_qty > 0:
        line_items.append({
            'name': 'Temperature sensor - 2M',
            'quantity': temperature_sensor_qty,
            'reason': 'from metadata Temperatuursensoren'
        })

    line_items.extend([
        {
            'name': '2pin screw connector',
            'quantity': connector_2p_qty,
            'reason': (
                f"{connector_2p_qty} because relay count is {relay_count} "
                + ('and Aansluitset is Ja' if includes_aansluitset else 'and Aansluitset is not Ja')
            )
        },
        {
            'name': '3pin screw connector',
            'quantity': connector_3p_qty,
            'reason': f'{connector_3p_qty} because add 4 minus number of temperature sensors'
        },
        {
            'name': 'Verzenddoos A5',
            'quantity': 1,
            'reason': 'always add for these 2relay/4relay SKUs'
        }
    ])

    return line_items

def build_line_items(line_items: List[Dict]) -> List[Dict]:
    """Build line items for Heatpump Controle Module based orders."""
    primary_sku = ''
    primary_sku_price: Optional[str] = None

    for item in line_items:
        item_sku = normalize_text(item.get('sku', ''))

        if not primary_sku and item_sku in SKU_PRODUCT_TRANSLATION_MAP:
            primary_sku = item_sku
            primary_sku_price = normalize_decimal_text(item.get('price'))

            if primary_sku_price is None and item.get('price') is not None:
                logger.warning(
                    "Invalid primary SKU price '%s' for sku '%s'",
                    item.get('price'),
                    item_sku
                )

    if primary_sku == 'hpc-q':
        return build_heatpump_controller_line_items(line_items[0], primary_sku_price)

    if not primary_sku:
        return []

    return build_heatpump_module_line_items(primary_sku, line_items, primary_sku_price)

# ============================================================================
# MAIN SYNCHRONIZATION LOGIC
# ============================================================================



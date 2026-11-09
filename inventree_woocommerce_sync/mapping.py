"""Pure mapping helpers that translate WooCommerce orders into InvenTree line items."""

import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional

logger = logging.getLogger(__name__)

SHIPPING_EXTRA_LINE_DESCRIPTION = 'Verzendkosten'
LINE_ITEM_MAPPING_FILENAME = "line_item_mapping.json"
DEFAULT_INVENTREE_DATA_DIR = Path("/home/inventree/data")
LINE_ITEM_MAPPING_PATH = Path(
    os.environ.get(
        "WOOCOMMERCE_LINE_ITEM_MAPPING_FILE",
        DEFAULT_INVENTREE_DATA_DIR
        / "plugins"
        / "inventree-woocommerce-sync"
        / LINE_ITEM_MAPPING_FILENAME,
    )
)


class MetadataWarning(NamedTuple):
    """An unsupported metadata value on a WooCommerce line."""

    sku: str
    key: str
    value: str

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

def load_line_item_mapping() -> Dict:
    """Load the latest rules from InvenTree's persistent data directory."""
    try:
        LINE_ITEM_MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not LINE_ITEM_MAPPING_PATH.exists():
            packaged_config = resources.files(__package__).joinpath(
                LINE_ITEM_MAPPING_FILENAME
            )
            LINE_ITEM_MAPPING_PATH.write_bytes(packaged_config.read_bytes())
            logger.info(
                "Created default line-item mapping at %s", LINE_ITEM_MAPPING_PATH
            )

        with LINE_ITEM_MAPPING_PATH.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not load line item mapping configuration: {LINE_ITEM_MAPPING_PATH}"
        ) from exc

    if not isinstance(config, dict):
        raise ValueError("Line item mapping configuration must be a JSON object")
    if not isinstance(config.get("products"), dict):
        raise ValueError("Line item mapping configuration requires a 'products' object")

    return config


def _parse_positive_integer(value: object) -> Optional[int]:
    """Parse an integer quantity without accepting partial or fractional values."""
    value_text = str(value or "").strip()
    if re.fullmatch(r"\d+", value_text) is None:
        return None

    quantity = int(value_text)
    return quantity if quantity > 0 else None


def _metadata_integer(
    source_line_item: Dict, key: str, default: Optional[int] = None
) -> Optional[int]:
    """Extract an integer from metadata for declarative quantity expressions."""
    raw_value = get_line_item_meta_value(source_line_item, [key])
    if raw_value is None:
        return default
    match = re.search(r"\d+", raw_value)
    if match is None:
        logger.warning(
            "Invalid integer metadata '%s': '%s'",
            key,
            raw_value,
        )
        return default
    return int(match.group(0))


def _matches_condition(condition: Dict, source_line_item: Dict, context: Dict) -> bool:
    """Evaluate the supported declarative rule conditions."""
    metadata_key = condition.get("metadata")
    if metadata_key:
        raw_value = get_line_item_meta_value(source_line_item, [metadata_key])
        if raw_value is None:
            return False
        normalized_value = normalize_text(raw_value)
        if "equals" in condition:
            return normalized_value == normalize_text(condition["equals"])
        if "in" in condition:
            return normalized_value in {
                normalize_text(value) for value in condition["in"]
            }

    if "metadata_int" in condition and "greater_than" in condition:
        value = _metadata_integer(source_line_item, condition["metadata_int"])
        return value is not None and value > condition["greater_than"]

    if "all" in condition:
        return all(
            _matches_condition(part, source_line_item, context)
            for part in condition["all"]
        )
    if "any" in condition:
        return any(
            _matches_condition(part, source_line_item, context)
            for part in condition["any"]
        )
    if "not" in condition:
        return not _matches_condition(condition["not"], source_line_item, context)

    raise ValueError(f"Unsupported line-item condition: {condition!r}")


def _evaluate_quantity(expression: object, source_line_item: Dict, context: Dict) -> int:
    """Evaluate a safe arithmetic expression from mapping JSON."""
    if isinstance(expression, int) and not isinstance(expression, bool):
        return expression
    if not isinstance(expression, dict) or len(expression) != 1:
        raise ValueError(f"Invalid line-item quantity expression: {expression!r}")

    operation, operand = next(iter(expression.items()))
    if operation == "metadata_int":
        if isinstance(operand, str):
            key, default = operand, 0
        else:
            key = operand["key"]
            default = operand.get("default", 0)
        value = _metadata_integer(source_line_item, key, default)
        if value is None:
            raise ValueError(f"Metadata '{key}' is not an integer")
        return value
    if operation == "variable":
        if operand not in context:
            raise ValueError(f"Unknown line-item quantity variable '{operand}'")
        return context[operand]
    if operation == "add":
        if not isinstance(operand, list) or not operand:
            raise ValueError("'add' quantity expression requires a non-empty list")
        return sum(
            _evaluate_quantity(value, source_line_item, context)
            for value in operand
        )
    if operation == "subtract":
        if not isinstance(operand, list) or len(operand) != 2:
            raise ValueError("'subtract' quantity expression requires two values")
        return _evaluate_quantity(
            operand[0], source_line_item, context
        ) - _evaluate_quantity(operand[1], source_line_item, context)
    if operation == "if":
        if not isinstance(operand, dict):
            raise ValueError("'if' quantity expression requires an object")
        branch = "then" if _matches_condition(
            operand["condition"], source_line_item, context
        ) else "else"
        return _evaluate_quantity(operand[branch], source_line_item, context)

    raise ValueError(f"Unsupported line-item quantity operation '{operation}'")


def _build_configured_line_items(
    source_line_item: Dict,
    product_rules: Dict,
    on_metadata_warning: Optional[Callable[[MetadataWarning], None]] = None,
) -> List[Dict]:
    """Apply the JSON-configured rules to one WooCommerce order line."""
    order_quantity = _parse_positive_integer(source_line_item.get("quantity", 1))
    sku = normalize_text(source_line_item.get("sku"))
    if order_quantity is None:
        logger.warning(
            "Invalid quantity '%s' for WooCommerce SKU '%s'; skipping line",
            source_line_item.get("quantity"),
            sku,
        )
        return []

    context = product_rules.get("variables", {})
    required_metadata = set(product_rules.get("required_metadata", []))
    allowed_metadata_values = product_rules.get("allowed_metadata_values", {})
    invalid_required_metadata = False
    for metadata_key in required_metadata | set(allowed_metadata_values):
        raw_value = get_line_item_meta_value(source_line_item, [metadata_key])
        if raw_value is None:
            if metadata_key in required_metadata:
                logger.warning(
                    "Missing required metadata '%s' for WooCommerce SKU '%s'",
                    metadata_key,
                    sku,
                )
                invalid_required_metadata = True
            continue

        allowed_values = allowed_metadata_values.get(metadata_key)
        if allowed_values and normalize_text(raw_value) not in {
            normalize_text(value) for value in allowed_values
        }:
            logger.warning(
                "Unsupported metadata '%s' value '%s' for WooCommerce SKU '%s'",
                metadata_key,
                raw_value,
                sku,
            )
            if on_metadata_warning is not None:
                on_metadata_warning(MetadataWarning(sku, metadata_key, raw_value))
            if metadata_key in required_metadata:
                invalid_required_metadata = True

    if invalid_required_metadata:
        return []

    primary_price = normalize_decimal_text(source_line_item.get("price"))
    if primary_price is None and source_line_item.get("price") is not None:
        logger.warning(
            "Invalid price '%s' for WooCommerce SKU '%s'",
            source_line_item.get("price"),
            sku,
        )

    result: List[Dict] = []
    for rule in product_rules.get("rules", []):
        condition = rule.get("when")
        if condition and not _matches_condition(condition, source_line_item, context):
            continue

        for configured_item in rule.get("add", []):
            part_sku = configured_item.get("sku")
            if not isinstance(part_sku, str) or not part_sku.strip():
                raise ValueError(
                    f"Line item mapping for WooCommerce SKU '{sku}' requires "
                    "an InvenTree SKU in each add entry"
                )

            try:
                quantity_per_unit = _evaluate_quantity(
                    configured_item.get("quantity", 1),
                    source_line_item,
                    context,
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "Could not evaluate quantity for WooCommerce SKU '%s', "
                    "InvenTree SKU '%s': %s",
                    sku,
                    part_sku,
                    exc,
                )
                continue

            if quantity_per_unit <= 0:
                continue

            item = {
                "part_sku": part_sku,
                "quantity": order_quantity * quantity_per_unit,
            }
            if configured_item.get("price_from_order_line") and primary_price is not None:
                item["sale_price"] = primary_price

            notes = configured_item.get("notes")
            if isinstance(notes, dict) and "metadata" in notes:
                notes = get_line_item_meta_value(
                    source_line_item, [notes["metadata"]]
                )
            if notes:
                item["notes"] = notes
            result.append(item)

    return result

def build_line_items(
    line_items: List[Dict],
    on_metadata_warning: Optional[Callable[[MetadataWarning], None]] = None,
) -> List[Dict]:
    """Build mapped InvenTree lines using the current persistent JSON rules."""
    config = load_line_item_mapping()
    products = {
        normalize_text(sku): rules
        for sku, rules in config["products"].items()
    }

    mapped_items: List[Dict] = []
    for source_line_item in line_items:
        sku = normalize_text(source_line_item.get("sku"))
        product_rules = products.get(sku)
        if product_rules is not None:
            mapped_items.extend(
                _build_configured_line_items(
                    source_line_item, product_rules, on_metadata_warning
                )
            )
        elif sku:
            logger.warning("No line item mapping configured for WooCommerce SKU '%s'", sku)

    return mapped_items

# ============================================================================
# MAIN SYNCHRONIZATION LOGIC
# ============================================================================

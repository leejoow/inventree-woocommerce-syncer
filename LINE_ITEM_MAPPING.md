# WooCommerce line-item mapping configuration

The importer reads `line_item_mapping.json` for each WooCommerce order. The
configuration maps WooCommerce product SKUs and their metadata to InvenTree
parts, identified by InvenTree's IPN/SKU.

In Docker, the active file is:

```text
/home/inventree/data/plugins/inventree-woocommerce-sync/line_item_mapping.json
```

On first use, the plugin copies the packaged default file to this location if
it does not already exist. Existing files are never overwritten. Edit the file
in the mounted InvenTree data volume; changes apply to the next imported order
without restarting InvenTree. To use a different path, set
`WOOCOMMERCE_LINE_ITEM_MAPPING_FILE` to the same path in both the server and
worker containers.

## JSON Schema and validation

The accompanying [JSON Schema](inventree_woocommerce_sync/line_item_mapping.schema.json)
describes the supported structure and detects mistakes such as a `sku`
directly inside `rules` instead of inside a rule's `add` list. It deliberately
rejects unknown fields and empty rules, even where the importer might silently
ignore them.

For editor validation, place the schema next to the mapping file and add this
optional property at the root of the mapping:

```json
"$schema": "./line_item_mapping.schema.json"
```

In Docker, copy the schema to the active mapping's data directory as well; the
plugin does not automatically copy it there. The importer ignores `$schema`
and does **not** automatically enforce the schema. `python -m json.tool` checks
JSON syntax only, not the schema. To validate from the command line, install
the project's test dependencies (`pip install -e ".[test]"`) and run:

```bash
python -m jsonschema -i line_item_mapping.json line_item_mapping.schema.json
```

Schema validation cannot confirm that an InvenTree IPN exists, that a variable
is declared, or that a WooCommerce metadata key and value match those sent by
the store. JSON Schema treats `1.0` as an integer, but the importer requires
integer literals such as `1` for quantities and variables.

## Overall structure

The root object has a `products` object. Each key under `products` is an exact
WooCommerce SKU (case-insensitive). A product contains:

| Property | Required | Purpose |
| --- | --- | --- |
| `rules` | Yes | Ordered list of rules to evaluate for each matching order line. |
| `variables` | No | Named numeric values usable in quantity expressions. |
| `required_metadata` | No | Metadata keys that must be present on the WooCommerce line. |
| `allowed_metadata_values` | No | Known values for metadata keys; unexpected values are logged. |

Example:

```json
{
  "products": {
    "dallas_sensor": {
      "rules": [
        {
          "add": [
            {
              "sku": "sensor",
              "quantity": 1,
              "price_from_order_line": true
            }
          ]
        }
      ]
    }
  }
}
```

The example adds one InvenTree part with IPN `sensor` for each unit ordered.
The `sku` in an `add` entry is the InvenTree IPN, not the WooCommerce SKU or
the InvenTree part name. The importer looks up the part by IPN, and requires
it to be active and salable.

## Rules and conditions

Each rule can contain:

| Property | Purpose |
| --- | --- |
| `when` | Optional condition. If omitted, the rule always applies. |
| `add` | Parts to add when the condition matches. |

Conditions can inspect text metadata or an integer extracted from metadata.
Text comparisons ignore case and surrounding whitespace.

### Text metadata

Match one value with `equals`:

```json
{
  "when": {"metadata": "Tweakers editie", "equals": "Ja"},
  "add": [{"sku": "hpcq_t_1.3", "quantity": 1}]
}
```

Match one of several values with `in`:

```json
{
  "when": {
    "metadata": "Aansluitset",
    "in": ["Ja", "Yes", "True", "1"]
  },
  "add": [{"sku": "amber_connection_set", "quantity": 1}]
}
```

### Combining conditions

- `all`: every nested condition must match.
- `any`: at least one nested condition must match.
- `not`: the nested condition must not match.

These can be nested. For example:

```json
{
  "when": {
    "all": [
      {"metadata": "Tweakers editie", "equals": "Ja"},
      {
        "any": [
          {"metadata": "Temperatuursensor", "equals": "Ja-22"},
          {"metadata": "Temperatuursensor", "equals": "Ja-28"}
        ]
      }
    ]
  },
  "add": [{"sku": "thermalpaste", "quantity": 1}]
}
```

### Integer metadata

`metadata_int` extracts the first sequence of digits from a metadata value.
For example, `2` is extracted from `"2 sensoren"`. Use it in a condition with
`greater_than`:

```json
{
  "when": {"metadata_int": "Temperatuursensoren", "greater_than": 0},
  "add": [{"sku": "temperature_sensor_2m", "quantity": 1}]
}
```

If metadata is absent or has no digits, this condition does not match.

## Parts and quantities

Each item in `add` has these options:

| Property | Required | Purpose |
| --- | --- | --- |
| `sku` | Yes | InvenTree IPN of the part to add. |
| `quantity` | No | Quantity per WooCommerce unit; defaults to `1`. |
| `price_from_order_line` | No | If `true`, use the WooCommerce line's unit price as the InvenTree sale price. |
| `notes` | No | Fixed notes, or `{"metadata": "key"}` to copy a metadata value. |

The calculated quantity is multiplied by the WooCommerce line's `quantity`.
For example, `quantity: 2` adds two parts for every unit of the WooCommerce
product.

`quantity` can be a positive integer or an expression:

| Expression | Meaning |
| --- | --- |
| `{"metadata_int": "Temperatuursensoren"}` | Use the first integer found in that metadata. |
| `{"metadata_int": {"key": "Temperatuursensoren", "default": 0}}` | Same, but use `default` if missing or invalid. |
| `{"variable": "relay_count"}` | Use a value declared in the product's `variables`. |
| `{"add": [expression, expression, ...]}` | Add one or more expressions. |
| `{"subtract": [expression, expression]}` | Subtract the second expression from the first. |
| `{"if": {"condition": ..., "then": ..., "else": ...}}` | Choose an expression based on a condition. |

Expressions can be nested. Example: add one connector per relay, plus one more
when no connection set is selected:

```json
{
  "variables": {"relay_count": 4},
  "rules": [
    {
      "add": [
        {
          "sku": "connector_2pin",
          "quantity": {
            "if": {
              "condition": {
                "metadata": "Aansluitset",
                "in": ["Ja", "Yes", "True", "1"]
              },
              "then": {"variable": "relay_count"},
              "else": {"add": [{"variable": "relay_count"}, 1]}
            }
          }
        }
      ]
    }
  ]
}
```

Calculated quantities of zero or less are ignored. Invalid expressions are
logged and that part is skipped.

## Metadata validation

Use `required_metadata` to prevent a product line from being mapped when a
key is missing:

```json
{
  "required_metadata": ["Tweakers editie"]
}
```

Use `allowed_metadata_values` to document expected values and log unexpected
ones:

```json
{
  "allowed_metadata_values": {
    "Tweakers editie": ["Ja", "Nee"],
    "Temperatuursensor": ["Nee", "Ja", "Ja-18", "Ja-22", "Ja-28"]
  }
}
```

An unrecognized value for a required metadata key skips the line. An
unrecognized value for a non-required key is logged; rules that do not match
that value simply do not add their parts.

On InvenTree 1.7.0, an unrecognized allowed metadata value also produces an
in-app notification for all active users. It includes the order reference,
WooCommerce SKU, metadata key and received value. Each distinct combination is
notified once, including on repeated imports; changing the value produces a new
notification. The built-in InvenTree UI notification plugin must be active.
Notification failures are logged and do not change the order import result.
Missing required metadata continues to be logged but is not included in these
notifications.

## Adding a product or rule

1. Add the WooCommerce SKU as a key under `products`.
2. Add one or more rules under its `rules` list.
3. Use the exact metadata key and value sent by WooCommerce.
4. Put the exact InvenTree IPN in each `add` entry's `sku`.
5. Set any metadata requirements or product-specific `variables` as needed.
6. Validate the JSON before using it:

   ```bash
    check-jsonschema .\line_item_mapping.json --schemafile .\line_item_mapping.schema.json
   ```

Rules without `when` are unconditional. Multiple matching rules all apply.
If the same InvenTree part is added more than once with the same sale price
and notes, the importer combines those quantities into one order line.

## Import warnings

The importer logs a warning when a mapped part cannot be found, matches
multiple active and salable parts, or cannot be looked up or added. The warning
includes the order reference, InvenTree IPN and quantity. A failure to add the
shipping extra line is also logged with its description and price. Database
errors are re-raised and roll back the order import; lines already present
on the order are intentionally skipped without a warning.

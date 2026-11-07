# InvenTree WooCommerce Sync

An InvenTree plugin that provides the event boundary for synchronizing a shipped sales order to WooCommerce.

## Current behavior

The plugin listens for the InvenTree event `salesordershipment.completed`. It extracts the WooCommerce order number from a sales order reference such as `SO-741`, then sends this request:

```text
PUT https://example.com/wp-json/wc/v3/orders/741
```

The request sets the WooCommerce status to `completed` and updates the order metadata key `tracking_number` with the shipment tracking number.

The WooCommerce connection is configured through the plugin settings:

- `WOOCOMMERCE_URL`: host and API path without `https://`, for example `shop.example.com/wp-json/wc/v3`
- `WOOCOMMERCE_CONSUMER_KEY`: WooCommerce REST API consumer key (`ck_...`)
- `WOOCOMMERCE_CONSUMER_SECRET`: WooCommerce REST API consumer secret (`cs_...`)
- `WOOCOMMERCE_IMPORT_INTERVAL_MINUTES`: interval for the open-order import stub; defaults to `60`

Both credentials are stored as protected settings. The plugin logs an error and skips the API call when the sales order reference does not match `SO-<number>`.

If the worker reports HTTP 403 with Basic Auth, set `WOOCOMMERCE_AUTH_METHOD` to `query` and retry. If it still returns 403, verify that the WooCommerce REST API key has **Read/Write** permissions and that the website firewall allows `PUT` requests to `/wp-json/wc/v3/orders/<id>`. The worker log now includes WooCommerce's response body, which usually identifies the exact permission or authentication problem.

## Local validation

```powershell
python -m pip install -e ".[test]"
python -m pytest
```

## Docker installation

Install this package into the InvenTree container, for example by adding the repository URL or a built package to the InvenTree plugin installation configuration. InvenTree must have custom plugins and event integration enabled. For Docker deployments, enable **Check Plugins on Startup** so the plugin is installed again when the container is recreated.

To copy the current plugin files from the project directory to the InvenTree host, run this command in PowerShell:

```powershell
scp -r . root@inventree.local:~/inventree/data/plugins/inventree-woocommerce-sync
```

Then restart the worker and web container so InvenTree reloads the plugin:

```bash
docker compose restart
```

After installation, restart both the InvenTree web server and background worker, then enable **WooCommerce Order Sync** in the Admin Center. Complete a shipment and inspect the worker log at debug level.

## Open-order import framework

The plugin imports open WooCommerce orders. Enable **Schedule Integration** in InvenTree to activate the worker schedule. The worker checks the configured interval once per minute and calls `load_open_woocommerce_orders(trigger="schedule")` when the interval is due.

On sales-order pages, the **Import WooCommerce orders** primary action calls the same method with `trigger="manual"`.

## WooCommerce line-item mapping

The order importer inspects every WooCommerce order line. Product rules are stored in [`inventree_woocommerce_sync/line_item_mapping.json`](./inventree_woocommerce_sync/line_item_mapping.json), so new SKU and metadata combinations can be added there without changing Python code.

- On first use, the plugin copies the packaged defaults to `/home/inventree/data/plugins/inventree-woocommerce-sync/line_item_mapping.json` if the file does not exist. It never overwrites an existing data-folder configuration.
- The file is read again for each imported WooCommerce order. Edit the file in the mounted InvenTree data volume; changes apply to the next order without restarting InvenTree.
- To use a different file location, set `WOOCOMMERCE_LINE_ITEM_MAPPING_FILE` in the server and worker containers to the same path.
- `products` maps a WooCommerce SKU to its ordered rules.
- A rule can match metadata with `when`, using `equals`, `in`, `not`, `all`, or `any`.
- Each `add` entry contains an InvenTree internal part number (`sku`/IPN), a quantity per ordered unit, and optionally `price_from_order_line: true`.
- Quantities can use `metadata_int`, `variable`, `add`, `subtract`, and `if` expressions. Variables are defined per product in `variables`.
- Replace example SKU values with the exact IPNs configured on your InvenTree parts.

For example, a line with quantity 2 and metadata `Temperatuursensoren=2` adds 4 sensors. Repeated mapped parts with the same sale price and notes are combined into one InvenTree order line. Product SKUs, metadata conditions, line-item SKUs, and relay-module quantity formulas are all configured in JSON; Python only evaluates the generic rule and arithmetic syntax.

See [LINE_ITEM_MAPPING.md](./LINE_ITEM_MAPPING.md) for the full JSON format, supported conditions, quantity expressions, and configuration examples.
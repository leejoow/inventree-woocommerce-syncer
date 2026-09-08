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
scp -r . <user>@inventree.local:~/inventree/data/plugins
```

Then restart the worker and web container so InvenTree reloads the plugin:

```bash
docker compose restart
```

After installation, restart both the InvenTree web server and background worker, then enable **WooCommerce Order Sync** in the Admin Center. Complete a shipment and inspect the worker log at debug level.
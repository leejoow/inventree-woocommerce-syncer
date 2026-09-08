import importlib
import sys
import types


def load_plugin_module():
    """Load the plugin with the small portion of InvenTree needed by this test."""
    plugin_module = types.ModuleType("plugin")
    plugin_module.InvenTreePlugin = type("InvenTreePlugin", (), {})
    mixins_module = types.ModuleType("plugin.mixins")
    mixins_module.APICallMixin = type("APICallMixin", (), {})
    mixins_module.EventMixin = type("EventMixin", (), {})

    class SettingsMixin:
        def get_setting(self, key, **kwargs):
            return "basic" if key == "WOOCOMMERCE_AUTH_METHOD" else "test"

    mixins_module.SettingsMixin = SettingsMixin
    status_codes_module = types.ModuleType("order.status_codes")
    status_codes_module.SalesOrderStatus = types.SimpleNamespace(
        SHIPPED=types.SimpleNamespace(value=20),
        COMPLETE=types.SimpleNamespace(value=30),
    )

    sys.modules["plugin"] = plugin_module
    sys.modules["plugin.mixins"] = mixins_module
    sys.modules["order"] = types.ModuleType("order")
    sys.modules["order.status_codes"] = status_codes_module
    return importlib.import_module("inventree_woocommerce_sync.core")


def test_only_shipment_completed_events_are_processed():
    module = load_plugin_module()
    plugin = module.WooCommerceOrderSyncPlugin()

    assert plugin.wants_process_event("salesordershipment.completed")
    assert not plugin.wants_process_event("salesorder.completed")


def test_shipment_event_logs_payload(caplog):
    module = load_plugin_module()
    checked_by = object()

    class FakeOrder:
        pk = 7
        status = 15

        def ship_order(self, user):
            assert user is checked_by

    fake_order_models = types.ModuleType("order.models")
    fake_order_models.SalesOrderShipment = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            get=lambda pk: types.SimpleNamespace(
                order=FakeOrder(), checked_by=checked_by
            )
        )
    )
    sys.modules["order.models"] = fake_order_models

    with caplog.at_level("DEBUG", logger=module.logger.name):
        plugin = module.WooCommerceOrderSyncPlugin()
        plugin.sync_order_to_woocommerce = lambda order, shipment: None
        plugin.process_event("salesordershipment.completed", id=42)

    assert "WooCommerce Order Sync plugin - Handle order" in caplog.text
    assert "Sales order shipment completed" in caplog.text
    assert "order=7" in caplog.text


def test_shipment_event_marks_related_order_as_shipped(monkeypatch):
    module = load_plugin_module()
    plugin = module.WooCommerceOrderSyncPlugin()
    checked_by = object()

    class FakeOrder:
        pk = 7
        status = 15

        def ship_order(self, user):
            assert user is checked_by
            self.shipped = True

    order = FakeOrder()

    class FakeShipmentManager:
        def get(self, pk):
            assert pk == 42
            return types.SimpleNamespace(order=order, checked_by=checked_by)

    fake_order_models = types.ModuleType("order.models")
    fake_order_models.SalesOrderShipment = types.SimpleNamespace(
        objects=FakeShipmentManager()
    )
    monkeypatch.setitem(sys.modules, "order.models", fake_order_models)

    plugin.sync_order_to_woocommerce = lambda order, shipment: None
    plugin.process_event("salesordershipment.completed", id=42)

    assert order.shipped is True


def test_sync_order_to_woocommerce_logs_and_sends_request(caplog):
    module = load_plugin_module()
    plugin = module.WooCommerceOrderSyncPlugin()
    calls = []

    class FakeResponse:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

    plugin.api_call = lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResponse()
    order = types.SimpleNamespace(reference="SO-741")
    shipment = types.SimpleNamespace(
        reference="1",
        tracking_number="TRACK-123",
    )

    with caplog.at_level("WARNING", logger=module.logger.name):
        plugin.sync_order_to_woocommerce(order, shipment)

    assert calls == [
        (
            ("orders/741",),
            {
                "method": "PUT",
                "json": {
                    "status": "completed",
                    "meta_data": [
                        {"key": "tracking_number", "value": "TRACK-123"}
                    ],
                },
                "url_args": None,
                "simple_response": False,
            },
        )
    ]
    assert "WooCommerce outgoing request:" in caplog.text
    assert "PUT https://test/orders/741" in caplog.text
    assert '"tracking_number", "value": "TRACK-123"' in caplog.text
    assert "Basic ********" in caplog.text
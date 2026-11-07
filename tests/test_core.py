import importlib
import sys
import types
from datetime import datetime, timezone

import pytest


def load_plugin_module():
    """Load the plugin with the small portion of InvenTree needed by this test."""
    plugin_module = types.ModuleType("plugin")
    plugin_module.InvenTreePlugin = type("InvenTreePlugin", (), {})
    mixins_module = types.ModuleType("plugin.mixins")
    mixins_module.APICallMixin = type("APICallMixin", (), {})
    mixins_module.ActionMixin = type("ActionMixin", (), {})
    mixins_module.EventMixin = type("EventMixin", (), {})
    mixins_module.ScheduleMixin = type("ScheduleMixin", (), {})
    mixins_module.UserInterfaceMixin = type("UserInterfaceMixin", (), {})
    mixins_module.UrlsMixin = type("UrlsMixin", (), {})

    class SettingsMixin:
        def get_setting(self, key, **kwargs):
            return "basic" if key == "WOOCOMMERCE_AUTH_METHOD" else "test"

    mixins_module.SettingsMixin = SettingsMixin
    django_module = types.ModuleType("django")
    django_http_module = types.ModuleType("django.http")
    django_http_module.HttpResponse = type("HttpResponse", (), {})
    django_http_module.HttpResponseForbidden = type("HttpResponseForbidden", (), {})
    django_urls_module = types.ModuleType("django.urls")
    django_urls_module.path = lambda *args, **kwargs: (args, kwargs)
    django_utils_module = types.ModuleType("django.utils")
    django_timezone_module = types.ModuleType("django.utils.timezone")
    django_timezone_module.now = lambda: datetime.now(timezone.utc)
    sys.modules["django"] = django_module
    sys.modules["django.http"] = django_http_module
    sys.modules["django.urls"] = django_urls_module
    sys.modules["django.utils"] = django_utils_module
    sys.modules["django.utils.timezone"] = django_timezone_module
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


def use_test_mapping_file(mapping_module, monkeypatch, tmp_path):
    """Keep mapping-file tests isolated from the host or Docker data directory."""
    monkeypatch.setattr(
        mapping_module, "LINE_ITEM_MAPPING_PATH", tmp_path / "line_item_mapping.json"
    )


def load_importer_module(monkeypatch, order_models, part_models):
    """Load the importer with minimal Django and InvenTree model stubs."""
    load_plugin_module()
    cache_module = types.ModuleType("django.core.cache")
    cache_module.cache = types.SimpleNamespace()
    database_module = types.ModuleType("django.db")
    database_module.transaction = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "django.core", types.ModuleType("django.core"))
    monkeypatch.setitem(sys.modules, "django.core.cache", cache_module)
    monkeypatch.setitem(sys.modules, "django.db", database_module)
    monkeypatch.setitem(sys.modules, "order.models", order_models)
    monkeypatch.setitem(sys.modules, "part.models", part_models)
    return importlib.import_module("inventree_woocommerce_sync.importer")


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
                "simple_response": False,
            },
        )
    ]
    assert "WooCommerce outgoing request:" in caplog.text
    assert "PUT https://test/orders/741" in caplog.text
    assert '"tracking_number", "value": "TRACK-123"' in caplog.text
    assert "Basic ********" in caplog.text


def test_scheduled_import_uses_configured_interval(monkeypatch):
    module = load_plugin_module()
    plugin = module.WooCommerceOrderSyncPlugin()
    calls = []
    plugin.get_setting = lambda key, **kwargs: 60
    monkeypatch.setattr(
        module.timezone,
        "now",
        lambda: datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
    )
    plugin.load_open_woocommerce_orders = lambda trigger: calls.append(trigger)

    plugin.scheduled_order_import()

    assert calls == ["schedule"]


def test_sales_order_ui_action_points_to_manual_import():
    module = load_plugin_module()
    plugin = module.WooCommerceOrderSyncPlugin()

    actions = plugin.get_ui_primary_actions(
        request=None,
        context={"target_model": "salesorder"},
    )

    assert actions[0]["key"] == "woocommerce-import-orders"
    assert actions[0]["options"]["url"].endswith("import-orders/")


def test_configured_line_item_mapping_inspects_every_order_line(
    monkeypatch, tmp_path
):
    load_plugin_module()
    mapping = importlib.import_module("inventree_woocommerce_sync.mapping")
    use_test_mapping_file(mapping, monkeypatch, tmp_path)

    items = mapping.build_line_items(
        [
            {
                "sku": "hpc-q",
                "quantity": 2,
                "price": "120.00",
                "meta_data": [
                    {"key": "Tweakers editie", "value": "Ja"},
                    {"key": "Bevestigingsbeugel", "value": "Ja"},
                    {"key": "Temperatuursensor", "value": "Ja-22"},
                ],
            },
            {
                "sku": "dallas_sensor",
                "quantity": 3,
                "price": "10.00",
                "meta_data": [],
            },
        ]
    )

    assert items == [
        {"part_sku": "hpcq_t_1.3", "quantity": 2, "sale_price": "120.00"},
        {"part_sku": "dupont-20cm", "quantity": 2},
        {"part_sku": "hpcq_wallholder", "quantity": 2},
        {"part_sku": "temperature_sensor", "quantity": 2},
        {"part_sku": "holder-ds18b20-22", "quantity": 2},
        {"part_sku": "thermalpaste", "quantity": 2},
        {"part_sku": "sensor", "quantity": 3, "sale_price": "10.00"},
    ]


def test_configured_line_item_mapping_handles_no_options_and_sensor_counts(
    monkeypatch, tmp_path
):
    load_plugin_module()
    mapping = importlib.import_module("inventree_woocommerce_sync.mapping")
    use_test_mapping_file(mapping, monkeypatch, tmp_path)

    items = mapping.build_line_items(
        [
            {
                "sku": "hpc-q",
                "quantity": 1,
                "price": "100",
                "meta_data": [
                    {"key": "Tweakers editie", "value": "Nee"},
                    {"key": "Temperatuursensor", "value": "Ja"},
                ],
            },
            {
                "sku": "itho-daalderop-amber-control-module",
                "quantity": 2,
                "price": "200",
                "meta_data": [
                    {"key": "Temperatuursensoren", "value": "2"},
                ],
            },
        ]
    )

    assert items == [
        {"part_sku": "hpcq_1.3", "quantity": 1, "sale_price": "100"},
        {"part_sku": "temperature_sensor", "quantity": 1},
        {"part_sku": "hpc_amber", "quantity": 2, "sale_price": "200"},
        {"part_sku": "sensor", "quantity": 4},
    ]


def test_configured_line_item_mapping_supports_28mm_sensor_holder(
    monkeypatch, tmp_path
):
    load_plugin_module()
    mapping = importlib.import_module("inventree_woocommerce_sync.mapping")
    use_test_mapping_file(mapping, monkeypatch, tmp_path)

    items = mapping.build_line_items(
        [
            {
                "sku": "hpc-q",
                "quantity": 1,
                "meta_data": [
                    {"key": "Tweakers editie", "value": "Nee"},
                    {"key": "Temperatuursensor", "value": "Ja-28"},
                ],
            }
        ]
    )

    assert [item["part_sku"] for item in items] == [
        "hpcq_1.3",
        "temperature_sensor",
        "holder-ds18b20-28",
        "thermalpaste",
    ]


def test_legacy_relay_skus_use_their_product_configuration(monkeypatch, tmp_path):
    load_plugin_module()
    mapping = importlib.import_module("inventree_woocommerce_sync.mapping")
    use_test_mapping_file(mapping, monkeypatch, tmp_path)

    items = mapping.build_line_items(
        [
            {
                "sku": "amber-control-module-2relay",
                "quantity": 1,
                "price": "80",
                "meta_data": [
                    {"key": "Aansluitset", "value": "Ja"},
                    {"key": "Temperatuursensoren", "value": "1"},
                ],
            }
        ]
    )

    assert items == [
        {
            "part_sku": "heatpump_module_2relay",
            "quantity": 1,
            "notes": "Amber",
            "sale_price": "80",
        },
        {
            "part_sku": "amber_connection_set",
            "quantity": 1,
        },
        {
            "part_sku": "temperature_sensor_2m",
            "quantity": 1,
        },
        {
            "part_sku": "connector_2pin",
            "quantity": 2,
        },
        {
            "part_sku": "connector_3pin",
            "quantity": 3,
        },
        {
            "part_sku": "verzenddoos_a5",
            "quantity": 1,
        },
    ]


def test_invalid_metadata_quantity_is_logged_and_not_added(
    caplog, monkeypatch, tmp_path
):
    load_plugin_module()
    mapping = importlib.import_module("inventree_woocommerce_sync.mapping")
    use_test_mapping_file(mapping, monkeypatch, tmp_path)

    with caplog.at_level("WARNING", logger=mapping.logger.name):
        items = mapping.build_line_items(
            [
                {
                    "sku": "itho-daalderop-amber-control-module",
                    "quantity": 1,
                    "meta_data": [
                        {"key": "Temperatuursensoren", "value": "two"},
                    ],
                }
            ]
        )

    assert [item["part_sku"] for item in items] == ["hpc_amber"]
    assert "Invalid integer metadata" in caplog.text


def test_line_item_mapping_is_reloaded_from_data_file_for_each_order(
    monkeypatch, tmp_path
):
    load_plugin_module()
    mapping = importlib.import_module("inventree_woocommerce_sync.mapping")
    use_test_mapping_file(mapping, monkeypatch, tmp_path)
    config_path = mapping.LINE_ITEM_MAPPING_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config_path.write_text(
        '{"products":{"example":{"rules":[{"add":[{"sku":"part-a","quantity":1}]}]}}}',
        encoding="utf-8",
    )
    order_line = [{"sku": "example", "quantity": 1}]
    assert mapping.build_line_items(order_line) == [
        {"part_sku": "part-a", "quantity": 1}
    ]

    config_path.write_text(
        '{"products":{"example":{"rules":[{"add":[{"sku":"part-b","quantity":3}]}]}}}',
        encoding="utf-8",
    )
    assert mapping.build_line_items(order_line) == [
        {"part_sku": "part-b", "quantity": 3}
    ]


def test_importer_combines_duplicate_parts_without_mixing_sale_prices(monkeypatch):
    load_plugin_module()

    cache_module = types.ModuleType("django.core.cache")
    cache_module.cache = types.SimpleNamespace()
    django_core_module = types.ModuleType("django.core")
    database_module = types.ModuleType("django.db")
    database_module.transaction = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "django.core", django_core_module)
    monkeypatch.setitem(sys.modules, "django.core.cache", cache_module)
    monkeypatch.setitem(sys.modules, "django.db", database_module)
    monkeypatch.setitem(sys.modules, "django.db.transaction", database_module.transaction)

    created_lines = []

    class SalesOrderLineItemManager:
        def filter(self, **kwargs):
            return types.SimpleNamespace(
                values_list=lambda *args, **options: []
            )

        def create(self, **kwargs):
            created_lines.append(kwargs)

    class PartManager:
        def filter(self, **kwargs):
            assert kwargs == {
                "IPN__iexact": "sensor",
                "salable": True,
                "active": True,
            }
            return types.SimpleNamespace(
                values_list=lambda *args, **options: [12]
            )

    order_models = types.ModuleType("order.models")
    order_models.SalesOrderLineItem = types.SimpleNamespace(
        objects=SalesOrderLineItemManager()
    )
    part_models = types.ModuleType("part.models")
    part_models.Part = types.SimpleNamespace(objects=PartManager())
    monkeypatch.setitem(sys.modules, "order.models", order_models)
    monkeypatch.setitem(sys.modules, "part.models", part_models)

    importer_module = importlib.import_module("inventree_woocommerce_sync.importer")
    monkeypatch.setattr(
        importer_module,
        "build_line_items",
        lambda lines: [
            {"part_sku": "sensor", "quantity": 2},
            {"part_sku": "sensor", "quantity": 3},
            {"part_sku": "sensor", "quantity": 1, "sale_price": "10.00"},
        ],
    )
    importer = importer_module.WooCommerceOrderImporter(plugin=None)
    order = object()
    importer.add_part_lines(order=order, woo_order={"line_items": []})

    assert created_lines == [
        {"order": order, "part_id": 12, "quantity": 5},
        {
            "order": order,
            "part_id": 12,
            "quantity": 1,
            "sale_price": "10.00",
        },
    ]


@pytest.mark.parametrize("matches", [[], [12, 13]])
def test_missing_or_ambiguous_part_logs_order_and_sku(monkeypatch, caplog, matches):
    order_models = types.ModuleType("order.models")
    order_models.SalesOrderLineItem = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(
                values_list=lambda *args, **kwargs: []
            )
        )
    )
    part_models = types.ModuleType("part.models")
    part_models.Part = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(
                values_list=lambda *args, **kwargs: matches
            )
        )
    )
    module = load_importer_module(monkeypatch, order_models, part_models)
    monkeypatch.setattr(
        module, "build_line_items", lambda lines: [{"part_sku": "S-123", "quantity": 2}]
    )

    with caplog.at_level("WARNING", logger=module.logger.name):
        module.WooCommerceOrderImporter(None).add_part_lines(
            types.SimpleNamespace(reference="SO-741"), {}
        )

    assert "SO-741" in caplog.text
    assert "S-123" in caplog.text
    assert "quantity=2" in caplog.text


def test_failed_part_creation_logs_details_and_propagates(monkeypatch, caplog):
    def fail_create(**kwargs):
        raise ValueError("invalid sale price")

    order_models = types.ModuleType("order.models")
    order_models.SalesOrderLineItem = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(
                values_list=lambda *args, **kwargs: []
            ),
            create=fail_create,
        )
    )
    part_models = types.ModuleType("part.models")
    part_models.Part = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(
                values_list=lambda *args, **kwargs: [12]
            )
        )
    )
    module = load_importer_module(monkeypatch, order_models, part_models)
    monkeypatch.setattr(
        module,
        "build_line_items",
        lambda lines: [
            {"part_sku": "S-123", "quantity": 2},
            {"part_sku": "s-123", "quantity": 3},
        ],
    )

    with caplog.at_level("WARNING", logger=module.logger.name):
        with pytest.raises(ValueError, match="invalid sale price"):
            module.WooCommerceOrderImporter(None).add_part_lines(
                types.SimpleNamespace(reference="SO-741"), {}
            )

    assert "SO-741" in caplog.text
    assert "S-123" in caplog.text
    assert "s-123" in caplog.text
    assert "quantity=5" in caplog.text
    assert "invalid sale price" in caplog.text


@pytest.mark.parametrize("failing_lookup", ["existing", "part"])
def test_failed_part_lookup_logs_details_and_propagates(monkeypatch, caplog, failing_lookup):
    def fail_lookup(**kwargs):
        raise ValueError("database unavailable")

    def existing_lines(**kwargs):
        return types.SimpleNamespace(values_list=lambda *args, **kwargs: [])

    order_models = types.ModuleType("order.models")
    order_models.SalesOrderLineItem = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=fail_lookup if failing_lookup == "existing" else existing_lines
        )
    )
    part_models = types.ModuleType("part.models")
    part_models.Part = types.SimpleNamespace(
        objects=types.SimpleNamespace(filter=fail_lookup)
    )
    module = load_importer_module(monkeypatch, order_models, part_models)
    monkeypatch.setattr(
        module, "build_line_items", lambda lines: [{"part_sku": "S-123", "quantity": 2}]
    )

    with caplog.at_level("WARNING", logger=module.logger.name):
        with pytest.raises(ValueError, match="database unavailable"):
            module.WooCommerceOrderImporter(None).add_part_lines(
                types.SimpleNamespace(reference="SO-741"), {}
            )

    assert "SO-741" in caplog.text
    assert "S-123" in caplog.text
    assert "quantity=2" in caplog.text
    assert "database unavailable" in caplog.text


def test_existing_part_is_skipped_without_warning(monkeypatch, caplog):
    order_models = types.ModuleType("order.models")
    order_models.SalesOrderLineItem = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(
                values_list=lambda *args, **kwargs: [12]
            )
        )
    )
    part_models = types.ModuleType("part.models")
    part_models.Part = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(
                values_list=lambda *args, **kwargs: [12]
            )
        )
    )
    module = load_importer_module(monkeypatch, order_models, part_models)
    monkeypatch.setattr(
        module, "build_line_items", lambda lines: [{"part_sku": "S-123", "quantity": 2}]
    )

    with caplog.at_level("WARNING", logger=module.logger.name):
        module.WooCommerceOrderImporter(None).add_part_lines(
            types.SimpleNamespace(reference="SO-741"), {}
        )

    assert not caplog.records


def test_failed_shipping_creation_logs_details_and_propagates(monkeypatch, caplog):
    def fail_create(**kwargs):
        raise ValueError("invalid price")

    order_models = types.ModuleType("order.models")
    order_models.SalesOrderExtraLine = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(exists=lambda: False),
            create=fail_create,
        )
    )
    module = load_importer_module(monkeypatch, order_models, types.ModuleType("part.models"))

    with caplog.at_level("WARNING", logger=module.logger.name):
        with pytest.raises(ValueError, match="invalid price"):
            module.WooCommerceOrderImporter(None).add_shipping_line(
                types.SimpleNamespace(reference="SO-741"),
                {"shipping_lines": [{"total": "5.00"}]},
            )

    assert "SO-741" in caplog.text
    assert "Verzendkosten" in caplog.text
    assert "5.00" in caplog.text
    assert "quantity=1" in caplog.text
    assert "invalid price" in caplog.text


@pytest.mark.parametrize("required", [True, False])
def test_unsupported_metadata_reports_structured_warning_and_keeps_mapping_behavior(
    caplog, required
):
    load_plugin_module()
    from inventree_woocommerce_sync import mapping

    product = {
        "allowed_metadata_values": {"Option": ["Yes", "No"]},
        "rules": [{"add": [{"sku": "part", "quantity": 1}]}],
    }
    if required:
        product["required_metadata"] = ["Option"]
    warnings = []
    with caplog.at_level("WARNING", logger=mapping.logger.name):
        result = mapping._build_configured_line_items(
            {
                "sku": "HPC-Q",
                "quantity": 1,
                "meta_data": [{"key": "Option", "value": "Unexpected"}],
            },
            product,
            warnings.append,
        )

    assert warnings == [mapping.MetadataWarning("hpc-q", "Option", "Unexpected")]
    assert "Unsupported metadata 'Option'" in caplog.text
    assert result == ([] if required else [{"part_sku": "part", "quantity": 1}])


def test_metadata_notifications_target_active_users_and_deduplicate(monkeypatch):
    load_plugin_module()
    from inventree_woocommerce_sync.mapping import MetadataWarning
    from inventree_woocommerce_sync import notifications

    sent = set()
    calls = []
    users = [object(), object()]

    class Entries:
        def filter(self, **kwargs):
            return types.SimpleNamespace(
                exists=lambda: (kwargs["key"], kwargs["uid"]) in sent
            )

    def trigger(target, **kwargs):
        calls.append((target, kwargs))
        sent.add((kwargs["category"], kwargs["notification_uid"]))

    auth_module = types.ModuleType("django.contrib.auth")
    auth_module.get_user_model = lambda: types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: users if kwargs == {"is_active": True} else []
        )
    )
    common_models = types.ModuleType("common.models")
    common_models.NotificationEntry = types.SimpleNamespace(objects=Entries())
    common_notifications = types.ModuleType("common.notifications")
    common_notifications.trigger_notification = trigger
    monkeypatch.setitem(sys.modules, "django.contrib", types.ModuleType("django.contrib"))
    monkeypatch.setitem(sys.modules, "django.contrib.auth", auth_module)
    monkeypatch.setitem(sys.modules, "common", types.ModuleType("common"))
    monkeypatch.setitem(sys.modules, "common.models", common_models)
    monkeypatch.setitem(sys.modules, "common.notifications", common_notifications)

    first = MetadataWarning("hpc-q", "Option", "Unexpected")
    second = MetadataWarning("hpc-q", "Option", "Other")
    notifications.notify_metadata_warnings("SO-12", [first, first, second])
    notifications.notify_metadata_warnings("SO-12", [first])
    notifications.notify_metadata_warnings("SO-13", [first])

    assert len(calls) == 3
    target, payload = calls[0]
    assert target is None
    assert payload["targets"] == users
    assert payload["delivery_methods"] == ["inventree-ui-notification"]
    assert payload["check_recent"] is False
    assert all(
        text in payload["context"]["message"]
        for text in ("SO-12", "hpc-q", "Option", "Unexpected")
    )
    assert len({call[1]["notification_uid"] for call in calls}) == 3


def test_metadata_notification_failure_is_logged(monkeypatch, caplog):
    load_plugin_module()
    from inventree_woocommerce_sync.mapping import MetadataWarning
    from inventree_woocommerce_sync import notifications

    auth_module = types.ModuleType("django.contrib.auth")
    auth_module.get_user_model = lambda: types.SimpleNamespace(
        objects=types.SimpleNamespace(filter=lambda **kwargs: [object()])
    )
    models_module = types.ModuleType("common.models")
    models_module.NotificationEntry = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(exists=lambda: False)
        )
    )
    notification_module = types.ModuleType("common.notifications")
    notification_module.trigger_notification = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "django.contrib", types.ModuleType("django.contrib"))
    monkeypatch.setitem(sys.modules, "django.contrib.auth", auth_module)
    monkeypatch.setitem(sys.modules, "common", types.ModuleType("common"))
    monkeypatch.setitem(sys.modules, "common.models", models_module)
    monkeypatch.setitem(sys.modules, "common.notifications", notification_module)

    with caplog.at_level("ERROR", logger=notifications.logger.name):
        notifications.notify_metadata_warnings(
            "SO-12", [MetadataWarning("hpc-q", "Option", "Unexpected")]
        )

    assert "not delivered" in caplog.text
    assert "SO-12" in caplog.text


def test_metadata_notifications_sent_after_order_transaction_rolls_back(monkeypatch):
    from contextlib import contextmanager

    order_models = types.ModuleType("order.models")
    order = types.SimpleNamespace(reference="SO-12")
    order_models.SalesOrder = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(first=lambda: None),
            create=lambda **kwargs: order,
        )
    )
    module = load_importer_module(
        monkeypatch, order_models, types.ModuleType("part.models")
    )
    from inventree_woocommerce_sync import notifications

    state = {"inside_transaction": False, "notifications": []}

    @contextmanager
    def atomic():
        state["inside_transaction"] = True
        try:
            yield
        finally:
            state["inside_transaction"] = False

    monkeypatch.setattr(module.transaction, "atomic", atomic, raising=False)
    status_module = sys.modules["order.status_codes"]
    monkeypatch.setattr(
        status_module.SalesOrderStatus,
        "PENDING",
        types.SimpleNamespace(value=10),
        raising=False,
    )
    importer = module.WooCommerceOrderImporter(None)
    monkeypatch.setattr(importer, "find_or_create_customer", lambda *args: (None, None))
    monkeypatch.setattr(importer, "add_shipping_line", lambda *args: None)

    def fail_add(order, woo_order, warnings):
        warnings.append(module.MetadataWarning("hpc-q", "Option", "Unexpected"))
        raise ValueError("order failed")

    def notify(reference, warnings):
        assert not state["inside_transaction"]
        state["notifications"].append((reference, list(warnings)))

    monkeypatch.setattr(importer, "add_part_lines", fail_add)
    monkeypatch.setattr(notifications, "notify_metadata_warnings", notify)
    with pytest.raises(ValueError, match="order failed"):
        importer.process_order(
            {
                "number": 12,
                "billing": {
                    "first_name": "A",
                    "email": "a@example.test",
                    "postcode": "1234",
                    "country": "NL",
                },
            }
        )

    assert state["notifications"] == [
        ("SO-12", [module.MetadataWarning("hpc-q", "Option", "Unexpected")])
    ]


@pytest.mark.parametrize("value, expects_warning", [("Unexpected", True), ("Yes", False)])
def test_order_import_notifies_only_for_invalid_metadata(monkeypatch, value, expects_warning):
    from contextlib import nullcontext

    order = types.SimpleNamespace(reference="SO-12", issue_order=lambda: None)
    order_models = types.ModuleType("order.models")
    order_models.SalesOrder = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(first=lambda: None),
            create=lambda **kwargs: order,
        )
    )
    created = []
    order_models.SalesOrderLineItem = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(
                values_list=lambda *args, **kwargs: []
            ),
            create=lambda **kwargs: created.append(kwargs),
        )
    )
    part_models = types.ModuleType("part.models")
    part_models.Part = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(
                values_list=lambda *args, **kwargs: [12]
            )
        )
    )
    module = load_importer_module(monkeypatch, order_models, part_models)
    from inventree_woocommerce_sync import mapping, notifications

    monkeypatch.setattr(
        mapping,
        "load_line_item_mapping",
        lambda: {
            "products": {
                "hpc-q": {
                    "allowed_metadata_values": {"Option": ["Yes", "No"]},
                    "rules": [{"add": [{"sku": "part"}]}],
                }
            }
        },
    )
    monkeypatch.setattr(module.transaction, "atomic", nullcontext, raising=False)
    importer = module.WooCommerceOrderImporter(None)
    monkeypatch.setattr(importer, "find_or_create_customer", lambda *args: (None, None))
    monkeypatch.setattr(importer, "add_shipping_line", lambda *args: None)
    monkeypatch.setattr(importer, "allocate_stock", lambda *args: None)
    delivered = []
    monkeypatch.setattr(
        notifications,
        "notify_metadata_warnings",
        lambda reference, warnings: delivered.append((reference, list(warnings))),
    )
    result = importer.process_order(
        {
            "number": 12,
            "billing": {
                "first_name": "A",
                "email": "a@example.test",
                "postcode": "1234",
                "country": "NL",
            },
            "line_items": [
                {
                    "sku": "hpc-q",
                    "quantity": 1,
                    "meta_data": [{"key": "Option", "value": value}],
                }
            ],
        }
    )

    assert result == "created"
    assert len(created) == 1
    expected = [("SO-12", [module.MetadataWarning("hpc-q", "Option", value)])]
    assert delivered == (expected if expects_warning else [])
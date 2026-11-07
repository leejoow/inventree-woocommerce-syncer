import json
from pathlib import Path

import jsonschema
import pytest


SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "inventree_woocommerce_sync"
    / "line_item_mapping.schema.json"
)


@pytest.fixture(scope="module")
def validator():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft7Validator.check_schema(schema)
    return jsonschema.Draft7Validator(schema)


def mapping_with_rule(rule):
    return {"products": {"hpc-q": {"rules": [rule]}}}


def test_schema_accepts_supported_conditions_and_nested_expressions(validator):
    config = {
        "$schema": "./line_item_mapping.schema.json",
        "products": {
            "hpc-q": {
                "variables": {"count": 2},
                "required_metadata": ["Tweakers editie"],
                "allowed_metadata_values": {"Tweakers editie": ["Ja", "Nee"]},
                "rules": [
                    {
                        "when": {
                            "all": [
                                {"metadata": "Tweakers editie", "equals": "Ja"},
                                {
                                    "any": [
                                        {"metadata": "Optie", "in": ["Ja", "Yes"]},
                                        {
                                            "not": {
                                                "metadata_int": "Sensors",
                                                "greater_than": 0,
                                            }
                                        },
                                    ]
                                },
                            ]
                        },
                        "add": [
                            {
                                "sku": "part",
                                "price_from_order_line": True,
                                "notes": {"metadata": "softwareversie"},
                                "quantity": {
                                    "if": {
                                        "condition": {
                                            "metadata_int": "Sensors",
                                            "greater_than": 0,
                                        },
                                        "then": {
                                            "add": [
                                                {"metadata_int": "Sensors"},
                                                {"variable": "count"},
                                            ]
                                        },
                                        "else": {
                                            "subtract": [
                                                0,
                                                {
                                                    "metadata_int": {
                                                        "key": "Sensors",
                                                        "default": 0,
                                                    }
                                                },
                                            ]
                                        },
                                    }
                                },
                            },
                            {"sku": "verzenddoos-a6", "notes": "fixed"},
                        ],
                    }
                ],
            }
        },
    }

    assert list(validator.iter_errors(config)) == []


@pytest.mark.parametrize(
    "rule",
    [
        {"sku": "verzenddoos-a6", "quantity": 1},
        {"add": []},
        {"add": [{"sku": " "}]},
        {"add": [{"sku": "part", "qty": 1}]},
        {"add": [{"sku": "part", "quantity": True}]},
        {"add": [{"sku": "part", "quantity": {"subtract": [1]}}]},
        {"add": [{"sku": "part", "quantity": {"add": []}}]},
        {"add": [{"sku": "part", "quantity": {"variable": "x", "add": [1]}}]},
        {
            "add": [
                {
                    "sku": "part",
                    "quantity": {
                        "if": {
                            "condition": {"metadata": "key", "equals": "value"},
                            "then": 1,
                        }
                    },
                }
            ]
        },
        {"when": {"metadata": "key"}, "add": [{"sku": "part"}]},
        {
            "when": {"metadata": "key", "equals": "a", "in": ["a"]},
            "add": [{"sku": "part"}],
        },
        {"when": {}, "add": [{"sku": "part"}]},
    ],
)
def test_schema_rejects_invalid_rules(validator, rule):
    assert not validator.is_valid(mapping_with_rule(rule))


def test_schema_accepts_unconditional_shipping_box(validator):
    config = mapping_with_rule({"add": [{"sku": "verzenddoos-a6", "quantity": 1}]})

    assert validator.is_valid(config)

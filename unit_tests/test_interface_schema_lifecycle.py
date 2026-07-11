"""Schema-driven interface lifecycle (IFNAME в nested полях)."""

import unittest

from main import (
    _resolve_interface_lifecycle_by_value,
    _value_matches_interface_lifecycle_schema,
    parse_interface_lifecycle_config,
)

IFNAME_PATTERN = (
    r"^((eth(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3}))"
    r"|(vlan([0-9]|[1-9][0-9]{1,2}|[1-3][0-9]{3}|40[0-8][0-9]|409[0-5]))"
    r"|([A-Za-uw-z][A-Za-z0-9_\-]{0,30}[A-Za-z](0|[1-9][0-9]{0,3})))$"
)
IP_PATTERN = (
    r"^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])\.){3}"
    r"(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])$"
)

COMPONENTS = {
    "schemas": {
        "IFNAME": {"type": "string", "pattern": IFNAME_PATTERN},
        "IP_ADDR": {"type": "string", "pattern": IP_PATTERN},
    },
}


class ParseInterfaceLifecycleConfigTests(unittest.TestCase):
    def test_defaults_when_section_missing(self):
        cfg = parse_interface_lifecycle_config({})
        self.assertEqual(cfg["schema_components"], ["IFNAME"])
        self.assertEqual(cfg["rules_key"], "ifname")

    def test_disabled(self):
        cfg = parse_interface_lifecycle_config({"interface_lifecycle": {"enabled": False}})
        self.assertIsNone(cfg)


class InterfaceSchemaMatchTests(unittest.TestCase):
    def setUp(self):
        self.lifecycle = parse_interface_lifecycle_config({
            "interface_lifecycle": {
                "schema_components": ["IFNAME"],
                "rules_key": "ifname",
            },
        })

    def test_anyof_ifname_branch(self):
        schema = {
            "anyOf": [
                {"$ref": "#/components/schemas/IP_ADDR"},
                {"$ref": "#/components/schemas/IFNAME"},
            ],
        }
        self.assertTrue(
            _value_matches_interface_lifecycle_schema(
                "vlan100", schema, COMPONENTS, self.lifecycle,
            ),
        )
        self.assertFalse(
            _value_matches_interface_lifecycle_schema(
                "10.0.0.1", schema, COMPONENTS, self.lifecycle,
            ),
        )

    def test_inline_ifname_pattern_after_mock_inventory(self):
        """После apply_mock_data/inventory $ref заменяется на inline pattern."""
        schema = {
            "anyOf": [
                {
                    "type": "string",
                    "pattern": IP_PATTERN,
                    "enum": ["10.0.0.1"],
                },
                {
                    "type": "string",
                    "pattern": IFNAME_PATTERN,
                    "enum": ["vlan100", "vlan200"],
                },
            ],
        }
        self.assertTrue(
            _value_matches_interface_lifecycle_schema(
                "vlan200", schema, COMPONENTS, self.lifecycle,
            ),
        )
        self.assertFalse(
            _value_matches_interface_lifecycle_schema(
                "10.0.0.1", schema, COMPONENTS, self.lifecycle,
            ),
        )

    def test_inline_pattern_not_ifname_component(self):
        schema = {"type": "string", "pattern": r"^[A-Za-z0-9_\-]{1,17}$"}
        self.assertFalse(
            _value_matches_interface_lifecycle_schema(
                "vlan100", schema, COMPONENTS, self.lifecycle,
            ),
        )

    def test_resolve_vlan_lifecycle_by_value(self):
        rules = {
            "rules": [
                {
                    "prefix": "vlan",
                    "setup": {"endpoint": "/interfaces/vlan/add"},
                    "teardown": [{"endpoint": "/interfaces/vlan/delete"}],
                },
            ],
        }
        lifecycle = _resolve_interface_lifecycle_by_value("vlan100", rules)
        self.assertIsNotNone(lifecycle)
        self.assertEqual(lifecycle["setup"]["endpoint"], "/interfaces/vlan/add")


if __name__ == "__main__":
    unittest.main()

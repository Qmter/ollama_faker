"""mock_data из dependencies.json."""

import unittest

from main import (
    apply_mock_data,
    apply_interface_inventory,
    build_mock_pattern_index,
    collect_test_values,
    parse_mock_data_config,
)


class ParseMockDataTests(unittest.TestCase):
    def test_missing_section_returns_none(self):
        self.assertIsNone(parse_mock_data_config({"field_mappings": {}}))

    def test_empty_section_returns_none(self):
        self.assertIsNone(parse_mock_data_config({"mock_data": {}}))

    def test_parses_schema_and_field_lists(self):
        config = parse_mock_data_config({
            "mock_data": {
                "by_schema": {"IP_ADDR": ["10.0.0.1", "10.0.0.2"]},
                "by_field": {"vrf_name": "mgmt,lab"},
            },
        })
        self.assertEqual(config["by_schema"]["IP_ADDR"], ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(config["by_field"]["vrf_name"], ["mgmt", "lab"])


class ApplyMockDataTests(unittest.TestCase):
    def test_by_schema_sets_enum_on_matching_pattern(self):
        ip_pattern = (
            r"^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])\.){3}"
            r"(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])$"
        )
        mock_config = parse_mock_data_config({
            "mock_data": {"by_schema": {"IP_ADDR": ["10.0.0.1"]}},
        })
        pattern_index = {ip_pattern: ["10.0.0.1"]}
        schema = {
            "type": "object",
            "properties": {
                "ip_addr": {"type": "string", "pattern": ip_pattern},
            },
        }
        result = apply_mock_data(schema, mock_config, pattern_index)
        self.assertEqual(result["properties"]["ip_addr"]["enum"], ["10.0.0.1"])

    def test_by_field_sets_enum_on_property_name(self):
        mock_config = parse_mock_data_config({
            "mock_data": {"by_field": {"vrf_name": ["autotest-vrf-1"]}},
        })
        schema = {
            "type": "object",
            "properties": {
                "vrf_name": {"type": "string", "pattern": "^[a-z]+$"},
            },
        }
        result = apply_mock_data(schema, mock_config, {})
        self.assertEqual(result["properties"]["vrf_name"]["enum"], ["autotest-vrf-1"])

    def test_by_field_overrides_existing_enum(self):
        mock_config = parse_mock_data_config({
            "mock_data": {"by_field": {"ifname": ["eth99"]}},
        })
        schema = {
            "type": "object",
            "properties": {
                "ifname": {"type": "string", "enum": ["eth1"]},
            },
        }
        result = apply_mock_data(schema, mock_config, {})
        self.assertEqual(result["properties"]["ifname"]["enum"], ["eth99"])

    def test_by_field_overrides_interface_inventory(self):
        ifname_pattern = (
            r"^((eth(0|[1-9][0-9]{0,3})\\.(0|[1-9][0-9]{0,3}))"
            r"|(vlan([0-9]|[1-9][0-9]{1,2}|[1-3][0-9]{3}|40[0-8][0-9]|409[0-5]))"
            r"|([A-Za-uw-z][A-Za-z0-9_\\-]{0,30}[A-Za-z](0|[1-9][0-9]{0,3})))$"
        )
        inventory = [{
            "names": ["vlan100", "vlan200"],
            "prefix": "vlan",
            "schema_patterns": [ifname_pattern],
        }]
        schema = {
            "type": "object",
            "properties": {
                "vrf_name": {
                    "type": "string",
                    "pattern": "^[A-Za-z0-9_-]{1,12}$",
                },
                "ifname": {
                    "type": "string",
                    "pattern": ifname_pattern,
                },
            },
        }
        with_inventory = apply_interface_inventory(schema, inventory)
        self.assertNotIn("enum", with_inventory["properties"]["vrf_name"])
        self.assertEqual(
            with_inventory["properties"]["ifname"]["enum"],
            ["vlan100", "vlan200"],
        )
        mock_config = parse_mock_data_config({
            "mock_data": {"by_field": {"vrf_name": ["test_vrf_1", "test_vrf_2"]}},
        })
        result = apply_mock_data(with_inventory, mock_config, {})
        self.assertEqual(
            result["properties"]["vrf_name"]["enum"],
            ["test_vrf_1", "test_vrf_2"],
        )

    def test_collect_test_values_uses_mock_enum(self):
        ip_pattern = (
            r"^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])\.){3}"
            r"(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])$"
        )
        mock_config = parse_mock_data_config({
            "mock_data": {"by_schema": {"IP_ADDR": ["10.0.0.5"]}},
        })
        schema = apply_mock_data(
            {"type": "string", "pattern": ip_pattern},
            mock_config,
            {ip_pattern: ["10.0.0.5"]},
        )
        values = collect_test_values(schema)
        self.assertEqual(values, ["10.0.0.5"])


class PatternIndexTests(unittest.TestCase):
    def test_builds_index_from_openapi_component(self):
        mock_config = parse_mock_data_config({
            "mock_data": {"by_schema": {"IP_ADDR": ["10.0.0.1"]}},
        })
        components = {
            "schemas": {
                "IP_ADDR": {
                    "type": "string",
                    "pattern": "^10\\.0\\.0\\.1$",
                },
            },
        }
        index = build_mock_pattern_index(components, mock_config)
        self.assertEqual(index["^10\\.0\\.0\\.1$"], ["10.0.0.1"])


if __name__ == "__main__":
    unittest.main()

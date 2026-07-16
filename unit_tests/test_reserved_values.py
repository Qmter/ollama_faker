"""reserved_values: запрет имён/VID при генерации тестов."""

import unittest

from main import (
    PayloadCoverage,
    apply_interface_inventory,
    apply_reserved_values_to_schema,
    build_interface_inventory,
    filter_reserved_values,
    generate_value_coverage_payloads,
    is_reserved_field_value,
    parse_reserved_values,
    synchronize_vid_ifname,
    _parse_vid_pool,
)


class ParseReservedValuesTests(unittest.TestCase):
    def test_missing_returns_none(self):
        self.assertIsNone(parse_reserved_values({}))

    def test_parses_by_field(self):
        reserved = parse_reserved_values({
            "reserved_values": {
                "by_field": {
                    "ifname": ["vlan1", "switchport1"],
                    "vid": [0, 1, 603, 4095],
                },
            },
        })
        self.assertIsNotNone(reserved)
        self.assertTrue(is_reserved_field_value("ifname", "vlan1", reserved))
        self.assertTrue(is_reserved_field_value("vid", 603, reserved))
        self.assertTrue(is_reserved_field_value("vid", "603", reserved))
        self.assertFalse(is_reserved_field_value("ifname", "vlan100", reserved))

    def test_env_merges_into_config(self):
        reserved = parse_reserved_values(
            {"reserved_values": {"by_field": {"ifname": ["vlan1"]}}},
            {"RESERVED_IFNAMES": "vlan603,switchport1", "RESERVED_VIDS": "0,4095"},
        )
        self.assertTrue(is_reserved_field_value("ifname", "vlan1", reserved))
        self.assertTrue(is_reserved_field_value("ifname", "vlan603", reserved))
        self.assertTrue(is_reserved_field_value("vid", 4095, reserved))
        self.assertTrue(is_reserved_field_value("vlan", "0", reserved))


class ReservedInferenceTests(unittest.TestCase):
    def test_vlan_name_blocked_via_reserved_vid(self):
        reserved = parse_reserved_values({
            "reserved_values": {"by_field": {"vid": [603]}},
        })
        self.assertTrue(is_reserved_field_value("ifname", "vlan603", reserved))
        self.assertTrue(is_reserved_field_value("ifname", "eth1.603", reserved))
        self.assertFalse(is_reserved_field_value("ifname", "vlan100", reserved))

    def test_filter_list(self):
        reserved = parse_reserved_values({
            "reserved_values": {"by_field": {"vid": [1, 4095]}},
        })
        self.assertEqual(
            filter_reserved_values([1, 100, 4094, 4095], "vid", reserved),
            [100, 4094],
        )


class InventoryReservedTests(unittest.TestCase):
    def test_build_inventory_filters_reserved(self):
        deps = {
            "interface_rules": {
                "ifname": {
                    "rules": [{
                        "prefix": "vlan",
                        "env": "DEVICE_VLAN_IFNAMES",
                    }],
                },
            },
        }
        reserved = parse_reserved_values({
            "reserved_values": {
                "by_field": {"ifname": ["vlan1"], "vid": [603]},
            },
        })
        inventory = build_interface_inventory(
            deps,
            {"DEVICE_VLAN_IFNAMES": "vlan1,vlan100,vlan603,vlan200"},
            reserved=reserved,
        )
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["names"], ["vlan100", "vlan200"])

    def test_apply_inventory_filters_reserved(self):
        pattern = r"^vlan([0-9]+)$"
        inventory = [{"pattern": pattern, "names": ["vlan1", "vlan100", "vlan4095"]}]
        reserved = parse_reserved_values({
            "reserved_values": {
                "by_field": {
                    "ifname": ["vlan1", "vlan4095"],
                    "vid": [0, 4095],
                },
            },
        })
        schema = {
            "type": "object",
            "properties": {
                "ifname": {"type": "string", "pattern": pattern},
            },
        }
        result = apply_interface_inventory(schema, inventory, reserved=reserved)
        self.assertEqual(result["properties"]["ifname"]["enum"], ["vlan100"])


class SchemaAndCoverageReservedTests(unittest.TestCase):
    def test_apply_reserved_to_schema_filters_enum(self):
        reserved = parse_reserved_values({
            "reserved_values": {"by_field": {"vid": [1, 4095]}},
        })
        schema = {
            "type": "object",
            "properties": {
                "vid": {"type": "integer", "enum": [1, 100, 4094, 4095]},
            },
        }
        result = apply_reserved_values_to_schema(schema, reserved)
        self.assertEqual(result["properties"]["vid"]["enum"], [100, 4094])

    def test_coverage_skips_reserved_bounds(self):
        reserved = parse_reserved_values({
            "reserved_values": {"by_field": {"vid": [1]}},
        })
        schema = {
            "type": "object",
            "required": ["vid"],
            "properties": {
                "vid": {"type": "integer", "minimum": 1, "maximum": 4094},
            },
        }
        records = generate_value_coverage_payloads(schema, reserved=reserved)
        vids = {
            record.payload.get("vid")
            for record in records
            if isinstance(record.payload.get("vid"), int)
        }
        self.assertNotIn(1, vids)
        self.assertTrue(vids)


class SyncAndPoolReservedTests(unittest.TestCase):
    def test_sync_skips_reserved_vid(self):
        reserved = parse_reserved_values({
            "reserved_values": {"by_field": {"vid": [4095]}},
        })
        schema = {
            "type": "object",
            "required": ["ifname", "vid"],
            "properties": {
                "ifname": {"type": "string"},
                "vid": {"type": "integer"},
            },
        }
        synced = synchronize_vid_ifname(
            {"ifname": "vlan4095", "vid": 100},
            schema,
            reserved=reserved,
        )
        self.assertEqual(synced["vid"], 100)

    def test_vid_pool_excludes_reserved(self):
        reserved = parse_reserved_values({
            "reserved_values": {"by_field": {"vid": [3]}},
        })
        self.assertEqual(
            _parse_vid_pool({"VID_RANGE": "2-4"}, reserved=reserved),
            [2, 4],
        )


if __name__ == "__main__":
    unittest.main()

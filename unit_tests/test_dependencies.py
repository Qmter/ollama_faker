"""Поиск зависимостей в payload, endpoint_rules."""

import unittest

from main import (
    _get_endpoint_rules,
    scan_payload_for_dependencies,
)


class ScanDependenciesTests(unittest.TestCase):
    def test_finds_top_level_field(self):
        dep_map = {
            "vrf_name": {"setup": {"endpoint": "/vrf"}},
        }
        found = scan_payload_for_dependencies(
            {"ifname": "eth1", "vrf_name": "mgmt"},
            dep_map,
        )
        self.assertIn("vrf_name", found)
        self.assertEqual(found["vrf_name"]["value"], "mgmt")

    def test_finds_nested_field(self):
        dep_map = {
            "primary_interface": {"setup": []},
        }
        found = scan_payload_for_dependencies(
            {
                "ifname": "bond0",
                "mode": {"primary_interface": "eth1"},
            },
            dep_map,
        )
        paths = list(found.keys())
        self.assertTrue(any("primary_interface" in p for p in paths))

    def test_ignores_unknown_fields(self):
        dep_map = {"vrf_name": {}}
        found = scan_payload_for_dependencies({"ifname": "eth1"}, dep_map)
        self.assertEqual(found, {})


class EndpointRulesTests(unittest.TestCase):
    def test_get_endpoint_rules_exact_match(self):
        rules = {
            "/interfaces/switchport/vlandb": {"bind_fields": ["vlan"]},
        }
        result = _get_endpoint_rules("/interfaces/switchport/vlandb", rules)
        self.assertIsNotNone(result)
        self.assertIn("bind_fields", result)

    def test_get_endpoint_rules_missing(self):
        self.assertIsNone(_get_endpoint_rules("/unknown", {}))


if __name__ == "__main__":
    unittest.main()

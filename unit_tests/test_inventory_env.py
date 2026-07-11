"""Инвентарь интерфейсов, .env, interface_rules."""

import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from main import (
    _inventory_matches_schema_pattern,
    _matches_interface_prefix,
    _normalize_interface_rules,
    _resolve_allowed_names,
    _resolve_auto_interface,
    apply_interface_inventory,
    build_interface_inventory,
    load_env_file,
)


class LoadEnvTests(unittest.TestCase):
    def test_parse_env_file(self):
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text(
                'DEVICE_ETH_IFNAMES=eth0,eth1\n# comment\nAPI_BASE_URL=http://x\n',
                encoding="utf-8",
            )
            env = load_env_file(str(env_path))
            self.assertEqual(env["DEVICE_ETH_IFNAMES"], "eth0,eth1")
            self.assertEqual(env["API_BASE_URL"], "http://x")
            self.assertNotIn("comment", env)

    def test_missing_env_returns_empty(self):
        self.assertEqual(load_env_file("/nonexistent/.env"), {})


class InterfacePrefixTests(unittest.TestCase):
    def test_bond_prefix(self):
        self.assertTrue(_matches_interface_prefix("bond0", "bond"))
        self.assertTrue(_matches_interface_prefix("bond12", "bond"))
        self.assertFalse(_matches_interface_prefix("bonding", "bond"))

    def test_vlan_prefix(self):
        self.assertTrue(_matches_interface_prefix("vlan100", "vlan"))
        self.assertTrue(_matches_interface_prefix("vlan", "vlan"))


class InterfaceRulesTests(unittest.TestCase):
    def test_normalize_rules_format(self):
        rules = _normalize_interface_rules({
            "rules": [{"prefix": "bond", "create": "/bond/add"}],
        })
        self.assertEqual(rules[0]["prefix"], "bond")

    def test_resolve_auto_interface_by_prefix(self):
        iface_rules = {
            "ifname": {
                "rules": [
                    {
                        "prefix": "bond",
                        "create": "/interfaces/bonding/add",
                        "delete": "/interfaces/bonding/delete",
                    },
                ],
            },
        }
        lifecycle = _resolve_auto_interface("ifname", "bond0", iface_rules)
        self.assertEqual(lifecycle["create"], "/interfaces/bonding/add")
        self.assertEqual(lifecycle["delete"], "/interfaces/bonding/delete")

    def test_resolve_auto_interface_physical_skip(self):
        iface_rules = {
            "ifname": {
                "rules": [{"pattern": r"^eth1$", "physical": True}],
            },
        }
        lifecycle = _resolve_auto_interface("ifname", "eth1", iface_rules)
        self.assertTrue(lifecycle.get("physical"))


class InterfaceInventoryTests(unittest.TestCase):
    def test_build_inventory_from_env(self):
        dependencies = {
            "interface_rules": {
                "ifname": {
                    "rules": [
                        {
                            "pattern": r"^eth(0|[1-9][0-9]{0,3})$",
                            "env": "DEVICE_ETH_IFNAMES",
                        },
                    ],
                },
            },
        }
        env = {"DEVICE_ETH_IFNAMES": "eth0,eth1"}
        inventory = build_interface_inventory(dependencies, env)
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["names"], ["eth0", "eth1"])

    def test_apply_inventory_sets_enum(self):
        inventory = [{
            "pattern": r"^eth(0|[1-9][0-9]{0,3})$",
            "names": ["eth1"],
        }]
        schema = {
            "type": "object",
            "properties": {
                "ifname": {
                    "type": "string",
                    "pattern": r"^eth(0|[1-9][0-9]{0,3})$",
                },
            },
        }
        result = apply_interface_inventory(schema, inventory)
        self.assertEqual(result["properties"]["ifname"]["enum"], ["eth1"])

    def test_prefix_inventory_only_on_interface_schema_patterns(self):
        """vlan100 не должен попадать в jail_name с произвольным pattern."""
        ifname_pattern = r"^vlan\d+$"
        inventory = [{
            "names": ["vlan100", "vlan200"],
            "prefix": "vlan",
            "schema_patterns": [ifname_pattern],
        }]
        schema = {
            "type": "object",
            "properties": {
                "jail_name": {
                    "type": "string",
                    "pattern": r"^[A-Za-z0-9_\-]{1,17}$",
                },
                "ifname": {
                    "type": "string",
                    "pattern": ifname_pattern,
                },
            },
        }
        result = apply_interface_inventory(schema, inventory)
        self.assertNotIn("enum", result["properties"]["jail_name"])
        self.assertEqual(result["properties"]["ifname"]["enum"], ["vlan100", "vlan200"])

    def test_prefix_inventory_entry_gets_schema_patterns_from_rules(self):
        dependencies = {
            "interface_rules": {
                "ifname": {
                    "rules": [
                        {"pattern": r"^vlan\d+$", "env": "DEVICE_VLAN_IFNAMES"},
                        {
                            "prefix": "vlan",
                            "env": "DEVICE_VLAN_IFNAMES",
                            "create": "/interfaces/vlan/add",
                        },
                    ],
                },
            },
        }
        env = {"DEVICE_VLAN_IFNAMES": "vlan100"}
        inventory = build_interface_inventory(dependencies, env)
        prefix_entry = next(e for e in inventory if e.get("prefix") == "vlan")
        self.assertIn(r"^vlan\d+$", prefix_entry["schema_patterns"])

    def test_inventory_matches_schema_pattern(self):
        entry = {"pattern": r"^eth1$", "names": ["eth1"]}
        self.assertTrue(_inventory_matches_schema_pattern(entry, r"^eth1$"))
        self.assertFalse(_inventory_matches_schema_pattern(entry, r"^eth2$"))

    def test_prefix_inventory_matches_only_declared_schema_patterns(self):
        entry = {
            "prefix": "vlan",
            "names": ["vlan100"],
            "schema_patterns": [r"^vlan\d+$"],
        }
        self.assertTrue(_inventory_matches_schema_pattern(entry, r"^vlan\d+$"))
        self.assertFalse(
            _inventory_matches_schema_pattern(entry, r"^[A-Za-z0-9_\-]{1,17}$"),
        )

    def test_resolve_allowed_names_from_os_environ(self):
        rule = {"env": "DEVICE_ETH_IFNAMES"}
        with unittest.mock.patch.dict(os.environ, {"DEVICE_ETH_IFNAMES": "eth5"}):
            names = _resolve_allowed_names(rule, {})
        self.assertEqual(names, ["eth5"])


if __name__ == "__main__":
    unittest.main()

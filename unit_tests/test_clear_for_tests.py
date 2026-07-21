"""clear_for_tests.py — list → delete по cleanup.json."""

import unittest

from clear_for_tests import (
    build_delete_steps_for_items,
    extract_list_items,
    parse_cleanup_rules,
    should_skip_item,
)


class ExtractListItemsTests(unittest.TestCase):
    def test_simple_scalar_array(self):
        body = {"result": {"ipv4": ["acl1", "acl2"], "ipv6": []}}
        items = extract_list_items(body, {"items_path": "result.ipv4"})
        self.assertEqual(items, ["acl1", "acl2"])

    def test_interfaces_category_filter(self):
        body = {
            "result": {
                "interfaces": [
                    {"category": "ethernet", "ifname": ["eth1", "eth2"]},
                    {"category": "vlan", "ifname": ["vlan100", "vlan200"]},
                    {"category": "tunnel", "ifname": ["tunnel0"]},
                ],
            },
        }
        items = extract_list_items(
            body,
            {
                "items_path": "result.interfaces",
                "item_filter": {"category": "vlan"},
                "item_values": "ifname",
            },
        )
        self.assertEqual(items, ["vlan100", "vlan200"])

    def test_missing_path_returns_empty(self):
        items = extract_list_items({"result": {}}, {"items_path": "result.ipv4"})
        self.assertEqual(items, [])


class BuildDeleteStepsTests(unittest.TestCase):
    def test_builds_payload_with_item_placeholder(self):
        steps = build_delete_steps_for_items(
            {
                "endpoint": "/acl/acl_ipv4",
                "method": "POST",
                "payload": {
                    "action": {"delete": {"acl_name": "{{item}}"}},
                },
            },
            ["acl1", "acl2"],
        )
        self.assertEqual(len(steps), 2)
        self.assertEqual(
            steps[0]["payload"],
            {"action": {"delete": {"acl_name": "acl1"}}},
        )
        self.assertEqual(steps[1]["endpoint"], "/acl/acl_ipv4")

    def test_skip_and_prefix(self):
        steps = build_delete_steps_for_items(
            {
                "endpoint": "/interfaces/vlan/delete",
                "payload": {"ifname": "{{item}}"},
            },
            ["vlan100", "vlan603", "eth1.10"],
            skip=["vlan603"],
            skip_prefix=["eth"],
        )
        names = [s["payload"]["ifname"] for s in steps]
        self.assertEqual(names, ["vlan100"])


class ParseRulesTests(unittest.TestCase):
    def test_sorts_by_priority(self):
        config = {
            "rules": [
                {
                    "name": "acl",
                    "priority": 50,
                    "list": {"endpoint": "/acl/list"},
                    "delete": {"endpoint": "/acl/acl_ipv4"},
                },
                {
                    "name": "vlan",
                    "priority": 10,
                    "list": {"endpoint": "/interfaces/list"},
                    "delete": {"endpoint": "/interfaces/vlan/delete"},
                },
            ],
        }
        rules = parse_cleanup_rules(config)
        self.assertEqual([r["name"] for r in rules], ["vlan", "acl"])

    def test_filter_by_name(self):
        config = {
            "rules": [
                {
                    "name": "acl",
                    "list": {"endpoint": "/acl/list"},
                    "delete": {"endpoint": "/acl/acl_ipv4"},
                },
                {
                    "name": "vlan",
                    "list": {"endpoint": "/interfaces/list"},
                    "delete": {"endpoint": "/interfaces/vlan/delete"},
                },
            ],
        }
        rules = parse_cleanup_rules(config, only_names=["vlan"])
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["name"], "vlan")

    def test_defaults_skip_merged(self):
        config = {
            "defaults": {
                "skip": ["vlan1", "vlan603", "switchport1"],
            },
            "rules": [
                {
                    "name": "vlan",
                    "list": {"endpoint": "/interfaces/list"},
                    "delete": {"endpoint": "/interfaces/vlan/delete"},
                    "skip": ["vlan999"],
                },
            ],
        }
        rules = parse_cleanup_rules(config)
        self.assertEqual(
            rules[0]["skip"],
            ["vlan1", "vlan603", "switchport1", "vlan999"],
        )

class SkipTests(unittest.TestCase):
    def test_skip_exact_and_prefix(self):
        self.assertTrue(should_skip_item("vlan603", skip=["vlan603"], skip_prefix=[]))
        self.assertTrue(should_skip_item("eth1", skip=[], skip_prefix=["eth"]))
        self.assertFalse(should_skip_item("vlan100", skip=["vlan603"], skip_prefix=["eth"]))


if __name__ == "__main__":
    unittest.main()

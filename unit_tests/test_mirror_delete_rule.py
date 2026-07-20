"""Зеркалирование delete.rule в setup action.add на том же эндпоинте."""

import unittest

from main import _mirror_delete_rule_into_same_endpoint_setup


class MirrorDeleteRuleTests(unittest.TestCase):
    def _scenario(self, setup_add: dict, main_payload: dict) -> dict:
        return {
            "setup": [
                {
                    "endpoint": "/acl/acl_ipv4",
                    "method": "POST",
                    "payload": {"action": {"add": dict(setup_add)}},
                },
            ],
            "main_test": {
                "endpoint": "/acl/acl_ipv4",
                "payload": main_payload,
            },
            "teardown": [],
        }

    def test_mirrors_rule_from_delete_into_setup_add(self):
        schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "object",
                    "properties": {
                        "add": {
                            "type": "object",
                            "properties": {
                                "acl_name": {"type": "string"},
                                "rule": {"type": "object"},
                            },
                        },
                        "delete": {
                            "type": "object",
                            "properties": {
                                "acl_name": {"type": "string"},
                                "rule": {"type": "object"},
                            },
                        },
                    },
                },
            },
        }
        default_rule = {"dpi": {"dpi_protocol": "sina(weibo)"}}
        delete_rule = {"conn_state": {"established": True}}
        scenario = self._scenario(
            {"acl_name": "acl1", "rule": default_rule},
            {"action": {"delete": {"acl_name": "acl1", "rule": delete_rule}}},
        )
        _mirror_delete_rule_into_same_endpoint_setup(
            scenario,
            scenario["main_test"]["payload"],
            "/acl/acl_ipv4",
            schema,
        )
        add = scenario["setup"][0]["payload"]["action"]["add"]
        self.assertEqual(add["rule"], delete_rule)
        self.assertEqual(add["acl_name"], "acl1")

    def test_keeps_dependencies_rule_when_delete_has_no_rule(self):
        default_rule = {"dpi": {"dpi_protocol": "sina(weibo)"}}
        scenario = self._scenario(
            {"acl_name": "acl1", "rule": default_rule},
            {"action": {"delete": {"acl_name": "acl1"}}},
        )
        _mirror_delete_rule_into_same_endpoint_setup(
            scenario,
            scenario["main_test"]["payload"],
            "/acl/acl_ipv4",
            None,
        )
        add = scenario["setup"][0]["payload"]["action"]["add"]
        self.assertEqual(add["rule"], default_rule)

    def test_keeps_dependencies_when_delete_has_only_index(self):
        default_rule = {"dpi": {"dpi_protocol": "sina(weibo)"}}
        scenario = self._scenario(
            {"acl_name": "acl2", "rule": default_rule},
            {"action": {"delete": {"acl_name": "acl2", "index": 1}}},
        )
        _mirror_delete_rule_into_same_endpoint_setup(
            scenario,
            scenario["main_test"]["payload"],
            "/acl/acl_ipv4",
            None,
        )
        add = scenario["setup"][0]["payload"]["action"]["add"]
        self.assertEqual(add["rule"], default_rule)

    def test_drops_index_from_setup_when_mirroring_rule(self):
        scenario = self._scenario(
            {
                "acl_name": "acl1",
                "rule": {"dpi": {"dpi_protocol": "x"}},
                "index": 1,
            },
            {
                "action": {
                    "delete": {
                        "acl_name": "acl1",
                        "rule": {"fragment": {"fragment": True}},
                    },
                },
            },
        )
        _mirror_delete_rule_into_same_endpoint_setup(
            scenario,
            scenario["main_test"]["payload"],
            "/acl/acl_ipv4",
            None,
        )
        add = scenario["setup"][0]["payload"]["action"]["add"]
        self.assertEqual(add["rule"], {"fragment": {"fragment": True}})
        self.assertNotIn("index", add)

    def test_skips_other_endpoint_setup(self):
        scenario = {
            "setup": [
                {
                    "endpoint": "/ipsla",
                    "payload": {
                        "action": {
                            "add": {
                                "tracker_name": 1,
                                "type": "logical",
                            },
                        },
                    },
                },
                {
                    "endpoint": "/acl/acl_ipv4",
                    "payload": {
                        "action": {
                            "add": {
                                "acl_name": "acl1",
                                "rule": {"dpi": {"dpi_protocol": "old"}},
                            },
                        },
                    },
                },
            ],
            "main_test": {"endpoint": "/acl/acl_ipv4"},
        }
        main = {
            "action": {
                "delete": {
                    "acl_name": "acl1",
                    "rule": {"tos": {"tos_value": 1}},
                },
            },
        }
        _mirror_delete_rule_into_same_endpoint_setup(
            scenario, main, "/acl/acl_ipv4", None,
        )
        self.assertEqual(
            scenario["setup"][0]["payload"]["action"]["add"]["type"],
            "logical",
        )
        self.assertEqual(
            scenario["setup"][1]["payload"]["action"]["add"]["rule"],
            {"tos": {"tos_value": 1}},
        )

    def test_skips_when_schema_has_no_add_rule(self):
        schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "object",
                    "properties": {
                        "add": {
                            "type": "object",
                            "properties": {"acl_name": {"type": "string"}},
                        },
                    },
                },
            },
        }
        default_rule = {"dpi": {"dpi_protocol": "x"}}
        scenario = self._scenario(
            {"acl_name": "acl1", "rule": default_rule},
            {
                "action": {
                    "delete": {
                        "acl_name": "acl1",
                        "rule": {"tos": {"tos_value": 1}},
                    },
                },
            },
        )
        _mirror_delete_rule_into_same_endpoint_setup(
            scenario,
            scenario["main_test"]["payload"],
            "/acl/acl_ipv4",
            schema,
        )
        self.assertEqual(
            scenario["setup"][0]["payload"]["action"]["add"]["rule"],
            default_rule,
        )


if __name__ == "__main__":
    unittest.main()

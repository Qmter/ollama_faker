"""Поиск зависимостей в payload, endpoint_rules, bind_fields."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from main import (
    PayloadCoverage,
    _fill_bind_fields_from_mock_data,
    _get_endpoint_rules,
    _inject_synthetic_field_dependencies,
    _should_skip_field_mapping,
    _target_matches_skip_pattern,
    build_test_scenarios,
    scan_payload_for_dependencies,
)
from ollama_orchestrator import OllamaOrchestrator


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


class BindFieldsTests(unittest.TestCase):
    def test_fill_bind_fields_from_mock_data(self):
        bind_vars = {"vrf": "vrf1", "chain": "input"}
        result = _fill_bind_fields_from_mock_data(
            bind_vars,
            ["vrf", "chain", "acl_name"],
            {"acl_name": ["acl1", "acl2"]},
        )
        self.assertEqual(result["acl_name"], "acl1")
        self.assertEqual(result["vrf"], "vrf1")

    def test_fill_bind_fields_keeps_payload_value(self):
        bind_vars = {"acl_name": "from-payload"}
        result = _fill_bind_fields_from_mock_data(
            bind_vars,
            ["acl_name"],
            {"acl_name": ["acl1"]},
        )
        self.assertEqual(result["acl_name"], "from-payload")


class SyntheticDependenciesTests(unittest.TestCase):
    def test_injects_acl_name_for_delete_without_field(self):
        dep_map = {
            "acl_name": {
                "setup": {"endpoint": "/acl/acl_ipv4"},
                "optional": True,
            },
        }
        deps = {}
        variables = {}
        _inject_synthetic_field_dependencies(
            deps,
            variables,
            target_endpoint="/acl/filter/filter_ipv4",
            main_action="delete",
            dep_map=dep_map,
            mock_by_field={"acl_name": ["acl1"]},
            synthetic_fields={"/acl/filter/filter_ipv4": ["acl_name"]},
        )
        self.assertEqual(variables["acl_name"], "acl1")
        self.assertIn("_synthetic.acl_name", deps)

    def test_skips_when_acl_name_in_payload(self):
        dep_map = {"acl_name": {"setup": {"endpoint": "/acl/acl_ipv4"}}}
        deps = {
            "action.delete.acl_name": {
                "field": "acl_name",
                "value": "acl2",
                "config": dep_map["acl_name"],
            },
        }
        variables = {}
        _inject_synthetic_field_dependencies(
            deps,
            variables,
            target_endpoint="/acl/filter/filter_ipv4",
            main_action="delete",
            dep_map=dep_map,
            mock_by_field={"acl_name": ["acl1"]},
            synthetic_fields={"/acl/filter/filter_ipv4": ["acl_name"]},
        )
        self.assertNotIn("acl_name", variables)
        self.assertEqual(len(deps), 1)


class FilterIpv4ScenarioTests(unittest.TestCase):
    def _filter_deps(self):
        return {
            "field_mappings": {
                "vrf": {
                    "setup": {
                        "endpoint": "/vrf",
                        "method": "POST",
                        "payload": {
                            "action": "add",
                            "vrf_name": "{{vrf}}",
                        },
                    },
                    "teardown": {
                        "endpoint": "/vrf",
                        "method": "POST",
                        "payload": {
                            "action": "delete",
                            "vrf_name": "{{vrf}}",
                        },
                    },
                    "optional": True,
                },
                "acl_name": {
                    "setup": {
                        "endpoint": "/acl/acl_ipv4",
                        "method": "POST",
                        "payload": {
                            "action": {
                                "add": {
                                    "acl_name": "{{acl_name}}",
                                    "rule": {
                                        "dpi": {
                                            "dpi_protocol": "sina(weibo)",
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "teardown": {
                        "endpoint": "/acl/acl_ipv4",
                        "method": "POST",
                        "payload": {
                            "action": {
                                "delete": {
                                    "acl_name": "{{acl_name}}",
                                },
                            },
                        },
                    },
                    "optional": True,
                },
            },
            "interface_rules": {},
            "endpoint_rules": {
                "/acl/filter/filter_ipv4": {
                    "bind_fields": ["vrf", "chain", "acl_name"],
                    "delete": {
                        "setup": [
                            {
                                "endpoint": "/acl/filter/filter_ipv4",
                                "method": "POST",
                                "payload": {
                                    "action": {
                                        "add": {
                                            "vrf": "{{vrf}}",
                                            "chain": "{{chain}}",
                                            "filter": {
                                                "acl_name": "{{acl_name}}",
                                                "action": "permit",
                                            },
                                        },
                                    },
                                },
                                "note": "Create filter before delete test",
                            },
                        ],
                    },
                },
            },
            "synthetic_bind_fields": {
                "/acl/filter/filter_ipv4": ["acl_name"],
            },
            "mock_data": {
                "by_field": {
                    "vrf": ["vrf1"],
                    "acl_name": ["acl1"],
                },
            },
        }

    def test_delete_without_acl_name_resolves_placeholders(self):
        ollama = OllamaOrchestrator.from_cli(False)
        records = [
            PayloadCoverage(
                {
                    "action": {
                        "delete": {
                            "vrf": "vrf1",
                            "chain": "forward",
                        },
                    },
                },
                ["action.delete.chain=\"forward\""],
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/acl/filter/filter_ipv4",
                    "post",
                    records,
                    self._filter_deps(),
                    ollama=ollama,
                )
                scenario = json.loads(
                    Path(
                        "tests/acl/acl_filter_filter_ipv4_post.json",
                    ).read_text(encoding="utf-8"),
                )[0]
                setup_eps = [s["endpoint"] for s in scenario["setup"]]
                self.assertIn("/acl/acl_ipv4", setup_eps)
                filter_setup = next(
                    s for s in scenario["setup"]
                    if s["endpoint"] == "/acl/filter/filter_ipv4"
                )
                acl_name = filter_setup["payload"]["action"]["add"]["filter"][
                    "acl_name"
                ]
                self.assertEqual(acl_name, "acl1")
                self.assertNotIn("{{", json.dumps(scenario))
            finally:
                os.chdir(prev)


class DnsProxyControlScenarioTests(unittest.TestCase):
    def test_top_level_lifecycle_applies_for_string_action(self):
        ollama = OllamaOrchestrator.from_cli(False)
        deps = {
            "field_mappings": {
                "vrf_name": {
                    "setup": {
                        "endpoint": "/vrf",
                        "method": "POST",
                        "payload": {
                            "action": "add",
                            "vrf_name": "{{vrf_name}}",
                        },
                    },
                    "teardown": {
                        "endpoint": "/vrf",
                        "method": "POST",
                        "payload": {
                            "action": "delete",
                            "vrf_name": "{{vrf_name}}",
                        },
                    },
                    "optional": True,
                },
            },
            "interface_rules": {},
            "endpoint_rules": {
                "/dns/proxy/control": {
                    "bind_fields": [
                        "vrf_name",
                        "option_authoritative",
                        "option_recursive",
                    ],
                    "setup": {
                        "endpoint": "/dns/proxy",
                        "method": "POST",
                        "payload": {
                            "vrf_name": "{{vrf_name}}",
                            "option_authoritative": "{{option_authoritative}}",
                            "option_recursive": "{{option_recursive}}",
                        },
                        "expected_status": 200,
                    },
                    "teardown": {
                        "endpoint": "/dns/proxy/delete",
                        "method": "POST",
                        "payload": {
                            "vrf_name": "{{vrf_name}}",
                        },
                    },
                },
            },
            "mock_data": {
                "by_field": {
                    "vrf_name": ["vrf1"],
                    "option_authoritative": ["10.0.0.1"],
                    "option_recursive": ["10.0.0.1"],
                },
            },
        }
        records = [
            PayloadCoverage(
                {"vrf_name": "vrf1", "action": "on"},
                ["__minimal__"],
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/dns/proxy/control",
                    "post",
                    records,
                    deps,
                    ollama=ollama,
                )
                scenario = json.loads(
                    Path(
                        "tests/dns/dns_proxy_control_post.json",
                    ).read_text(encoding="utf-8"),
                )[0]
                setup_eps = [s["endpoint"] for s in scenario["setup"]]
                teardown_eps = [s["endpoint"] for s in scenario["teardown"]]
                self.assertIn("/dns/proxy", setup_eps)
                self.assertIn("/dns/proxy/delete", teardown_eps)
                self.assertNotIn("{{", json.dumps(scenario))
            finally:
                os.chdir(prev)


class SkipTargetsTests(unittest.TestCase):
    def test_exact_skip_pattern(self):
        self.assertTrue(
            _target_matches_skip_pattern(
                "/dns/server/zone/master/add",
                "/dns/server/zone/master/add",
            ),
        )
        self.assertFalse(
            _target_matches_skip_pattern(
                "/dns/server/zone/master/post",
                "/dns/server/zone/master/add",
            ),
        )

    def test_wildcard_skip_pattern(self):
        pattern = "/dns/server/zone/slave/*"
        self.assertTrue(
            _target_matches_skip_pattern(
                "/dns/server/zone/slave/add",
                pattern,
            ),
        )
        self.assertTrue(
            _target_matches_skip_pattern(
                "/dns/server/zone/slave/delete",
                pattern,
            ),
        )
        self.assertFalse(
            _target_matches_skip_pattern(
                "/dns/server/zone/master/add",
                pattern,
            ),
        )

    def test_should_skip_field_mapping(self):
        config = {
            "skip_targets": [
                "/dns/server/zone/master/add",
                "/dns/server/zone/slave/*",
            ],
        }
        self.assertTrue(
            _should_skip_field_mapping(
                config, "/dns/server/zone/slave/add",
            ),
        )
        self.assertFalse(
            _should_skip_field_mapping(
                config, "/dns/server/zone/master/entry/add",
            ),
        )

    def test_slave_add_has_no_master_zone_setup(self):
        ollama = OllamaOrchestrator.from_cli(False)
        zone_mapping = {
            "requirements": ["setup", "teardown"],
            "skip_targets": [
                "/dns/server/zone/master/add",
                "/dns/server/zone/slave/*",
            ],
            "setup": {
                "endpoint": "/dns/server/zone/master/add",
                "method": "POST",
                "payload": {"zone_name": "{{zone_name}}"},
            },
            "teardown": {
                "endpoint": "/dns/server/zone/master/delete",
                "method": "POST",
                "payload": {"zone_name": "{{zone_name}}"},
            },
        }
        deps = {
            "field_mappings": {"zone_name": zone_mapping},
            "interface_rules": {},
            "endpoint_rules": {
                "/dns/server/zone/slave/add": {
                    "bind_fields": ["zone_name", "master_ip"],
                    "teardown": {
                        "endpoint": "/dns/server/zone/slave/delete",
                        "method": "POST",
                        "payload": {"zone_name": "{{zone_name}}"},
                    },
                },
            },
            "mock_data": {
                "by_field": {
                    "zone_name": ["zone1"],
                    "master_ip": ["10.0.0.2"],
                },
            },
        }
        records = [
            PayloadCoverage(
                {"zone_name": "zone1", "master_ip": "10.0.0.2"},
                ["master_ip=\"10.0.0.2\""],
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/dns/server/zone/slave/add",
                    "post",
                    records,
                    deps,
                    ollama=ollama,
                )
                scenario = json.loads(
                    Path(
                        "tests/dns/dns_server_zone_slave_add_post.json",
                    ).read_text(encoding="utf-8"),
                )[0]
                setup_eps = [s["endpoint"] for s in scenario["setup"]]
                teardown_eps = [s["endpoint"] for s in scenario["teardown"]]
                self.assertNotIn("/dns/server/zone/master/add", setup_eps)
                self.assertIn("/dns/server/zone/slave/delete", teardown_eps)
            finally:
                os.chdir(prev)


class ZoneEntryScenarioTests(unittest.TestCase):
    def test_entry_delete_setup_by_entry_type(self):
        ollama = OllamaOrchestrator.from_cli(False)
        deps = {
            "field_mappings": {
                "zone_name": {
                    "skip_targets": ["/dns/server/zone/master/add"],
                    "setup": {
                        "endpoint": "/dns/server/zone/master/add",
                        "method": "POST",
                        "payload": {"zone_name": "{{zone_name}}"},
                    },
                    "teardown": {
                        "endpoint": "/dns/server/zone/master/delete",
                        "method": "POST",
                        "payload": {"zone_name": "{{zone_name}}"},
                    },
                },
            },
            "interface_rules": {},
            "endpoint_rules": {
                "/dns/server/zone/master/entry/delete": {
                    "lifecycle_key_field": "entry_type",
                    "bind_fields": [
                        "zone_name", "entry_name", "entry_type", "ip_address",
                    ],
                    "a": {
                        "setup": {
                            "endpoint": "/dns/server/zone/master/entry/add",
                            "method": "POST",
                            "payload": {
                                "zone_name": "{{zone_name}}",
                                "entry": {
                                    "entry_name": "{{entry_name}}",
                                    "entry_params": {
                                        "entry_type": "a",
                                        "ip_address": "{{ip_address}}",
                                    },
                                },
                            },
                            "expected_status": 200,
                        },
                    },
                },
            },
            "mock_data": {
                "by_field": {
                    "zone_name": ["zone1"],
                    "ip_address": ["10.0.0.1"],
                },
            },
        }
        records = [
            PayloadCoverage(
                {
                    "zone_name": "zone1",
                    "entry_name": "test-entry",
                    "entry_type": "a",
                },
                ["__minimal__"],
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/dns/server/zone/master/entry/delete",
                    "post",
                    records,
                    deps,
                    ollama=ollama,
                )
                scenario = json.loads(
                    Path(
                        "tests/dns/dns_server_zone_master_entry_delete_post.json",
                    ).read_text(encoding="utf-8"),
                )[0]
                setup_eps = [s["endpoint"] for s in scenario["setup"]]
                self.assertIn("/dns/server/zone/master/add", setup_eps)
                entry_add = next(
                    s for s in scenario["setup"]
                    if s["endpoint"] == "/dns/server/zone/master/entry/add"
                )
                self.assertEqual(
                    entry_add["payload"]["entry"]["entry_name"],
                    "test-entry",
                )
            finally:
                os.chdir(prev)


if __name__ == "__main__":
    unittest.main()

"""Интеграционные unit-тесты build_test_scenarios (без API)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from main import (
    PayloadCoverage,
    build_test_scenarios,
)

MINIMAL_DEPS = {
    "field_mappings": {},
    "interface_rules": {
        "ifname": {
            "rules": [
                {
                    "prefix": "bond",
                    "create": "/interfaces/bonding/add",
                    "delete": "/interfaces/bonding/delete",
                },
            ],
        },
    },
    "endpoint_rules": {},
}


class BuildTestScenariosTests(unittest.TestCase):
    def test_writes_scenario_json(self):
        records = [
            PayloadCoverage({"ifname": "bond0"}, ["__minimal__"]),
        ]
        schema = {
            "type": "object",
            "required": ["ifname"],
            "properties": {
                "ifname": {"type": "string", "enum": ["bond0"]},
            },
            "additionalProperties": False,
        }
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/interfaces/bonding/add",
                    "post",
                    records,
                    MINIMAL_DEPS,
                    request_schema=schema,
                )
                out = Path("tests/interfaces/interfaces_bonding_add_post.json")
                self.assertTrue(out.is_file())
                data = json.loads(out.read_text(encoding="utf-8"))
                self.assertEqual(len(data), 1)
                self.assertEqual(data[0]["main_test"]["endpoint"], "/interfaces/bonding/add")
                self.assertIn("setup", data[0])
                self.assertIn("teardown", data[0])
            finally:
                os.chdir(prev)

    def test_bond_lifecycle_in_scenario(self):
        records = [PayloadCoverage({"ifname": "bond1"}, ["ifname=bond1"])]
        schema = {
            "type": "object",
            "required": ["ifname"],
            "properties": {"ifname": {"type": "string", "enum": ["bond1"]}},
            "additionalProperties": False,
        }
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/interfaces/bonding/add",
                    "post",
                    records,
                    MINIMAL_DEPS,
                    request_schema=schema,
                )
                scenario = json.loads(
                    Path("tests/interfaces/interfaces_bonding_add_post.json").read_text(encoding="utf-8"),
                )[0]
                setup_eps = [s["endpoint"] for s in scenario["setup"]]
                teardown_eps = [s["endpoint"] for s in scenario["teardown"]]
                # self-skip: main_test уже bonding/add — setup create не дублируется
                self.assertEqual(setup_eps, [])
                self.assertIn("/interfaces/bonding/delete", teardown_eps)
            finally:
                os.chdir(prev)

    def test_vlan_add_syncs_vid_with_ifname_and_vlandb(self):
        """vid в main_test и vlandb teardown должны совпадать с ifname (vlan4092 → 4092)."""
        deps = {
            "field_mappings": {},
            "interface_rules": {
                "ifname": {
                    "rules": [
                        {
                            "prefix": "vlan",
                            "setup": {
                                "endpoint": "/interfaces/vlan/add",
                                "payload": {"ifname": "{{ifname}}", "vid": "{{vid}}"},
                            },
                            "teardown": {
                                "endpoint": "/interfaces/vlan/delete",
                                "payload": {"ifname": "{{ifname}}"},
                            },
                        },
                    ],
                },
            },
            "endpoint_rules": {
                "/interfaces/switchport/vlandb": {
                    "bind_fields": ["vlan"],
                    "add": {
                        "teardown": {
                            "endpoint": "/interfaces/switchport/vlandb",
                            "payload": {"action": "delete", "vlan": "{{vlan}}"},
                        },
                    },
                    "delete": {
                        "setup": {
                            "endpoint": "/interfaces/switchport/vlandb",
                            "payload": {"action": "add", "vlan": "{{vlan}}"},
                        },
                    },
                },
            },
        }
        records = [
            PayloadCoverage({"ifname": "vlan4092", "vid": 1926}, ["__minimal__"]),
        ]
        schema = {
            "type": "object",
            "required": ["ifname", "vid"],
            "properties": {
                "ifname": {"type": "string", "enum": ["vlan4092"]},
                "vid": {"type": "integer"},
            },
            "additionalProperties": False,
        }
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/interfaces/vlan/add",
                    "post",
                    records,
                    deps,
                    request_schema=schema,
                )
                scenario = json.loads(
                    Path("tests/interfaces/interfaces_vlan_add_post.json").read_text(encoding="utf-8"),
                )[0]
                self.assertEqual(scenario["main_test"]["payload"]["vid"], 4092)
                vlandb_steps = [
                    s for s in scenario["setup"] + scenario["teardown"]
                    if s["endpoint"] == "/interfaces/switchport/vlandb"
                ]
                self.assertEqual(len(vlandb_steps), 2)
                for step in vlandb_steps:
                    self.assertEqual(step["payload"]["vlan"], "4092")
            finally:
                os.chdir(prev)

    def test_common_arp_main_test_has_no_spurious_vid(self):
        """main_test без vid в схеме не должен получать vid из synchronize_vid_ifname."""
        deps = {
            "field_mappings": {},
            "interface_rules": {
                "ifname": {
                    "rules": [
                        {
                            "prefix": "vlan",
                            "setup": {
                                "endpoint": "/interfaces/vlan/add",
                                "payload": {"ifname": "{{ifname}}", "vid": "{{vid}}"},
                            },
                            "teardown": {
                                "endpoint": "/interfaces/vlan/delete",
                                "payload": {"ifname": "{{ifname}}"},
                            },
                        },
                    ],
                },
            },
            "endpoint_rules": {},
        }
        records = [
            PayloadCoverage(
                {"ifname": "vlan100", "announce": "any"},
                ['announce="any"'],
            ),
        ]
        schema = {
            "type": "object",
            "required": ["ifname"],
            "properties": {
                "ifname": {"type": "string", "enum": ["vlan100"]},
                "announce": {"type": "string", "enum": ["any", "best"]},
            },
            "additionalProperties": False,
        }
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/interfaces/common/arp",
                    "post",
                    records,
                    deps,
                    request_schema=schema,
                )
                scenario = json.loads(
                    Path("tests/interfaces/interfaces_common_arp_post.json").read_text(
                        encoding="utf-8",
                    ),
                )[0]
                main_payload = scenario["main_test"]["payload"]
                self.assertNotIn("vid", main_payload)
                self.assertEqual(main_payload["announce"], "any")
                setup_vlan = next(
                    s for s in scenario["setup"]
                    if s["endpoint"] == "/interfaces/vlan/add"
                )
                self.assertEqual(setup_vlan["payload"]["vid"], 100)
            finally:
                os.chdir(prev)

    def test_fail2ban_jail_name_does_not_trigger_vlan_lifecycle(self):
        """jail_name=jail1/vlan100: свой lifecycle из field_mappings, без vlan/add."""
        from resolve_scheme import ResolveScheme
        from main import (
            apply_interface_inventory,
            apply_mock_data,
            build_eth_parents_with_vlan_children,
            build_interface_inventory,
            build_mock_pattern_index,
            load_env_file,
            load_openapi_components,
            parse_mock_data_config,
            preprocess_schema_for_jsf,
        )

        deps_path = Path(__file__).resolve().parent.parent / "dependencies.json"
        with open(deps_path, encoding="utf-8") as f:
            deps = json.load(f)
        resolved = ResolveScheme.resolve_endpoint(
            str(Path(__file__).resolve().parent.parent / "openapi.json"),
            "/fail2ban/jail/add",
            "post",
        )
        schema = preprocess_schema_for_jsf(
            resolved["requestBody"]["content"]["application/json"]["schema"],
        )
        inventory = build_interface_inventory(deps, load_env_file())
        schema = apply_interface_inventory(
            schema,
            inventory,
            blocked_eth_parents=build_eth_parents_with_vlan_children(inventory or []),
        )
        mock_config = parse_mock_data_config(deps)
        if mock_config:
            schema = apply_mock_data(
                schema,
                mock_config,
                build_mock_pattern_index(load_openapi_components(), mock_config),
            )
        jail_enum = schema["properties"]["jail_name"].get("enum", [])
        self.assertNotIn("vlan100", jail_enum)
        records = [
            PayloadCoverage({"jail_name": "jail1"}, ['jail_name="jail1"']),
            PayloadCoverage({"jail_name": "jail2"}, ['jail_name="jail2"']),
        ]
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/fail2ban/jail/add",
                    "post",
                    records,
                    deps,
                    request_schema=schema,
                )
                scenarios = json.loads(
                    Path("tests/fail2ban/fail2ban_jail_add_post.json").read_text(
                        encoding="utf-8",
                    ),
                )
                for scenario in scenarios:
                    jail_name = scenario["main_test"]["payload"]["jail_name"]
                    self.assertIn(jail_name, {"jail1", "jail2"}, jail_name)
                    setup_eps = [s["endpoint"] for s in scenario["setup"]]
                    self.assertNotIn("/interfaces/vlan/add", setup_eps, jail_name)
                    self.assertEqual(setup_eps, [], jail_name)
                    self.assertEqual(
                        [s["endpoint"] for s in scenario["teardown"]],
                        ["/fail2ban/jail/delete"],
                    )
            finally:
                os.chdir(prev)

    def test_tunnel_source_ifname_triggers_vlan_setup(self):
        """settings.source=vlan100 (IFNAME) → setup vlan/add; IP — нет."""
        from resolve_scheme import ResolveScheme
        from main import (
            apply_interface_inventory,
            apply_mock_data,
            build_eth_parents_with_vlan_children,
            build_interface_inventory,
            build_mock_pattern_index,
            load_env_file,
            load_openapi_components,
            parse_mock_data_config,
            preprocess_schema_for_jsf,
        )

        deps = {
            "field_mappings": {},
            "interface_rules": {
                "ifname": {
                    "rules": [
                        {
                            "prefix": "vlan",
                            "setup": {
                                "endpoint": "/interfaces/vlan/add",
                                "payload": {"ifname": "{{ifname}}", "vid": "{{vid}}"},
                            },
                            "teardown": [
                                {
                                    "endpoint": "/interfaces/vlan/delete",
                                    "payload": {"ifname": "{{ifname}}"},
                                },
                            ],
                        },
                        {
                            "prefix": "tunnel",
                            "teardown": [
                                {
                                    "endpoint": "/interfaces/tunnel/delete",
                                    "payload": {"ifname": "{{ifname}}"},
                                },
                            ],
                        },
                    ],
                },
            },
            "interface_lifecycle": {
                "schema_components": ["IFNAME"],
                "rules_key": "ifname",
            },
            "endpoint_rules": {},
        }
        with open(Path(__file__).resolve().parent.parent / "dependencies.json", encoding="utf-8") as f:
            full_deps = json.load(f)
        resolved = ResolveScheme.resolve_endpoint(
            str(Path(__file__).resolve().parent.parent / "openapi.json"),
            "/interfaces/tunnel/add",
            "post",
        )
        tunnel_schema = preprocess_schema_for_jsf(
            resolved["requestBody"]["content"]["application/json"]["schema"],
        )
        inventory = build_interface_inventory(full_deps, load_env_file())
        tunnel_schema = apply_interface_inventory(
            tunnel_schema,
            inventory,
            blocked_eth_parents=build_eth_parents_with_vlan_children(inventory or []),
        )
        mock_config = parse_mock_data_config(full_deps)
        if mock_config:
            tunnel_schema = apply_mock_data(
                tunnel_schema,
                mock_config,
                build_mock_pattern_index(load_openapi_components(), mock_config),
            )
        records = [
            PayloadCoverage(
                {"ifname": "tunnel39", "settings": {"source": "vlan100"}},
                ['settings.source="vlan100"'],
            ),
            PayloadCoverage(
                {"ifname": "tunnel0", "settings": {"source": "10.0.0.1"}},
                ['settings.source="10.0.0.1"'],
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/interfaces/tunnel/add",
                    "post",
                    records,
                    deps,
                    request_schema=tunnel_schema,
                )
                scenarios = json.loads(
                    Path("tests/interfaces/interfaces_tunnel_add_post.json").read_text(
                        encoding="utf-8",
                    ),
                )
                vlan_scenario = next(
                    s for s in scenarios
                    if s["main_test"]["payload"]["settings"]["source"] == "vlan100"
                )
                ip_scenario = next(
                    s for s in scenarios
                    if s["main_test"]["payload"]["settings"]["source"] == "10.0.0.1"
                )
                vlan_setup = [
                    s for s in vlan_scenario["setup"]
                    if s["endpoint"] == "/interfaces/vlan/add"
                ]
                self.assertEqual(len(vlan_setup), 1)
                self.assertEqual(vlan_setup[0]["payload"]["ifname"], "vlan100")
                self.assertEqual(vlan_setup[0]["payload"]["vid"], 100)
                self.assertEqual(
                    [
                        s["endpoint"] for s in ip_scenario["setup"]
                        if s["endpoint"] == "/interfaces/vlan/add"
                    ],
                    [],
                )
            finally:
                os.chdir(prev)

    def test_bonding_mode_primary_interface_eth_vlan_setup(self):
        """mode.primary_interface=eth1.1 → eth_vlan/add до enslave capability."""
        from resolve_scheme import ResolveScheme
        from main import (
            apply_interface_inventory,
            apply_mock_data,
            build_eth_parents_with_vlan_children,
            build_interface_inventory,
            build_mock_pattern_index,
            load_env_file,
            load_openapi_components,
            parse_mock_data_config,
            preprocess_schema_for_jsf,
        )

        deps_path = Path(__file__).resolve().parent.parent / "dependencies.json"
        with open(deps_path, encoding="utf-8") as f:
            deps = json.load(f)
        resolved = ResolveScheme.resolve_endpoint(
            str(Path(__file__).resolve().parent.parent / "openapi.json"),
            "/interfaces/bonding/mode",
            "post",
        )
        schema = preprocess_schema_for_jsf(
            resolved["requestBody"]["content"]["application/json"]["schema"],
        )
        inventory = build_interface_inventory(deps, load_env_file())
        schema = apply_interface_inventory(
            schema,
            inventory,
            blocked_eth_parents=build_eth_parents_with_vlan_children(inventory or []),
        )
        mock_config = parse_mock_data_config(deps)
        if mock_config:
            schema = apply_mock_data(
                schema,
                mock_config,
                build_mock_pattern_index(load_openapi_components(), mock_config),
            )
        records = [
            PayloadCoverage(
                {
                    "ifname": "bond9410",
                    "mode": {
                        "mode_type": "active-backup",
                        "primary_interface": "eth1.2",
                    },
                },
                ['mode.primary_interface="eth1.2"'],
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/interfaces/bonding/mode",
                    "post",
                    records,
                    deps,
                    request_schema=schema,
                )
                scenario = json.loads(
                    Path("tests/interfaces/interfaces_bonding_mode_post.json").read_text(
                        encoding="utf-8",
                    ),
                )[0]
                setup_eps = [s["endpoint"] for s in scenario["setup"]]
                self.assertIn("/interfaces/eth_vlan/add", setup_eps)
                self.assertIn("/interfaces/bonding/add", setup_eps)
                self.assertIn("/interfaces/bonding/capability", setup_eps)
                eth_vlan_idx = setup_eps.index("/interfaces/eth_vlan/add")
                bond_idx = setup_eps.index("/interfaces/bonding/add")
                enslave_idx = setup_eps.index("/interfaces/bonding/capability")
                self.assertLess(eth_vlan_idx, bond_idx)
                self.assertLess(bond_idx, enslave_idx)
                eth_vlan_step = next(
                    s for s in scenario["setup"]
                    if s["endpoint"] == "/interfaces/eth_vlan/add"
                )
                self.assertEqual(eth_vlan_step["payload"]["ifname"], "eth1.2")
                self.assertEqual(eth_vlan_step["payload"]["vid"], 2)
                self.assertIn(
                    "/interfaces/eth_vlan/delete",
                    [s["endpoint"] for s in scenario["teardown"]],
                )
            finally:
                os.chdir(prev)

    def test_eth_vlan_add_self_skip_omits_post_create_setup(self):
        """Тест eth_vlan/add: ip/shutdown из lifecycle не в setup (интерфейса ещё нет)."""
        deps_path = Path(__file__).resolve().parent.parent / "dependencies.json"
        with open(deps_path, encoding="utf-8") as f:
            deps = json.load(f)
        records = [
            PayloadCoverage(
                {"ifname": "eth1.2", "vid": 2},
                ['__minimal__', 'ifname="eth1.2"'],
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/interfaces/eth_vlan/add",
                    "post",
                    records,
                    deps,
                )
                scenario = json.loads(
                    Path(
                        "tests/interfaces/interfaces_eth_vlan_add_post.json",
                    ).read_text(encoding="utf-8"),
                )[0]
                setup_eps = [s["endpoint"] for s in scenario["setup"]]
                self.assertNotIn("/interfaces/eth_vlan/add", setup_eps)
                self.assertNotIn("/interfaces/common/ip_address", setup_eps)
                self.assertNotIn("/interfaces/shutdown", setup_eps)
            finally:
                os.chdir(prev)

    def test_eth_vlan_bind_fields_fills_ip_addr_from_mock(self):
        """interface_rules.bind_fields + mock_data.by_field → {{ip_addr}} подставляется."""
        deps = {
            "field_mappings": {},
            "interface_rules": {
                "ifname": {
                    "rules": [
                        {
                            "prefix": "bond",
                            "create": "/interfaces/bonding/add",
                            "delete": "/interfaces/bonding/delete",
                        },
                        {
                            "pattern": (
                                r"^eth(0|[1-9][0-9]{0,3})"
                                r"([\\.](0|[1-9][0-9]{0,3}))$"
                            ),
                            "bind_fields": ["ifname", "ip_addr"],
                            "setup": [
                                {
                                    "endpoint": "/interfaces/eth_vlan/add",
                                    "method": "POST",
                                    "payload": {
                                        "ifname": "{{ifname}}",
                                        "vid": "{{vid}}",
                                    },
                                },
                                {
                                    "endpoint": "/interfaces/common/ip_address",
                                    "method": "POST",
                                    "payload": {
                                        "ifname": "{{ifname}}",
                                        "ip_addr": "{{ip_addr}}",
                                    },
                                },
                            ],
                            "teardown": [
                                {
                                    "endpoint": "/interfaces/eth_vlan/delete",
                                    "method": "POST",
                                    "payload": {"ifname": "{{ifname}}"},
                                },
                            ],
                        },
                    ],
                },
            },
            "endpoint_rules": {},
            "mock_data": {
                "by_field": {
                    "ip_addr": ["10.0.0.10/24"],
                },
            },
        }
        records = [
            PayloadCoverage(
                {
                    "ifname": "bond0",
                    "capability": {"enslave": ["eth1.2"]},
                },
                ['capability.enslave=["eth1.2"]'],
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                build_test_scenarios(
                    "/interfaces/bonding/capability",
                    "post",
                    records,
                    deps,
                )
                scenario = json.loads(
                    Path(
                        "tests/interfaces/interfaces_bonding_capability_post.json",
                    ).read_text(encoding="utf-8"),
                )[0]
                ip_step = next(
                    s for s in scenario["setup"]
                    if s["endpoint"] == "/interfaces/common/ip_address"
                )
                self.assertEqual(ip_step["payload"]["ifname"], "eth1.2")
                self.assertEqual(ip_step["payload"]["ip_addr"], "10.0.0.10/24")
                self.assertNotIn("{{", str(ip_step["payload"]["ip_addr"]))
            finally:
                os.chdir(prev)


if __name__ == "__main__":
    unittest.main()

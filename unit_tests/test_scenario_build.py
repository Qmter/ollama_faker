"""Интеграционные unit-тесты build_test_scenarios (без API)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from main import (
    PayloadCoverage,
    build_test_scenarios,
    synchronize_vid_ifname,
)
from ollama_orchestrator import OllamaOrchestrator


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
        ollama = OllamaOrchestrator.from_cli(False)
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
                    ollama=ollama,
                )
                out = Path("tests/interfaces_bonding_add_post.json")
                self.assertTrue(out.is_file())
                data = json.loads(out.read_text(encoding="utf-8"))
                self.assertEqual(len(data), 1)
                self.assertEqual(data[0]["main_test"]["endpoint"], "/interfaces/bonding/add")
                self.assertIn("setup", data[0])
                self.assertIn("teardown", data[0])
            finally:
                os.chdir(prev)

    def test_bond_lifecycle_in_scenario(self):
        ollama = OllamaOrchestrator.from_cli(False)
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
                    ollama=ollama,
                )
                scenario = json.loads(
                    Path("tests/interfaces_bonding_add_post.json").read_text(encoding="utf-8"),
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
        ollama = OllamaOrchestrator.from_cli(False)
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
                    ollama=ollama,
                )
                scenario = json.loads(
                    Path("tests/interfaces_vlan_add_post.json").read_text(encoding="utf-8"),
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


if __name__ == "__main__":
    unittest.main()

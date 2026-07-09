"""Lifecycle: порядок setup/teardown, VID, bind vars, defer bond."""

import unittest

from main import (
    _PLACEHOLDER_CONTEXT_KEY,
    _SETUP_PHASE_ORDER,
    _TEARDOWN_PRIORITY_INTERFACE,
    _TEARDOWN_PRIORITY_PREREQUISITE,
    _VidPool,
    _append_setup_step,
    _append_teardown_step,
    _collect_bind_vars,
    _extract_main_action,
    _field_lifecycle_phases,
    _infer_vid_from_ifname,
    _interface_deferred_prefixes,
    _interface_setup_sort_key,
    _is_prerequisite_field,
    _lifecycle_vars,
    _parse_vid_pool,
    _resolve_teardown_priority,
    _resolve_vid,
    _sort_scenario_setup,
    _sort_scenario_teardown,
    build_eth_parents_with_vlan_children,
    synchronize_vid_ifname,
)


class LifecycleVarsTests(unittest.TestCase):
    def test_primary_interface_does_not_override_bond_ifname(self):
        variables = {"ifname": "bond0", "mode_type": "active-backup"}
        result = _lifecycle_vars("primary_interface", "eth1.1", variables)
        self.assertEqual(result["ifname"], "bond0")
        self.assertEqual(result["primary_interface"], "eth1.1")

    def test_ifname_field_still_sets_ifname(self):
        result = _lifecycle_vars("ifname", "eth1", {})
        self.assertEqual(result["ifname"], "eth1")


class BindVarsTests(unittest.TestCase):
    def test_collect_bind_vars_from_bond_mode_payload(self):
        payload = {
            "ifname": "bond0",
            "mode": {
                "mode_type": "active-backup",
                "primary_interface": "eth1.1",
            },
        }
        variables = {}
        _collect_bind_vars(payload, variables)
        self.assertEqual(variables["ifname"], "bond0")
        self.assertEqual(variables["primary_interface"], "eth1.1")


class MainActionTests(unittest.TestCase):
    def test_extract_string_action(self):
        action, data = _extract_main_action({"action": "add", "pool_number": 1})
        self.assertEqual(action, "add")
        self.assertEqual(data["pool_number"], 1)

    def test_extract_object_action(self):
        action, data = _extract_main_action({
            "action": {"delete": {"acl_name": "test"}},
        })
        self.assertEqual(action, "delete")
        self.assertEqual(data["acl_name"], "test")

    def test_no_action_returns_none(self):
        self.assertEqual(_extract_main_action({"ifname": "eth1"}), (None, {}))


class FieldLifecyclePhasesTests(unittest.TestCase):
    def test_prerequisite_always_setup_and_teardown(self):
        config = {
            "setup": {"endpoint": "/vrf", "method": "POST", "payload": {}},
            "teardown": {"endpoint": "/vrf", "method": "POST", "payload": {}},
        }
        phases = _field_lifecycle_phases(None, config, "/interfaces/common/ip_address")
        self.assertEqual(phases, {"setup", "teardown"})

    def test_add_action_only_teardown(self):
        config = {"setup": {"endpoint": "/x"}, "teardown": {"endpoint": "/x"}}
        phases = _field_lifecycle_phases("add", config, "/x")
        self.assertEqual(phases, {"teardown"})

    def test_delete_action_only_setup(self):
        config = {"setup": {"endpoint": "/x"}, "teardown": {"endpoint": "/x"}}
        phases = _field_lifecycle_phases("delete", config, "/x")
        self.assertEqual(phases, {"setup"})


class PrerequisiteFieldTests(unittest.TestCase):
    def test_vrf_is_prerequisite_for_other_endpoint(self):
        config = {
            "setup": {"endpoint": "/vrf", "method": "POST", "payload": {}},
            "teardown": {"endpoint": "/vrf", "method": "POST", "payload": {}},
        }
        self.assertTrue(_is_prerequisite_field(config, "/interfaces/bridge/add"))

    def test_not_prerequisite_when_setup_and_teardown_same_endpoint(self):
        config = {
            "setup": {"endpoint": "/interfaces/shutdown"},
            "teardown": {"endpoint": "/interfaces/shutdown"},
        }
        self.assertFalse(_is_prerequisite_field(config, "/interfaces/shutdown"))

    def test_prerequisite_when_teardown_on_different_endpoint(self):
        config = {
            "create": "/interfaces/bonding/add",
            "delete": "/interfaces/bonding/delete",
        }
        self.assertTrue(_is_prerequisite_field(config, "/interfaces/bonding/add"))


class SetupSortTests(unittest.TestCase):
    def test_phase_and_add_action_order(self):
        scenario = {"setup": []}
        steps = [
            ("/interfaces/bonding/capability", "field", {"enslave": True}),
            ("/interfaces/eth_vlan/add", "interface", {"vid": "{{vid}}"}),
            ("/interfaces/bonding/add", "interface", {}),
            ("/vrf", "prerequisite", {"action": "add"}),
        ]
        for endpoint, phase, payload in steps:
            _append_setup_step(
                scenario,
                {
                    "endpoint": endpoint,
                    "method": "POST",
                    "payload": payload,
                    "expected_status": 200,
                },
                phase=phase,
            )
        _sort_scenario_setup(scenario)
        ordered = [step["endpoint"] for step in scenario["setup"]]
        self.assertEqual(
            ordered,
            [
                "/vrf",
                "/interfaces/eth_vlan/add",
                "/interfaces/bonding/add",
                "/interfaces/bonding/capability",
            ],
        )

    def test_equal_priority_preserves_insertion_order(self):
        scenario = {"setup": []}
        default_prio = _SETUP_PHASE_ORDER["field"] * 10 + 1
        for idx in range(3):
            _append_setup_step(
                scenario,
                {
                    "endpoint": f"/custom/step/{idx}",
                    "method": "POST",
                    "payload": {},
                    "expected_status": 200,
                    "setup_priority": default_prio,
                },
            )
        _sort_scenario_setup(scenario)
        ordered = [step["endpoint"] for step in scenario["setup"]]
        self.assertEqual(
            ordered,
            ["/custom/step/0", "/custom/step/1", "/custom/step/2"],
        )


class TeardownSortTests(unittest.TestCase):
    def test_lower_priority_runs_first(self):
        scenario = {"teardown": []}
        _append_teardown_step(
            scenario,
            {"endpoint": "/vrf", "method": "POST", "payload": {}},
            _TEARDOWN_PRIORITY_PREREQUISITE,
        )
        _append_teardown_step(
            scenario,
            {"endpoint": "/interfaces/bonding/delete", "method": "POST", "payload": {}},
            _TEARDOWN_PRIORITY_INTERFACE,
        )
        _sort_scenario_teardown(scenario)
        ordered = [s["endpoint"] for s in scenario["teardown"]]
        self.assertEqual(ordered[0], "/interfaces/bonding/delete")
        self.assertEqual(ordered[1], "/vrf")

    def test_explicit_teardown_priority_in_config(self):
        config = {"teardown_priority": 5}
        self.assertEqual(
            _resolve_teardown_priority(config, "/any"),
            5,
        )


class InterfaceDeferTests(unittest.TestCase):
    def test_setup_defer_from_dependencies(self):
        iface_rules = {
            "ifname": {
                "rules": [
                    {"pattern": "^eth1\\.1$", "env": "DEVICE_ETH_VLAN_IFNAMES"},
                    {"prefix": "bond", "setup_defer": True, "create": "/interfaces/bonding/add"},
                ],
            },
        }
        self.assertEqual(_interface_deferred_prefixes(iface_rules), frozenset({"bond"}))
        self.assertEqual(_interface_setup_sort_key("eth1.1", iface_rules), (0, "eth1.1"))
        self.assertEqual(_interface_setup_sort_key("bond0", iface_rules), (1, "bond0"))


class VidResolutionTests(unittest.TestCase):
    def test_infer_vid_from_vlan_ifname(self):
        self.assertEqual(_infer_vid_from_ifname("vlan100"), 100)
        self.assertEqual(_infer_vid_from_ifname("eth1.200"), 200)

    def test_parse_vid_pool_range(self):
        self.assertEqual(_parse_vid_pool({"VID_RANGE": "2-4"}), [2, 3, 4])

    def test_resolve_vid_from_pool_when_name_has_no_vid(self):
        pool = _VidPool({"VID_RANGE": "50-52"})
        self.assertEqual(_resolve_vid("bond0", vid_pool=pool), 50)
        self.assertEqual(_resolve_vid("br0", vid_pool=pool), 51)

    def test_lifecycle_vars_include_vid_and_vlan(self):
        result = _lifecycle_vars("ifname", "vlan62", {})
        self.assertEqual(result["vid"], 62)
        self.assertEqual(result["vlan"], "62")

    def test_synchronize_vid_ifname_overwrites_mismatched_vid(self):
        payload = {"ifname": "vlan4092", "vid": 1926}
        synced = synchronize_vid_ifname(payload)
        self.assertEqual(synced["vid"], 4092)
        self.assertEqual(payload["vid"], 1926)

    def test_synchronize_vid_ifname_eth_vlan(self):
        synced = synchronize_vid_ifname({"ifname": "eth1.200", "vid": 999})
        self.assertEqual(synced["vid"], 200)

    def test_synchronize_vid_ifname_skips_vid_without_schema(self):
        """Эндпоинты без vid в схеме (напр. /interfaces/common/arp) не получают лишний vid."""
        arp_schema = {
            "type": "object",
            "required": ["ifname"],
            "properties": {
                "ifname": {"type": "string", "enum": ["vlan100"]},
                "announce": {"type": "string", "enum": ["any"]},
            },
            "additionalProperties": False,
        }
        synced = synchronize_vid_ifname({"ifname": "vlan100", "announce": "any"}, arp_schema)
        self.assertNotIn("vid", synced)

    def test_synchronize_vid_ifname_adds_vid_when_schema_declares_it(self):
        vlan_schema = {
            "type": "object",
            "required": ["ifname", "vid"],
            "properties": {
                "ifname": {"type": "string", "enum": ["vlan4092"]},
                "vid": {"type": "integer"},
            },
        }
        synced = synchronize_vid_ifname({"ifname": "vlan4092", "vid": 1926}, vlan_schema)
        self.assertEqual(synced["vid"], 4092)

    def test_lifecycle_vars_prefer_synced_context_vid(self):
        ctx = {"ifname": "vlan4092", "vid": 4092}
        result = _lifecycle_vars(
            "ifname",
            "vlan4092",
            {_PLACEHOLDER_CONTEXT_KEY: ctx, "ifname": "vlan4092"},
        )
        self.assertEqual(result["vid"], 4092)
        self.assertEqual(result["vlan"], "4092")


class InventoryFilterTests(unittest.TestCase):
    def test_blocked_eth_parents_from_vlan_children(self):
        inventory = [{"names": ["eth1.1", "eth1.2"], "pattern": r"^eth\d+\.\d+$"}]
        blocked = build_eth_parents_with_vlan_children(inventory)
        self.assertEqual(blocked, {"eth1"})


if __name__ == "__main__":
    unittest.main()

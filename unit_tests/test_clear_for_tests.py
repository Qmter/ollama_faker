"""clear_for_tests.py — сбор и сортировка teardown."""

import unittest

from clear_for_tests import (
    collect_unique_teardown_steps,
    infer_teardown_priority,
    sort_teardown_steps,
    teardown_step_fingerprint,
)


class TeardownCollectTests(unittest.TestCase):
    def test_dedupes_identical_steps(self):
        scenarios = [
            {
                "teardown": [
                    {
                        "endpoint": "/interfaces/vlan/delete",
                        "method": "POST",
                        "payload": {"ifname": "vlan100"},
                    },
                ],
            },
            {
                "teardown": [
                    {
                        "endpoint": "/interfaces/vlan/delete",
                        "method": "POST",
                        "payload": {"ifname": "vlan100"},
                    },
                ],
            },
        ]
        steps = collect_unique_teardown_steps(scenarios)
        self.assertEqual(len(steps), 1)

    def test_keeps_different_payloads(self):
        scenarios = [
            {
                "teardown": [
                    {
                        "endpoint": "/interfaces/vlan/delete",
                        "payload": {"ifname": "vlan100"},
                    },
                    {
                        "endpoint": "/interfaces/vlan/delete",
                        "payload": {"ifname": "vlan200"},
                    },
                ],
            },
        ]
        steps = collect_unique_teardown_steps(scenarios)
        self.assertEqual(len(steps), 2)


class TeardownSortTests(unittest.TestCase):
    def test_tunnel_before_vrf(self):
        steps = [
            {
                "endpoint": "/vrf",
                "payload": {"action": "delete", "vrf_name": "test_vrf_1"},
            },
            {
                "endpoint": "/interfaces/tunnel/delete",
                "payload": {"ifname": "tunnel0"},
            },
            {
                "endpoint": "/interfaces/vlan/delete",
                "payload": {"ifname": "vlan100"},
            },
        ]
        ordered = sort_teardown_steps(steps)
        self.assertEqual(ordered[0]["endpoint"], "/interfaces/tunnel/delete")
        self.assertEqual(ordered[1]["endpoint"], "/interfaces/vlan/delete")
        self.assertEqual(ordered[2]["endpoint"], "/vrf")

    def test_fingerprint_stable(self):
        step = {
            "endpoint": "/interfaces/vlan/delete",
            "method": "POST",
            "payload": {"ifname": "vlan100"},
        }
        self.assertEqual(
            teardown_step_fingerprint(step),
            teardown_step_fingerprint(step),
        )


class TeardownPriorityTests(unittest.TestCase):
    def test_vlan_priority(self):
        self.assertLess(
            infer_teardown_priority({
                "endpoint": "/interfaces/vlan/delete",
                "payload": {"ifname": "vlan100"},
            }),
            infer_teardown_priority({
                "endpoint": "/vrf",
                "payload": {"action": "delete", "vrf_name": "x"},
            }),
        )


if __name__ == "__main__":
    unittest.main()

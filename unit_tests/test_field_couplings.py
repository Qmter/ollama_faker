"""field_couplings: связка полей в payload."""

import logging
import unittest

from main import apply_field_couplings, parse_field_couplings


class ParseFieldCouplingsTests(unittest.TestCase):
    def test_parses_valid_rule(self):
        deps = {
            "field_couplings": [
                {
                    "endpoints": ["/interfaces/tunnel/add"],
                    "when": {"path": "settings.mode", "in": ["gretap"]},
                    "ensure": {
                        "settings.destination": {"from_mock": "destination"},
                    },
                    "only_if_missing": True,
                },
            ],
        }
        rules = parse_field_couplings(deps)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["when"]["path"], "settings.mode")
        self.assertEqual(rules[0]["when"]["in"], ["gretap"])

    def test_skips_invalid_rule(self):
        deps = {
            "field_couplings": [
                {"when": {"path": "x"}, "ensure": {}},
                "not-an-object",
            ],
        }
        self.assertEqual(parse_field_couplings(deps), [])


class ApplyFieldCouplingsTests(unittest.TestCase):
    def setUp(self):
        self.couplings = parse_field_couplings({
            "field_couplings": [
                {
                    "endpoints": ["/interfaces/tunnel/add"],
                    "when": {
                        "path": "settings.mode",
                        "in": ["gretap", "gre", "ipip", "sit"],
                    },
                    "ensure": {
                        "settings.destination": {"from_mock": "destination"},
                    },
                    "only_if_missing": True,
                },
            ],
        })
        self.mock = {"destination": ["10.0.0.2", "10.0.0.3"]}

    def test_adds_destination_for_gretap(self):
        payload = {
            "ifname": "tunnel1",
            "settings": {"source": "10.0.0.1", "mode": "gretap"},
        }
        result = apply_field_couplings(
            payload,
            endpoint="/interfaces/tunnel/add",
            couplings=self.couplings,
            mock_by_field=self.mock,
        )
        self.assertEqual(result["settings"]["destination"], "10.0.0.2")
        self.assertEqual(payload["settings"].get("destination"), None)

    def test_does_not_overwrite_existing_destination(self):
        payload = {
            "ifname": "tunnel1",
            "settings": {
                "source": "10.0.0.1",
                "mode": "gretap",
                "destination": "9.9.9.9",
            },
        }
        result = apply_field_couplings(
            payload,
            endpoint="/interfaces/tunnel/add",
            couplings=self.couplings,
            mock_by_field=self.mock,
        )
        self.assertEqual(result["settings"]["destination"], "9.9.9.9")

    def test_other_mode_no_change(self):
        payload = {
            "ifname": "tunnel1",
            "settings": {"source": "10.0.0.1", "mode": "vxlan"},
        }
        result = apply_field_couplings(
            payload,
            endpoint="/interfaces/tunnel/add",
            couplings=self.couplings,
            mock_by_field=self.mock,
        )
        self.assertNotIn("destination", result["settings"])

    def test_endpoint_filter(self):
        payload = {
            "ifname": "tunnel1",
            "settings": {"mode": "gretap"},
        }
        result = apply_field_couplings(
            payload,
            endpoint="/interfaces/vlan/add",
            couplings=self.couplings,
            mock_by_field=self.mock,
        )
        self.assertNotIn("destination", result.get("settings", {}))

    def test_from_mock_missing_leaves_payload(self):
        payload = {
            "ifname": "tunnel1",
            "settings": {"mode": "gretap"},
        }
        with self.assertLogs(level=logging.WARNING) as captured:
            result = apply_field_couplings(
                payload,
                endpoint="/interfaces/tunnel/add",
                couplings=self.couplings,
                mock_by_field={},
            )
        self.assertNotIn("destination", result["settings"])
        self.assertTrue(
            any("mock_data.by_field" in line for line in captured.output),
        )

    def test_literal_value_and_present_when(self):
        couplings = parse_field_couplings({
            "field_couplings": [
                {
                    "when": {"path": "peer", "present": True},
                    "ensure": {"mask": {"value": "24"}},
                },
            ],
        })
        result = apply_field_couplings(
            {"peer": "10.0.0.1"},
            endpoint="/any",
            couplings=couplings,
            mock_by_field={},
        )
        self.assertEqual(result["mask"], "24")


if __name__ == "__main__":
    unittest.main()

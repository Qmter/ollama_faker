"""Генерация payload, покрытие значений, dedupe."""

import unittest

from main import (
    PayloadCoverage,
    _build_payload_for_path,
    _payload_fingerprint,
    collect_test_values,
    dedupe_payloads,
    extract_all_fields,
    generate_value_coverage_payloads,
    get_payload_fields,
    set_field_test_value,
)


class CollectTestValuesTests(unittest.TestCase):
    def test_enum_returns_all_values(self):
        values = collect_test_values({"type": "string", "enum": ["a", "b", "c"]})
        self.assertEqual(values, ["a", "b", "c"])

    def test_boolean_returns_true_false(self):
        values = collect_test_values({"type": "boolean"})
        self.assertEqual(values, [True, False])

    def test_integer_uses_bounds(self):
        values = collect_test_values({"type": "integer", "minimum": 1, "maximum": 3})
        self.assertIn(1, values)
        self.assertIn(3, values)


class PayloadBuildingTests(unittest.TestCase):
    def test_build_payload_for_path_nested(self):
        schema = {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "object",
                    "properties": {"mode_type": {"type": "string", "enum": ["rr"]}},
                },
            },
        }
        payload = _build_payload_for_path(schema, "mode.mode_type", "rr")
        self.assertEqual(payload["mode"]["mode_type"], "rr")

    def test_set_field_test_value(self):
        schema = {
            "type": "object",
            "properties": {"ifname": {"type": "string"}},
        }
        obj = {}
        set_field_test_value(obj, schema, "ifname", "eth1")
        self.assertEqual(obj["ifname"], "eth1")

    def test_get_payload_fields_recursive(self):
        fields = get_payload_fields({
            "ifname": "eth1",
            "mode": {"mode_type": "rr"},
            "tags": ["a"],
        })
        self.assertIn("ifname", fields)
        self.assertIn("mode_type", fields)


class CoverageGenerationTests(unittest.TestCase):
    def test_generate_value_coverage_for_simple_schema(self):
        schema = {
            "type": "object",
            "required": ["adm_state"],
            "properties": {
                "ifname": {"type": "string", "enum": ["eth1"]},
                "adm_state": {"type": "boolean"},
            },
            "additionalProperties": False,
        }
        records = generate_value_coverage_payloads(schema)
        self.assertGreater(len(records), 0)
        self.assertTrue(all(isinstance(r, PayloadCoverage) for r in records))

    def test_extract_all_fields(self):
        schema = {
            "type": "object",
            "properties": {
                "ifname": {"type": "string"},
                "mtu": {"type": "integer"},
            },
        }
        fields = extract_all_fields(schema)
        self.assertIn("ifname", fields)
        self.assertIn("mtu", fields)


class DedupeTests(unittest.TestCase):
    def test_dedupe_identical_payloads(self):
        records = [
            PayloadCoverage({"ifname": "eth1"}, ["k1"]),
            PayloadCoverage({"ifname": "eth1"}, ["k2"]),
        ]
        result = dedupe_payloads(records)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].coverage_keys), 2)

    def test_fingerprint_stable(self):
        fp1 = _payload_fingerprint({"a": 1, "b": 2})
        fp2 = _payload_fingerprint({"b": 2, "a": 1})
        self.assertEqual(fp1, fp2)


if __name__ == "__main__":
    unittest.main()

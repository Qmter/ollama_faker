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

    def test_discriminated_oneof_covers_modulus_with_matching_key_type(self):
        """Корневой oneOf по key_type: modulus/key_name не должны теряться из‑за JSF."""
        from main import preprocess_schema_for_jsf, _build_payload_for_path

        schema = preprocess_schema_for_jsf({
            "required": ["key_type"],
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "key_type": {"type": "string"},
                "modulus": {"type": "integer"},
                "key_name": {"type": "string", "pattern": "^[A-Za-z0-9_-]+$"},
            },
            "oneOf": [
                {
                    "properties": {
                        "key_type": {"const": "rsa"},
                        "modulus": {"enum": [1024, 2048, 4096]},
                    },
                },
                {
                    "properties": {
                        "key_type": {"const": "dsa"},
                        "modulus": {"const": 1024},
                    },
                },
                {
                    "properties": {
                        "key_type": {"const": "ecdsa"},
                        "modulus": {"enum": [256, 384, 521]},
                    },
                },
                {
                    "not": {"required": ["modulus"]},
                    "properties": {
                        "key_type": {"const": "ed25519"},
                    },
                },
            ],
        })

        rsa_mod = _build_payload_for_path(schema, "modulus", 2048)
        self.assertEqual(rsa_mod["key_type"], "rsa")
        self.assertEqual(rsa_mod["modulus"], 2048)

        ecdsa_mod = _build_payload_for_path(schema, "modulus", 256)
        self.assertEqual(ecdsa_mod["key_type"], "ecdsa")
        self.assertEqual(ecdsa_mod["modulus"], 256)

        named = _build_payload_for_path(schema, "key_name", "autotest-key")
        self.assertIn(named["key_type"], {"rsa", "dsa", "ecdsa", "ed25519"})
        self.assertEqual(named["key_name"], "autotest-key")
        if named["key_type"] == "ed25519":
            self.assertNotIn("modulus", named)

        records = generate_value_coverage_payloads(schema)
        keys = {k for r in records for k in r.coverage_keys}
        self.assertIn('key_type="rsa"', keys)
        self.assertIn('key_type="ed25519"', keys)
        self.assertIn("modulus=2048", keys)
        self.assertIn("modulus=256", keys)
        self.assertTrue(any(k.startswith("key_name=") for k in keys))
        self.assertNotIn("modulus=1", keys)

        by_mod = {
            r.payload.get("modulus"): r.payload.get("key_type")
            for r in records
            if "modulus" in r.payload
        }
        self.assertEqual(by_mod.get(2048), "rsa")
        self.assertEqual(by_mod.get(256), "ecdsa")


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

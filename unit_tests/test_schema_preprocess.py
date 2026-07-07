"""Препроцессинг схемы, валидация, minimal payload."""

import unittest

from main import (
    _enrich_optional_null_fields,
    _expand_nullable_schema,
    _is_valid_for_schema,
    _schema_allows_null,
    _validate_payload,
    build_minimal_payload,
    preprocess_schema_for_jsf,
)


class PreprocessSchemaTests(unittest.TestCase):
    def test_adds_object_type_when_properties_without_type(self):
        schema = {"properties": {"name": {"type": "string"}}, "required": ["name"]}
        result = preprocess_schema_for_jsf(schema)
        self.assertEqual(result["type"], "object")

    def test_oneof_const_becomes_enum(self):
        schema = {
            "oneOf": [
                {"const": "add"},
                {"const": "delete"},
            ],
        }
        result = preprocess_schema_for_jsf(schema)
        self.assertEqual(result["enum"], ["add", "delete"])
        self.assertNotIn("oneOf", result)

    def test_nullable_oneof_unwraps_inner_schema(self):
        schema = {
            "oneOf": [
                {"type": "string"},
                {"type": "null"},
            ],
        }
        result = preprocess_schema_for_jsf(schema)
        self.assertEqual(result["type"], "string")
        self.assertTrue(result.get("x-nullable"))


class NullableSchemaTests(unittest.TestCase):
    def test_x_nullable_expands_to_oneof(self):
        expanded = _expand_nullable_schema({"type": "string", "x-nullable": True})
        types = [b.get("type") for b in expanded["oneOf"]]
        self.assertIn("string", types)
        self.assertIn("null", types)

    def test_schema_allows_null_for_union_type(self):
        self.assertTrue(_schema_allows_null({"type": ["string", "null"]}))


class MinimalPayloadTests(unittest.TestCase):
    def test_build_minimal_required_fields(self):
        schema = {
            "type": "object",
            "required": ["ifname", "mtu"],
            "properties": {
                "ifname": {"type": "string"},
                "mtu": {"type": "integer", "minimum": 68, "maximum": 1500},
            },
            "additionalProperties": False,
        }
        payload = build_minimal_payload(schema)
        self.assertIn("ifname", payload)
        self.assertIn("mtu", payload)

    def test_enrich_optional_null_fields(self):
        schema = {
            "type": "object",
            "required": ["ifname"],
            "properties": {
                "ifname": {"type": "string"},
                "vrf_name": {
                    "oneOf": [{"type": "string"}, {"type": "null"}],
                },
            },
        }
        payload = _enrich_optional_null_fields({"ifname": "eth1"}, schema)
        self.assertIsNone(payload["vrf_name"])

    def test_validate_payload_accepts_minimal(self):
        schema = {
            "type": "object",
            "required": ["ifname"],
            "properties": {"ifname": {"type": "string", "enum": ["eth1"]}},
            "additionalProperties": False,
        }
        payload = {"ifname": "eth1"}
        ok, msg = _validate_payload(payload, schema)
        self.assertTrue(ok, msg)
        self.assertTrue(_is_valid_for_schema(payload, schema))


if __name__ == "__main__":
    unittest.main()

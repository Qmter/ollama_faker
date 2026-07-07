"""Авто lifecycle для action.add + scalar action.delete."""

import unittest

from main import (
    _build_auto_add_setup_payload,
    _id_value_from_add_payload,
    _scalar_delete_value_from_payload,
    detect_scalar_delete_action_pattern,
)


class ScalarDeleteDetectionTests(unittest.TestCase):
    def _sample_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "oneOf": [
                        {
                            "properties": {
                                "add": {
                                    "type": "object",
                                    "required": ["pool_number"],
                                    "properties": {
                                        "pool_number": {"type": "integer"},
                                        "network": {"type": "string"},
                                    },
                                },
                            },
                        },
                        {
                            "properties": {
                                "delete": {"type": "integer"},
                            },
                        },
                    ],
                },
            },
        }

    def test_detect_scalar_delete_pattern(self):
        meta = detect_scalar_delete_action_pattern(self._sample_schema())
        self.assertIsNotNone(meta)
        self.assertEqual(meta["id_field"], "pool_number")

    def test_no_pattern_for_object_delete(self):
        schema = {
            "type": "object",
            "properties": {
                "action": {
                    "properties": {
                        "add": {"type": "object", "properties": {"x": {"type": "string"}}},
                        "delete": {"type": "object", "properties": {"x": {"type": "string"}}},
                    },
                },
            },
        }
        self.assertIsNone(detect_scalar_delete_action_pattern(schema))


class ScalarDeletePayloadTests(unittest.TestCase):
    def test_scalar_delete_value_from_payload(self):
        payload = {"action": {"delete": 42}}
        self.assertEqual(_scalar_delete_value_from_payload(payload), 42)

    def test_id_value_from_add_payload(self):
        payload = {"action": {"add": {"pool_number": 7, "network": "1.1.1.0/24"}}}
        self.assertEqual(_id_value_from_add_payload(payload, "pool_number"), 7)

    def test_build_auto_add_setup_payload(self):
        schema = {
            "type": "object",
            "required": ["pool_number"],
            "properties": {
                "pool_number": {"type": "integer"},
                "network": {"type": "string", "enum": ["1.1.1.0/24"]},
            },
        }
        payload = _build_auto_add_setup_payload(schema, "pool_number", 5)
        self.assertEqual(payload["action"]["add"]["pool_number"], 5)


if __name__ == "__main__":
    unittest.main()

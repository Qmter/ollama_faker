"""normalize_schema_field_relations: start/stop swap и drop optional по maximum."""

import unittest

from main import normalize_schema_field_relations


class OrderedPairsTests(unittest.TestCase):
    def test_swaps_start_stop_when_inverted(self):
        schema = {
            "type": "object",
            "properties": {
                "length": {
                    "type": "object",
                    "required": ["start_length_value"],
                    "properties": {
                        "start_length_value": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 65535,
                        },
                        "stop_length_value": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 65535,
                        },
                    },
                },
            },
        }
        payload = {
            "length": {
                "start_length_value": 53524,
                "stop_length_value": 24599,
            },
        }
        result = normalize_schema_field_relations(payload, schema)
        self.assertEqual(result["length"]["start_length_value"], 24599)
        self.assertEqual(result["length"]["stop_length_value"], 53524)

    def test_leaves_ordered_start_stop(self):
        payload = {
            "length": {
                "start_length_value": 100,
                "stop_length_value": 200,
            },
        }
        result = normalize_schema_field_relations(payload, None)
        self.assertEqual(result["length"]["start_length_value"], 100)
        self.assertEqual(result["length"]["stop_length_value"], 200)

    def test_swaps_min_max_suffix_pair(self):
        payload = {"min_foo": 50, "max_foo": 10}
        result = normalize_schema_field_relations(payload, None)
        self.assertEqual(result["min_foo"], 10)
        self.assertEqual(result["max_foo"], 50)


class DropOptionalByMaximumTests(unittest.TestCase):
    def _hash_limit_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "hash_limit": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["rate_above_value", "hash_limit_name"],
                    "properties": {
                        "rate_above_value": {
                            "type": "integer",
                            "minimum": 8,
                            "maximum": 33554431,
                        },
                        "h1_burst_value": {
                            "type": "integer",
                            "minimum": 8,
                            "maximum": 4095,
                        },
                        "hash_limit_name": {"type": "string"},
                    },
                },
            },
        }

    def test_drops_burst_when_rate_exceeds_burst_maximum(self):
        schema = self._hash_limit_schema()
        payload = {
            "hash_limit": {
                "rate_above_value": 30631154,
                "h1_burst_value": 3785,
                "hash_limit_name": "test",
            },
        }
        result = normalize_schema_field_relations(payload, schema)
        self.assertNotIn("h1_burst_value", result["hash_limit"])
        self.assertEqual(result["hash_limit"]["rate_above_value"], 30631154)
        self.assertEqual(result["hash_limit"]["hash_limit_name"], "test")

    def test_keeps_burst_when_rate_within_burst_maximum(self):
        schema = self._hash_limit_schema()
        payload = {
            "hash_limit": {
                "rate_above_value": 1000,
                "h1_burst_value": 2000,
                "hash_limit_name": "test",
            },
        }
        result = normalize_schema_field_relations(payload, schema)
        self.assertEqual(result["hash_limit"]["h1_burst_value"], 2000)

    def test_does_not_drop_required_narrow_field(self):
        schema = {
            "type": "object",
            "required": ["narrow", "wide"],
            "properties": {
                "narrow": {"type": "integer", "maximum": 100},
                "wide": {"type": "integer", "maximum": 100000},
            },
        }
        payload = {"narrow": 50, "wide": 50000}
        result = normalize_schema_field_relations(payload, schema)
        self.assertEqual(result["narrow"], 50)
        self.assertEqual(result["wide"], 50000)

    def test_nested_under_action_rule(self):
        schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "object",
                    "properties": {
                        "add": {
                            "type": "object",
                            "properties": {
                                "rule": {
                                    "type": "object",
                                    "properties": {
                                        "length": {
                                            "type": "object",
                                            "properties": {
                                                "start_length_value": {
                                                    "type": "integer",
                                                    "maximum": 65535,
                                                },
                                                "stop_length_value": {
                                                    "type": "integer",
                                                    "maximum": 65535,
                                                },
                                            },
                                        },
                                        "hash_limit": {
                                            "type": "object",
                                            "required": [
                                                "rate_above_value",
                                                "hash_limit_name",
                                            ],
                                            "properties": {
                                                "rate_above_value": {
                                                    "type": "integer",
                                                    "maximum": 33554431,
                                                },
                                                "h1_burst_value": {
                                                    "type": "integer",
                                                    "maximum": 4095,
                                                },
                                                "hash_limit_name": {
                                                    "type": "string",
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
        payload = {
            "action": {
                "add": {
                    "rule": {
                        "length": {
                            "start_length_value": 53524,
                            "stop_length_value": 24599,
                        },
                        "hash_limit": {
                            "rate_above_value": 30631154,
                            "h1_burst_value": 3785,
                            "hash_limit_name": "x",
                        },
                    },
                },
            },
        }
        result = normalize_schema_field_relations(payload, schema)
        rule = result["action"]["add"]["rule"]
        self.assertEqual(rule["length"]["start_length_value"], 24599)
        self.assertEqual(rule["length"]["stop_length_value"], 53524)
        self.assertNotIn("h1_burst_value", rule["hash_limit"])


if __name__ == "__main__":
    unittest.main()

"""Подстановка {{placeholder}} в setup/teardown/main."""

import unittest

from main import (
    _PLACEHOLDER_CONTEXT_KEY,
    _get_nested_placeholder_value,
    _replace_placeholders,
    _resolve_placeholder,
)


class PlaceholderTests(unittest.TestCase):
    def test_flat_placeholder_from_variables(self):
        result = _replace_placeholders(
            {"ifname": "{{ifname}}"},
            {"ifname": "bond0"},
        )
        self.assertEqual(result["ifname"], "bond0")

    def test_dotted_placeholder_from_context(self):
        payload = {"settings": {"source": "1.1.1.1"}}
        variables = {_PLACEHOLDER_CONTEXT_KEY: payload}
        result = _replace_placeholders(
            {"source": "{{settings.source}}"},
            variables,
        )
        self.assertEqual(result["source"], "1.1.1.1")

    def test_whole_placeholder_preserves_type(self):
        result = _replace_placeholders("{{vid}}", {"vid": 42})
        self.assertEqual(result, 42)
        self.assertIsInstance(result, int)

    def test_unresolved_placeholder_left_unchanged(self):
        result = _replace_placeholders("{{missing}}", {})
        self.assertEqual(result, "{{missing}}")

    def test_nested_dict_substitution(self):
        result = _replace_placeholders(
            {
                "capability": {
                    "enslave": ["{{primary_interface}}"],
                },
            },
            {"primary_interface": "eth1"},
        )
        self.assertEqual(result["capability"]["enslave"], ["eth1"])

    def test_get_nested_placeholder_value(self):
        obj = {"mode": {"mode_type": "rr"}}
        self.assertEqual(
            _get_nested_placeholder_value(obj, "mode.mode_type"),
            "rr",
        )

    def test_resolve_placeholder_returns_string(self):
        self.assertEqual(_resolve_placeholder("ifname", {"ifname": "eth1"}, None), "eth1")


if __name__ == "__main__":
    unittest.main()

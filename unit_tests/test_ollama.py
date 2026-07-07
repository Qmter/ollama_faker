"""OllamaOrchestrator: fallback без сети."""

import unittest

from ollama_orchestrator import (
    OllamaOrchestrator,
    _fallback_name,
    _fits_field_schema,
    _parse_json_object,
    _should_enrich_field,
)


class OllamaDisabledTests(unittest.TestCase):
    def test_from_cli_disabled(self):
        orch = OllamaOrchestrator.from_cli(False)
        self.assertFalse(orch.enabled)
        self.assertFalse(orch.has_feature("describe"))

    def test_generate_raises_when_disabled(self):
        orch = OllamaOrchestrator(enabled=False)
        with self.assertRaises(RuntimeError):
            orch.generate("prompt")


class OllamaHelperTests(unittest.TestCase):
    def test_parse_json_object_from_text(self):
        parsed = _parse_json_object('prefix {"a": 1} suffix')
        self.assertEqual(parsed, {"a": 1})

    def test_fallback_name_unique(self):
        used = set()
        n1 = _fallback_name("ifname", 0, 0, used)
        n2 = _fallback_name("ifname", 0, 1, used)
        self.assertNotEqual(n1, n2)
        self.assertTrue(n1.startswith("test_ifname_"))

    def test_should_enrich_skips_enum(self):
        schema = {"enum": ["eth1", "eth2"]}
        self.assertFalse(_should_enrich_field("ifname", "eth1", schema))

    def test_fits_field_schema_enum(self):
        self.assertTrue(_fits_field_schema("eth1", {"enum": ["eth1", "eth2"]}))
        self.assertFalse(_fits_field_schema("eth9", {"enum": ["eth1"]}))


if __name__ == "__main__":
    unittest.main()

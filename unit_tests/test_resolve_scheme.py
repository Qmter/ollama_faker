"""ResolveScheme: $ref, extract_field_schemas, rules."""

import unittest

from resolve_scheme import ResolveScheme


COMPONENTS = {
    "schemas": {
        "PetName": {
            "type": "string",
            "enum": ["cat", "dog"],
        },
        "Pet": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"$ref": "#/components/schemas/PetName"},
                "age": {"type": "integer", "minimum": 0, "maximum": 30},
            },
        },
    },
}


class ResolveRefTests(unittest.TestCase):
    def test_resolve_simple_ref(self):
        obj = {"$ref": "#/components/schemas/PetName"}
        resolved = ResolveScheme._resolve_ref(obj, COMPONENTS)
        self.assertEqual(resolved["enum"], ["cat", "dog"])

    def test_merge_ref_with_siblings(self):
        obj = {
            "$ref": "#/components/schemas/PetName",
            "description": "override",
        }
        resolved = ResolveScheme._resolve_ref(obj, COMPONENTS)
        self.assertEqual(resolved["enum"], ["cat", "dog"])
        self.assertEqual(resolved["description"], "override")

    def test_circular_ref_marked(self):
        components = {
            "schemas": {
                "A": {"$ref": "#/components/schemas/B"},
                "B": {"$ref": "#/components/schemas/A"},
            },
        }
        resolved = ResolveScheme._resolve_ref(
            {"$ref": "#/components/schemas/A"},
            components,
        )
        self.assertTrue(resolved.get("x-circular"))


class ExtractFieldSchemasTests(unittest.TestCase):
    def test_extracts_nested_properties(self):
        schema = ResolveScheme._resolve_ref(
            {"$ref": "#/components/schemas/Pet"},
            COMPONENTS,
        )
        fields = ResolveScheme.extract_field_schemas(schema)
        self.assertIn("name", fields)
        self.assertIn("age", fields)

    def test_find_patterns_min_max(self):
        schema = ResolveScheme._resolve_ref(
            {"$ref": "#/components/schemas/Pet"},
            COMPONENTS,
        )
        rules = ResolveScheme.find_all_patterns_min_max(schema)
        self.assertIn("age", rules)
        self.assertEqual(rules["age"]["minimum"], 0)
        self.assertEqual(rules["age"]["maximum"], 30)


class MergeSchemaTests(unittest.TestCase):
    def test_merge_required_lists(self):
        base = {"type": "object", "required": ["a"]}
        overlay = {"required": ["b"], "properties": {"b": {"type": "string"}}}
        merged = ResolveScheme._merge_resolved_schema(base, overlay)
        self.assertEqual(merged["required"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()

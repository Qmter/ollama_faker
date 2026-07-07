"""VID_RANGE_LIST и семантическая валидация."""

import unittest

from main import (
    _is_semantically_valid_vid_range_list,
    _is_vid_range_list_schema,
    _vid_range_list_test_values,
    collect_test_values,
)


class VidRangeListTests(unittest.TestCase):
    def test_detects_vid_range_list_schema(self):
        schema = {
            "type": "string",
            "description": "VLAN ID range",
            "pattern": "anything",
        }
        self.assertTrue(_is_vid_range_list_schema(schema))

    def test_validates_range_syntax(self):
        self.assertTrue(_is_semantically_valid_vid_range_list("10"))
        self.assertTrue(_is_semantically_valid_vid_range_list("10-20,30"))
        self.assertFalse(_is_semantically_valid_vid_range_list("0"))
        self.assertFalse(_is_semantically_valid_vid_range_list("10-5"))

    def test_collect_test_values_for_vid_range(self):
        schema = {
            "type": "string",
            "description": "VLAN ID list",
        }
        values = collect_test_values(schema)
        self.assertGreater(len(values), 0)
        for value in values:
            if isinstance(value, str):
                self.assertTrue(_is_semantically_valid_vid_range_list(value))

    def test_builtin_test_values_nonempty(self):
        self.assertGreater(len(_vid_range_list_test_values()), 0)


if __name__ == "__main__":
    unittest.main()

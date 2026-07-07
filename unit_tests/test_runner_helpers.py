"""Вспомогательные функции run_tests.py (без HTTP)."""

import unittest
from pathlib import Path

from run_tests import (
    StepResult,
    _is_already_exists_error,
    _resolve_base_url,
    _response_text,
    endpoint_to_test_file,
)


class EndpointToFileTests(unittest.TestCase):
    def test_maps_endpoint_to_filename(self):
        path = endpoint_to_test_file("/interfaces/bonding/mode", Path("tests"))
        self.assertEqual(path.name, "interfaces_bonding_mode_post.json")


class BaseUrlTests(unittest.TestCase):
    def test_from_env_file(self):
        url = _resolve_base_url(None, {"API_BASE_URL": "https://10.0.0.1:8082/"})
        self.assertEqual(url, "https://10.0.0.1:8082")

    def test_cli_overrides_env(self):
        url = _resolve_base_url("http://cli", {"API_BASE_URL": "http://env"})
        self.assertEqual(url, "http://cli")


class AlreadyExistsTests(unittest.TestCase):
    def test_detects_already_exists_in_response(self):
        step = StepResult(
            phase="setup",
            step_index=1,
            endpoint="/x",
            method="POST",
            url="http://x",
            request_payload={},
            status_code=400,
            expected_status=200,
            response_body={"errCode": [9, "Interface eth1.1 already exists"]},
            elapsed_ms=1.0,
            passed=False,
        )
        self.assertTrue(_is_already_exists_error(step))

    def test_no_false_positive(self):
        step = StepResult(
            phase="setup",
            step_index=1,
            endpoint="/x",
            method="POST",
            url="http://x",
            request_payload={},
            status_code=500,
            expected_status=200,
            response_body={"errCode": [-1, "internal error"]},
            elapsed_ms=1.0,
            passed=False,
        )
        self.assertFalse(_is_already_exists_error(step))

    def test_response_text_from_string_body(self):
        step = StepResult(
            phase="main",
            step_index=1,
            endpoint="/x",
            method="POST",
            url="http://x",
            request_payload={},
            status_code=200,
            expected_status=200,
            response_body="Already Exist",
            elapsed_ms=1.0,
            passed=True,
        )
        self.assertIn("already exist", _response_text(step))


if __name__ == "__main__":
    unittest.main()

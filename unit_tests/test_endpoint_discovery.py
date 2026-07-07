"""Обнаружение эндпоинтов и фильтрация по префиксу."""

import unittest

from main import (
    discover_post_endpoints,
    resolve_endpoints_by_prefix,
    resolve_run_endpoints,
    resolve_target_endpoints,
)


SAMPLE_OPENAPI = {
    "paths": {
        "/interfaces/shutdown": {"post": {}},
        "/interfaces/bonding/add": {"post": {}},
        "/interfaces/bonding/mode": {"post": {}},
        "/vrf": {"post": {}, "get": {}},
        "/health": {"get": {}},
    },
}


class DiscoverEndpointsTests(unittest.TestCase):
    def test_discovers_only_post(self):
        endpoints = discover_post_endpoints(SAMPLE_OPENAPI)
        self.assertIn("/interfaces/shutdown", endpoints)
        self.assertIn("/vrf", endpoints)
        self.assertNotIn("/health", endpoints)

    def test_sorted_output(self):
        endpoints = discover_post_endpoints(SAMPLE_OPENAPI)
        self.assertEqual(endpoints, sorted(endpoints))


class ResolveEndpointsTests(unittest.TestCase):
    def setUp(self):
        self.all_eps = discover_post_endpoints(SAMPLE_OPENAPI)

    def test_all_when_no_filter(self):
        self.assertEqual(
            resolve_target_endpoints(None, self.all_eps),
            self.all_eps,
        )

    def test_specific_endpoints(self):
        result = resolve_target_endpoints(["/vrf"], self.all_eps)
        self.assertEqual(result, ["/vrf"])

    def test_prefix_filter(self):
        result = resolve_endpoints_by_prefix(["/interfaces"], self.all_eps)
        self.assertTrue(all(ep.startswith("/interfaces") for ep in result))
        self.assertEqual(len(result), 3)

    def test_resolve_run_with_dir_prefix(self):
        result = resolve_run_endpoints(
            requested=None,
            dir_prefixes=["/interfaces/bonding"],
            all_endpoints=self.all_eps,
        )
        self.assertEqual(
            result,
            ["/interfaces/bonding/add", "/interfaces/bonding/mode"],
        )


if __name__ == "__main__":
    unittest.main()

"""OllamaOrchestrator: анализ по одному FAIL без сети."""

import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from ollama_orchestrator import (
    CLASSIFICATION_RU,
    OllamaOrchestrator,
    _unwrap_failure_bundle,
    build_generation_analysis_context,
    build_run_analysis_context,
    extract_err_codes,
    format_run_report,
    heuristic_classify_failure,
    serialize_scenario_result,
)


class OllamaDisabledTests(unittest.TestCase):
    def test_from_cli_disabled(self):
        orch = OllamaOrchestrator.from_cli(False)
        self.assertFalse(orch.enabled)
        self.assertFalse(orch.is_available())

    def test_generate_raises_when_disabled(self):
        orch = OllamaOrchestrator(enabled=False)
        with self.assertRaises(RuntimeError):
            orch.generate("prompt")


class OllamaAnalysisTests(unittest.TestCase):
    def test_extract_err_codes_nested(self):
        body = {"data": {"errCode": ["E100", "E200"]}}
        self.assertEqual(extract_err_codes(body), ["E100", "E200"])

    def test_serialize_scenario_result_keeps_failed_steps(self):
        scenario = {
            "setup": [],
            "main_test": {"payload": {"ifname": "eth1"}},
            "teardown": [],
        }
        result = {
            "endpoint": "/interfaces/foo",
            "test_id": 3,
            "description": "test",
            "coverage_keys": ["ifname:eth1"],
            "steps": [
                {
                    "phase": "main",
                    "step_index": 1,
                    "endpoint": "/interfaces/foo",
                    "method": "POST",
                    "url": "http://x/interfaces/foo",
                    "request_payload": {"ifname": "eth1"},
                    "status_code": 400,
                    "expected_status": 200,
                    "response_body": {"errCode": "bad"},
                    "elapsed_ms": 12.3,
                    "passed": False,
                    "error": "status mismatch",
                },
            ],
        }
        data = serialize_scenario_result(result, scenario)
        self.assertEqual(data["test_id"], 3)
        self.assertEqual(len(data["failed_steps"]), 1)
        self.assertEqual(data["failed_steps"][0]["err_codes"], ["bad"])
        self.assertIn("scenario_definition", data)

    def test_unwrap_failed_scenario_record_dataclass(self):
        @dataclass
        class _Bundle:
            result: dict
            scenario: dict

        bundle = _Bundle(
            result={
                "endpoint": "/interfaces/foo",
                "test_id": 2,
                "description": "",
                "coverage_keys": [],
                "steps": [],
            },
            scenario={"main_test": {"payload": {}}},
        )
        result, scenario = _unwrap_failure_bundle(bundle)
        self.assertEqual(result["endpoint"], "/interfaces/foo")
        self.assertEqual(scenario, {"main_test": {"payload": {}}})

    def test_heuristic_minimal_missing_required_field(self):
        failure = {
            "endpoint": "/interfaces/description",
            "coverage_keys": ["__minimal__", 'ifname="vlan100"'],
            "scenario_definition": {
                "main_test": {
                    "payload": {"ifname": "vlan100"},
                },
            },
            "openapi_request_schema": {
                "required": ["ifname", "description"],
                "properties": {
                    "ifname": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
            "failed_steps": [
                {
                    "phase": "main",
                    "error": "'description'",
                    "response_body": {},
                },
            ],
        }
        verdict = heuristic_classify_failure(failure)
        self.assertEqual(verdict["classification"], "TEST_SETUP")
        self.assertEqual(
            verdict["classification_ru"],
            CLASSIFICATION_RU["TEST_SETUP"],
        )
        self.assertIn("description", verdict["reason_ru"])
        self.assertIn("Исправить", verdict["fix_ru"])
        self.assertEqual(verdict["severity"], "serious")

    def test_heuristic_tunnel_gretap_missing_destination(self):
        failure = {
            "endpoint": "/interfaces/tunnel/add",
            "coverage_keys": ['settings.mode="gretap"'],
            "scenario_definition": {
                "main_test": {
                    "payload": {
                        "ifname": "tunnel1107",
                        "settings": {"source": "10.0.0.1", "mode": "gretap"},
                    },
                },
            },
            "openapi_request_schema": {},
            "failed_steps": [
                {
                    "phase": "main",
                    "status_code": 400,
                    "expected_status": 200,
                    "request_payload": {
                        "ifname": "tunnel1107",
                        "settings": {"source": "10.0.0.1", "mode": "gretap"},
                    },
                    "response_body": {
                        "errCode": [9, "Destination address not defined"],
                    },
                    "error": "ожидался HTTP 200, получен HTTP 400",
                    "passed": False,
                },
            ],
        }
        verdict = heuristic_classify_failure(failure)
        self.assertEqual(verdict["classification"], "TEST_SETUP")
        self.assertIn("destination", verdict["short_reason_ru"].lower())
        self.assertNotIn("ожидался http", verdict["short_reason_ru"].lower())
        self.assertIn("destination", verdict["fix_ru"].lower())
        self.assertIn("field_couplings", verdict["fix_ru"].lower())
        self.assertIn("mock_data", verdict["fix_ru"].lower())
        self.assertIn("coverage", verdict["reason_ru"].lower())

    def test_analyze_run_fallback_without_ollama(self):
        bundle = {
            "result": {
                "endpoint": "/interfaces/foo",
                "test_id": 1,
                "description": "",
                "coverage_keys": [],
                "steps": [
                    {
                        "phase": "main",
                        "step_index": 1,
                        "endpoint": "/interfaces/foo",
                        "method": "POST",
                        "url": "http://x/interfaces/foo",
                        "request_payload": {"ifname": "eth1"},
                        "status_code": 500,
                        "expected_status": 200,
                        "response_body": {"errCode": "x", "message": "boom"},
                        "elapsed_ms": 1.0,
                        "passed": False,
                    },
                ],
            },
            "scenario": {
                "main_test": {"payload": {"ifname": "eth1"}},
                "setup": [],
                "teardown": [],
            },
        }
        context = build_run_analysis_context(
            failures=[bundle],
            summary={
                "total_scenarios": 2,
                "failed_scenarios": 1,
                "passed_scenarios": 1,
            },
            endpoint_results=[],
            run_log_path="logs/run_test.log",
            elapsed_sec=1.5,
            endpoints_count=1,
            openapi_path="openapi.json",
            dependencies_path="dependencies.json",
        )
        orch = OllamaOrchestrator(enabled=False)
        report = orch.analyze_run(context)
        self.assertIn("Прошло", report)
        self.assertIn("Не прошло", report)
        self.assertIn("/interfaces/foo", report)
        self.assertIn("Упавшие тесты", report)
        self.assertIn("Детальный разбор", report)
        self.assertIn("Запрос", report)
        self.assertIn("Ответ", report)
        self.assertIn("Диагноз", report)
        self.assertIn("Как исправить", report)
        self.assertIn("Coverage vs payload", report)
        self.assertIn("Критические", report)
        self.assertIn("Некритические", report)

    def test_format_run_report_structure(self):
        context = {
            "run_log_path": "logs/x.log",
            "elapsed_sec": 10,
            "endpoints_count": 1,
            "summary": {
                "passed_scenarios": 5,
                "failed_scenarios": 1,
                "total_scenarios": 6,
            },
            "failures": [
                {
                    "endpoint": "/interfaces/description",
                    "test_id": 1,
                    "coverage_keys": ["__minimal__"],
                    "description": "t",
                    "failed_steps": [
                        {
                            "phase": "main",
                            "status_code": 400,
                            "expected_status": 200,
                            "request_payload": {"ifname": "vlan100"},
                            "response_body": {"error": "missing description"},
                            "err_codes": [],
                            "passed": False,
                        },
                    ],
                    "openapi_request_schema": {
                        "required": ["ifname", "description"],
                    },
                    "heuristic": {
                        "classification": "TEST_SETUP",
                        "classification_ru": CLASSIFICATION_RU["TEST_SETUP"],
                        "severity": "serious",
                        "severity_ru": "Критические",
                        "short_reason_ru": "нет description",
                        "reason_ru": "Нет обязательного поля description",
                        "fix_ru": "1) Добавить description в генератор",
                        "confidence": "высокая",
                    },
                    "ollama": None,
                },
            ],
        }
        report = format_run_report(context)
        self.assertIn("**Прошло:** 5", report)
        self.assertIn("**Не прошло:** 1", report)
        self.assertIn("Упавшие тесты", report)
        self.assertIn("Критические", report)
        self.assertNotIn("ожидался http", report.lower())
        self.assertNotIn("[обрезано]", report)

    def test_generation_context_parses_warnings(self):
        log_path = self._create_temp_gen_log()
        context = build_generation_analysis_context(
            gen_log_path=log_path,
            endpoints=["/interfaces/foo"],
            elapsed_sec=2.0,
        )
        self.assertEqual(context["warning_count"], 1)
        self.assertEqual(context["error_count"], 1)

        orch = OllamaOrchestrator(enabled=False)
        report = orch.analyze_generation(context)
        self.assertIn("генерации", report.lower())

    def _create_temp_gen_log(self) -> str:
        fd, name = tempfile.mkstemp(suffix=".log")
        path = Path(name)
        path.write_text(
            "2026-01-01 | WARNING | MAIN | Missing coverage keys\n"
            "2026-01-01 | ERROR | MAIN | schema failed\n",
            encoding="utf-8",
        )
        os.close(fd)
        self.addCleanup(path.unlink, missing_ok=True)
        return str(path)


if __name__ == "__main__":
    unittest.main()

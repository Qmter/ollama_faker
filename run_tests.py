#!/usr/bin/env python3
"""Последовательный запуск сгенерированных REST API тестов из tests/*.json."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from main import (
    _replace_placeholders,
    discover_post_endpoints,
    load_env_file,
    resolve_target_endpoints,
)

logger = logging.getLogger("RUNNER")

_LOG_FORMAT = "%(message)s"
_SEPARATOR = "=" * 80
_SUB_SEPARATOR = "-" * 80


@dataclass
class StepResult:
    phase: str
    step_index: int
    endpoint: str
    method: str
    url: str
    request_payload: Any
    status_code: int | None
    expected_status: int | None
    response_body: Any
    elapsed_ms: float
    passed: bool
    note: str | None = None
    error: str | None = None


@dataclass
class ScenarioResult:
    endpoint: str
    test_id: int
    description: str
    coverage_keys: list[str]
    steps: list[StepResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(step.passed for step in self.steps)


@dataclass
class RunSummary:
    total_scenarios: int = 0
    passed_scenarios: int = 0
    failed_scenarios: int = 0
    total_steps: int = 0
    passed_steps: int = 0
    failed_steps: int = 0
    skipped_files: list[str] = field(default_factory=list)


def configure_logging(*, verbose: bool, log_file: str) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        filename=log_file,
        filemode="w",
        level=level,
        format=_LOG_FORMAT,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Запуск сгенерированных REST API тестов из tests/",
    )
    parser.add_argument(
        "-e",
        "--endpoint",
        nargs="+",
        metavar="PATH",
        help="Эндпоинт или список эндпоинтов (POST). Без -e — все с тестами из openapi.json",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Подробный лог: полные тела запросов и ответов (run.log)",
    )
    parser.add_argument(
        "--base-url",
        metavar="URL",
        help="Базовый URL API (иначе API_BASE_URL из .env / окружения)",
    )
    parser.add_argument(
        "--tests-dir",
        default="tests",
        metavar="DIR",
        help="Каталог с JSON-сценариями (по умолчанию: tests)",
    )
    parser.add_argument(
        "--log-file",
        default="run.log",
        metavar="FILE",
        help="Файл лога запуска (по умолчанию: run.log)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SEC",
        help="Таймаут HTTP-запроса в секундах (по умолчанию: 30)",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Остановиться после первого упавшего теста",
    )
    parser.add_argument(
        "--skip-teardown-on-failure",
        action="store_true",
        help="Не выполнять teardown, если setup или main_test упали",
    )
    return parser.parse_args(argv)


def _resolve_base_url(cli_value: str | None, env_file: dict) -> str:
    base_url = (
        cli_value
        or os.environ.get("API_BASE_URL")
        or env_file.get("API_BASE_URL")
    )
    if not base_url:
        raise SystemExit(
            "Не задан базовый URL API. Укажите --base-url или API_BASE_URL в .env"
        )
    return base_url.rstrip("/")


def _build_http_session(env_file: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
    })

    api_key = (
        os.environ.get("ISTOK_API_KEY")
        or os.environ.get("API_KEY")
        or env_file.get("ISTOK_API_KEY")
        or env_file.get("API_KEY")
    )
    bearer = (
        os.environ.get("API_BEARER_TOKEN")
        or env_file.get("API_BEARER_TOKEN")
    )
    user = os.environ.get("API_USER") or env_file.get("API_USER")
    password = os.environ.get("API_PASSWORD") or env_file.get("API_PASSWORD", "")

    if api_key:
        session.headers["istok-api-key"] = api_key
    elif bearer:
        session.headers["Authorization"] = f"Bearer {bearer}"
    elif user:
        session.auth = (user, password)

    session.verify = False
    return session


def endpoint_to_test_file(endpoint: str, tests_dir: Path, method: str = "post") -> Path:
    safe_name = endpoint.strip("/").replace("/", "_") + f"_{method}.json"
    return tests_dir / safe_name


def _format_json(data: Any) -> str:
    if data is None:
        return "(empty)"
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, indent=2, ensure_ascii=False)
    except TypeError:
        return repr(data)


def _extract_response_value(body: Any, path: str) -> Any:
    current = body
    for part in path.split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _parse_response(response: requests.Response) -> Any:
    text = response.text
    if not text:
        return None
    try:
        return response.json()
    except ValueError:
        return text


def _log_step(verbose: bool, step: StepResult) -> None:
    status_label = "PASS" if step.passed else "FAIL"
    phase_title = step.phase.upper()

    if verbose:
        logger.info(_SEPARATOR)
        logger.info(f"{phase_title}  {step.method} {step.endpoint}")
        if step.note:
            logger.info(f"Note: {step.note}")
        logger.info(_SUB_SEPARATOR)
        logger.info(f"URL: {step.url}")
        logger.info("Request body:")
        logger.info(_format_json(step.request_payload))
        logger.info(_SUB_SEPARATOR)
        if step.status_code is not None:
            if step.expected_status is None:
                status_info = (
                    f"HTTP {step.status_code} "
                    f"(без проверки статуса, {step.elapsed_ms:.0f} ms)"
                )
            else:
                status_info = (
                    f"HTTP {step.status_code} "
                    f"(expected {step.expected_status}, {step.elapsed_ms:.0f} ms)"
                )
            logger.info(f"Response: {status_info}")
            logger.info("Response body:")
            logger.info(_format_json(step.response_body))
        if step.error:
            logger.info(f"Error: {step.error}")
        logger.info(_SUB_SEPARATOR)
        logger.info(f"Result: {status_label}")
        return

    request_preview = json.dumps(step.request_payload, ensure_ascii=False)
    if len(request_preview) > 120:
        request_preview = request_preview[:117] + "..."
    response_preview = _format_json(step.response_body)
    if len(response_preview) > 120:
        response_preview = response_preview[:117] + "..."

    logger.info(
        f"{phase_title:8} {step.method:4} {step.endpoint} "
        f"→ {step.status_code} ({step.elapsed_ms:.0f} ms) [{status_label}]"
    )
    logger.info(f"  request : {request_preview}")
    if step.status_code is not None:
        logger.info(f"  response: {response_preview}")
    if step.error:
        logger.info(f"  error   : {step.error}")


def _log_scenario_header(
    verbose: bool,
    scenario: dict,
    endpoint: str,
    index: int,
    total: int,
) -> None:
    test_id = scenario.get("test_id", "?")
    description = scenario.get("description", "")
    coverage = ", ".join(scenario.get("coverage_keys", [])) or "—"

    if verbose:
        logger.info("")
        logger.info(_SEPARATOR)
        logger.info(
            f"TEST {index}/{total}  #{test_id}  {endpoint}"
        )
        logger.info(f"Description : {description}")
        logger.info(f"Coverage    : {coverage}")
        logger.info(_SEPARATOR)
        return

    logger.info("")
    logger.info(
        f"TEST {index}/{total}  #{test_id}  {endpoint}  |  {description}"
    )
    logger.info(f"  coverage: {coverage}")


def _log_scenario_footer(verbose: bool, result: ScenarioResult) -> None:
    label = "PASS" if result.passed else "FAIL"
    if verbose:
        logger.info(_SEPARATOR)
        logger.info(f"Scenario #{result.test_id}: {label}")
        return
    logger.info(f"  → scenario #{result.test_id}: {label}")


def _execute_step(
    *,
    session: requests.Session,
    base_url: str,
    step_def: dict,
    phase: str,
    step_index: int,
    variables: dict,
    timeout: float,
) -> StepResult:
    endpoint = step_def["endpoint"]
    method = step_def.get("method", "POST").upper()
    expected_status = step_def.get("expected_status")
    payload = _replace_placeholders(
        copy.deepcopy(step_def.get("payload", {})),
        variables,
    )
    url = f"{base_url}{endpoint}"
    note = step_def.get("note")

    started = time.perf_counter()
    status_code: int | None = None
    response_body: Any = None
    error: str | None = None

    try:
        response = session.request(
            method=method,
            url=url,
            json=payload,
            timeout=timeout,
        )
        status_code = response.status_code
        response_body = _parse_response(response)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if expected_status is None:
            passed = True
        else:
            passed = status_code == expected_status

        if phase == "setup" and (
            expected_status is None or status_code == expected_status
        ):
            extract_var = step_def.get("extract_to_variable")
            extract_path = step_def.get("response_extract")
            if extract_var and extract_path:
                value = _extract_response_value(response_body, extract_path)
                if value is not None:
                    variables[extract_var] = value

        if not passed:
            error = (
                f"Ожидался HTTP {expected_status}, получен HTTP {status_code}"
            )

        return StepResult(
            phase=phase,
            step_index=step_index,
            endpoint=endpoint,
            method=method,
            url=url,
            request_payload=payload,
            status_code=status_code,
            expected_status=expected_status,
            response_body=response_body,
            elapsed_ms=elapsed_ms,
            passed=passed,
            note=note,
            error=error,
        )
    except requests.RequestException as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return StepResult(
            phase=phase,
            step_index=step_index,
            endpoint=endpoint,
            method=method,
            url=url,
            request_payload=payload,
            status_code=status_code,
            expected_status=expected_status,
            response_body=response_body,
            elapsed_ms=elapsed_ms,
            passed=False,
            note=note,
            error=str(exc),
        )


def _run_scenario(
    *,
    scenario: dict,
    session: requests.Session,
    base_url: str,
    endpoint: str,
    verbose: bool,
    timeout: float,
    skip_teardown_on_failure: bool,
    scenario_index: int,
    scenario_total: int,
) -> ScenarioResult:
    result = ScenarioResult(
        endpoint=endpoint,
        test_id=scenario.get("test_id", 0),
        description=scenario.get("description", ""),
        coverage_keys=list(scenario.get("coverage_keys", [])),
    )
    variables: dict = {}

    _log_scenario_header(verbose, scenario, endpoint, scenario_index, scenario_total)

    phases: list[tuple[str, list[dict]]] = [
        ("setup", list(scenario.get("setup", []))),
        ("main", [scenario["main_test"]]),
        ("teardown", list(scenario.get("teardown", []))),
    ]

    setup_failed = False
    main_failed = False

    for phase, steps in phases:
        if setup_failed and phase == "main":
            logger.info("MAIN skipped (setup failed)")
            continue
        if (
            main_failed
            and phase == "teardown"
            and skip_teardown_on_failure
        ):
            logger.info("TEARDOWN skipped (--skip-teardown-on-failure)")
            continue

        phase_failed = False
        for step_index, step_def in enumerate(steps, 1):
            step_result = _execute_step(
                session=session,
                base_url=base_url,
                step_def=step_def,
                phase=phase,
                step_index=step_index,
                variables=variables,
                timeout=timeout,
            )
            result.steps.append(step_result)
            _log_step(verbose, step_result)

            if not step_result.passed:
                phase_failed = True
                break

        if phase_failed:
            if phase == "setup":
                setup_failed = True
            elif phase == "main":
                main_failed = True

    _log_scenario_footer(verbose, result)
    return result


def _write_run_summary(
    log_file: str,
    summary: RunSummary,
    elapsed_sec: float,
    endpoints_count: int,
) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    message = (
        f"Запуск завершён. Общее время: {elapsed_sec:.2f} сек. "
        f"(эндпоинтов: {endpoints_count}, "
        f"сценариев: {summary.total_scenarios}, "
        f"PASS: {summary.passed_scenarios}, FAIL: {summary.failed_scenarios}, "
        f"шагов PASS/FAIL: {summary.passed_steps}/{summary.failed_steps})"
    )
    line = f"{timestamp} | INFO | RUNNER | {message}\n"
    with open(log_file, "a", encoding="utf-8") as log:
        log.write(line)


def _load_scenarios(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list):
        raise ValueError(f"Ожидался JSON-массив сценариев: {path}")
    return data


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(verbose=args.verbose, log_file=args.log_file)

    started_at = time.time()
    env_file = load_env_file()
    base_url = _resolve_base_url(args.base_url, env_file)
    tests_dir = Path(args.tests_dir)
    session = _build_http_session(env_file)
    summary = RunSummary()

    logger.info("Запуск тестов REST API")
    logger.info(f"Base URL : {base_url}")
    logger.info(f"Tests dir: {tests_dir.resolve()}")
    logger.info(f"Log file : {Path(args.log_file).resolve()}")
    logger.info(f"Verbose  : {args.verbose}")
    logger.info(_SEPARATOR)

    with open("openapi.json", encoding="utf-8") as file:
        openapi_data = json.load(file)

    post_endpoints = discover_post_endpoints(openapi_data)
    endpoints = resolve_target_endpoints(args.endpoint, post_endpoints)

    if args.endpoint:
        logger.info(f"Выбрано эндпоинтов: {len(endpoints)}")
    else:
        logger.info(f"Все POST-эндпоинты из openapi.json: {len(endpoints)}")

    exit_code = 0
    global_scenario_index = 0
    global_scenario_total = 0

    endpoint_files: list[tuple[str, Path, list[dict]]] = []
    for endpoint in endpoints:
        test_file = endpoint_to_test_file(endpoint, tests_dir)
        if not test_file.is_file():
            summary.skipped_files.append(str(test_file))
            logger.warning(f"Файл тестов не найден, пропуск: {test_file}")
            continue
        scenarios = _load_scenarios(test_file)
        endpoint_files.append((endpoint, test_file, scenarios))
        global_scenario_total += len(scenarios)

    if not endpoint_files:
        logger.error("Нет тестов для запуска")
        _write_run_summary(args.log_file, summary, time.time() - started_at, len(endpoints))
        return 1

    for endpoint, test_file, scenarios in endpoint_files:
        logger.info("")
        logger.info(_SEPARATOR)
        logger.info(f"ENDPOINT {endpoint}  ({test_file.name}, {len(scenarios)} тестов)")
        logger.info(_SEPARATOR)

        for scenario in scenarios:
            global_scenario_index += 1
            print(
                f"[{global_scenario_index}/{global_scenario_total}] "
                f"{endpoint} #{scenario.get('test_id', '?')}",
                flush=True,
            )

            scenario_result = _run_scenario(
                scenario=scenario,
                session=session,
                base_url=base_url,
                endpoint=endpoint,
                verbose=args.verbose,
                timeout=args.timeout,
                skip_teardown_on_failure=args.skip_teardown_on_failure,
                scenario_index=global_scenario_index,
                scenario_total=global_scenario_total,
            )

            summary.total_scenarios += 1
            summary.total_steps += len(scenario_result.steps)
            summary.passed_steps += sum(1 for s in scenario_result.steps if s.passed)
            summary.failed_steps += sum(1 for s in scenario_result.steps if not s.passed)

            if scenario_result.passed:
                summary.passed_scenarios += 1
                print(f"  PASS", flush=True)
            else:
                summary.failed_scenarios += 1
                exit_code = 1
                print(f"  FAIL", flush=True)
                if args.stop_on_failure:
                    logger.error("Остановка по --stop-on-failure")
                    _write_run_summary(
                        args.log_file, summary, time.time() - started_at, len(endpoints),
                    )
                    return exit_code

    elapsed = time.time() - started_at
    _write_run_summary(args.log_file, summary, elapsed, len(endpoints))

    print("")
    print(
        f"Готово за {elapsed:.2f} с | "
        f"сценариев PASS/FAIL: {summary.passed_scenarios}/{summary.failed_scenarios} | "
        f"лог: {args.log_file}"
    )
    if summary.skipped_files:
        print(f"Пропущено файлов: {len(summary.skipped_files)}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

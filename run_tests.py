#!/usr/bin/env python3
"""Последовательный запуск сгенерированных REST API тестов из tests/**/*.json."""

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
    _normalize_prefix,
    _replace_placeholders,
    discover_post_endpoints,
    load_env_file,
    resolve_run_endpoints,
    resolve_target_endpoints,
)
from test_paths import endpoint_to_test_file
from log_paths import build_ollama_report_path, resolve_cli_log_file
from ollama_orchestrator import (
    OllamaOrchestrator,
    build_run_analysis_context,
)

logger = logging.getLogger("RUNNER")

_LOG_FORMAT = "%(message)s"
_SEPARATOR = "=" * 80
_SUB_SEPARATOR = "-" * 80
_ENDPOINT_BORDER = "#" * 80


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
    attempt: int = 1
    max_attempts: int = 1


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


@dataclass
class EndpointRunResult:
    endpoint: str
    status: str
    failed_test_ids: list[int] = field(default_factory=list)


@dataclass
class FailedScenarioRecord:
    result: ScenarioResult
    scenario: dict[str, Any]


def configure_logging(*, verbose: bool, log_file: str | Path) -> None:
    """
    Лог прогона тестов в файл (обычно logs/run_<datetime>_<scope>.log).

    verbose / -v → DEBUG (полные тела HTTP);
    иначе INFO. filemode="w" — новый файл на каждый запуск.
    urllib3/requests глушим, чтобы не засорять лог служебным шумом.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        filename=str(log_file),
        filemode="w",
        level=level,
        format=_LOG_FORMAT,
        force=True,
        encoding="utf-8",
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
        help="Эндпоинт или список эндпоинтов (POST). Без -e/-d — все с тестами из openapi.json",
    )
    parser.add_argument(
        "-d",
        "--dir",
        nargs="+",
        metavar="PREFIX",
        help="Префикс пути: все POST-эндпоинты, начинающиеся с PREFIX (напр. -d /interfaces)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Подробный лог: полные тела запросов и ответов (logs/run_*.log)",
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
        default=None,
        metavar="FILE",
        help="Файл лога (по умолчанию: logs/run_<datetime>_<scope>.log)",
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
    parser.add_argument(
        "--max-teardown-retry",
        type=int,
        default=3,
        metavar="N",
        help="Повторов teardown при ошибке на тот же запрос (по умолчанию: 3)",
    )
    parser.add_argument(
        "--recover-already-exists",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="При «already exists»: teardown → повтор setup/main (по умолчанию: включено)",
    )
    parser.add_argument(
        "--ollama-log",
        action="store_true",
        help="После прогона: отчёт Ollama (генератор vs баг API) в logs/ollama_run_*.md",
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


def _log_endpoint_block_start(
    endpoint: str,
    test_file: Path,
    scenario_count: int,
    endpoint_index: int,
    endpoint_total: int,
) -> None:
    logger.info("")
    logger.info(_ENDPOINT_BORDER)
    logger.info(
        f"ENDPOINT [{endpoint_index}/{endpoint_total}]  {endpoint}"
    )
    logger.info(f"File: {test_file.as_posix()}  |  тестов: {scenario_count}")
    logger.info(_ENDPOINT_BORDER)
    logger.info(_SEPARATOR)


def _log_endpoint_block_end(
    endpoint: str,
    *,
    passed: int,
    failed: int,
    elapsed_sec: float,
) -> None:
    logger.info(_SEPARATOR)
    logger.info(
        f"ENDPOINT {endpoint} — готово: "
        f"PASS {passed}, FAIL {failed}, {elapsed_sec:.2f} сек."
    )
    logger.info(_ENDPOINT_BORDER)


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
    attempt_suffix = ""
    if step.max_attempts > 1:
        attempt_suffix = f" (attempt {step.attempt}/{step.max_attempts})"

    if verbose:
        phase_header = f"[ {phase_title}{attempt_suffix} ] {step.method} {step.endpoint}"
        header_line = f"--- {phase_header} ".ljust(80, "-")
        logger.info("")
        logger.info(header_line)
        if step.note:
            logger.info(f"Note: {step.note}")
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
        logger.info(f"Step Result: {status_label}")
        return

    logger.info(
        f"  {phase_title:8} {step.method:4} {step.endpoint}{attempt_suffix} "
        f"→ {step.status_code} ({step.elapsed_ms:.0f} ms) [{status_label}]"
    )
    if step.error:
        logger.info(f"    error   : {step.error}")


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
            f">>> START TEST [{index}/{total}]  #{test_id}  {endpoint}"
        )
        logger.info(f"    Description : {description}")
        logger.info(f"    Coverage    : {coverage}")
        logger.info(_SEPARATOR)
        return

    logger.info("")
    logger.info(
        f">>> TEST [{index}/{total}]  #{test_id}  {endpoint}  |  {description}"
    )


def _log_scenario_footer(verbose: bool, result: ScenarioResult) -> None:
    label = "PASS" if result.passed else "FAIL"
    if verbose:
        logger.info("")
        logger.info(_SEPARATOR)
        logger.info(f"<<< END TEST #{result.test_id}  |  RESULT: {label}")
        logger.info(_SEPARATOR)
        logger.info("")
        return
    logger.info(f"  → scenario #{result.test_id}: {label}")
    logger.info("_" * 80)


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


def _teardown_succeeded(step: StepResult) -> bool:
    """Успешный teardown: HTTP 2xx или явное совпадение expected_status."""
    if step.status_code is None:
        return False
    if step.expected_status is not None:
        return step.passed
    return step.status_code < 400


def _execute_teardown_step(
    *,
    session: requests.Session,
    base_url: str,
    step_def: dict,
    step_index: int,
    variables: dict,
    timeout: float,
    verbose: bool,
    max_retries: int,
) -> StepResult:
    max_attempts = max_retries + 1
    last_result: StepResult | None = None

    for attempt in range(1, max_attempts + 1):
        result = _execute_step(
            session=session,
            base_url=base_url,
            step_def=step_def,
            phase="teardown",
            step_index=step_index,
            variables=variables,
            timeout=timeout,
        )
        result.attempt = attempt
        result.max_attempts = max_attempts
        last_result = result
        _log_step(verbose, result)

        if _teardown_succeeded(result):
            return result
        if attempt < max_attempts:
            logger.info(
                f"TEARDOWN retry {attempt}/{max_retries}: "
                f"{result.method} {result.endpoint}"
            )

    assert last_result is not None
    return last_result


def _response_text(step: StepResult) -> str:
    body = step.response_body
    if body is None:
        return ""
    if isinstance(body, str):
        return body.lower()
    try:
        return json.dumps(body, ensure_ascii=False).lower()
    except TypeError:
        return str(body).lower()


def _is_already_exists_error(step: StepResult) -> bool:
    text = _response_text(step)
    return "already exists" in text or "already exist" in text


def _execute_phase_step(
    *,
    phase: str,
    step_def: dict,
    step_index: int,
    session: requests.Session,
    base_url: str,
    variables: dict,
    timeout: float,
    verbose: bool,
    max_teardown_retry: int,
) -> StepResult:
    if phase == "teardown" and max_teardown_retry > 0:
        return _execute_teardown_step(
            session=session,
            base_url=base_url,
            step_def=step_def,
            step_index=step_index,
            variables=variables,
            timeout=timeout,
            verbose=verbose,
            max_retries=max_teardown_retry,
        )
    step_result = _execute_step(
        session=session,
        base_url=base_url,
        step_def=step_def,
        phase=phase,
        step_index=step_index,
        variables=variables,
        timeout=timeout,
    )
    _log_step(verbose, step_result)
    return step_result


def _run_phase_steps(
    *,
    phase: str,
    steps: list[dict],
    session: requests.Session,
    base_url: str,
    variables: dict,
    timeout: float,
    verbose: bool,
    max_teardown_retry: int,
    result: ScenarioResult,
) -> bool:
    for step_index, step_def in enumerate(steps, 1):
        step_result = _execute_phase_step(
            phase=phase,
            step_def=step_def,
            step_index=step_index,
            session=session,
            base_url=base_url,
            variables=variables,
            timeout=timeout,
            verbose=verbose,
            max_teardown_retry=max_teardown_retry,
        )
        result.steps.append(step_result)
        if not step_result.passed:
            return False
    return True


def _attempt_already_exists_recovery(
    *,
    scenario: dict,
    retry_phase: str,
    session: requests.Session,
    base_url: str,
    variables: dict,
    timeout: float,
    verbose: bool,
    max_teardown_retry: int,
    result: ScenarioResult,
) -> bool:
    teardown_steps = list(scenario.get("teardown", []))
    if not teardown_steps:
        logger.info("RECOVERY skipped: в сценарии нет teardown")
        return False

    logger.info("")
    logger.info(_SEPARATOR)
    logger.info("RECOVERY: already exists → teardown → повтор")
    logger.info(_SEPARATOR)

    _run_phase_steps(
        phase="teardown",
        steps=teardown_steps,
        session=session,
        base_url=base_url,
        variables=variables,
        timeout=timeout,
        verbose=verbose,
        max_teardown_retry=max_teardown_retry,
        result=result,
    )

    if retry_phase == "setup":
        retry_steps = list(scenario.get("setup", []))
    else:
        retry_steps = [scenario["main_test"]]

    if not retry_steps:
        return False

    return _run_phase_steps(
        phase=retry_phase,
        steps=retry_steps,
        session=session,
        base_url=base_url,
        variables=variables,
        timeout=timeout,
        verbose=verbose,
        max_teardown_retry=0,
        result=result,
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
    max_teardown_retry: int,
    recover_already_exists: bool,
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
    recovery_used = False

    _log_scenario_header(verbose, scenario, endpoint, scenario_index, scenario_total)

    setup_steps = list(scenario.get("setup", []))
    teardown_steps = list(scenario.get("teardown", []))
    main_steps = [scenario["main_test"]]

    setup_failed = False
    main_failed = False

    if setup_steps:
        setup_ok = _run_phase_steps(
            phase="setup",
            steps=setup_steps,
            session=session,
            base_url=base_url,
            variables=variables,
            timeout=timeout,
            verbose=verbose,
            max_teardown_retry=0,
            result=result,
        )
        if not setup_ok:
            last_step = result.steps[-1]
            if (
                recover_already_exists
                and not recovery_used
                and _is_already_exists_error(last_step)
                and teardown_steps
            ):
                recovery_used = True
                setup_ok = _attempt_already_exists_recovery(
                    scenario=scenario,
                    retry_phase="setup",
                    session=session,
                    base_url=base_url,
                    variables=variables,
                    timeout=timeout,
                    verbose=verbose,
                    max_teardown_retry=max_teardown_retry,
                    result=result,
                )
            if not setup_ok:
                setup_failed = True
                logger.info("MAIN skipped (setup failed)")

    if not setup_failed:
        main_ok = _run_phase_steps(
            phase="main",
            steps=main_steps,
            session=session,
            base_url=base_url,
            variables=variables,
            timeout=timeout,
            verbose=verbose,
            max_teardown_retry=0,
            result=result,
        )
        if not main_ok:
            last_step = result.steps[-1]
            if (
                recover_already_exists
                and not recovery_used
                and not setup_steps
                and _is_already_exists_error(last_step)
                and teardown_steps
            ):
                recovery_used = True
                main_ok = _attempt_already_exists_recovery(
                    scenario=scenario,
                    retry_phase="main",
                    session=session,
                    base_url=base_url,
                    variables=variables,
                    timeout=timeout,
                    verbose=verbose,
                    max_teardown_retry=max_teardown_retry,
                    result=result,
                )
            if not main_ok:
                main_failed = True

    if teardown_steps and not (main_failed and skip_teardown_on_failure):
        _run_phase_steps(
            phase="teardown",
            steps=teardown_steps,
            session=session,
            base_url=base_url,
            variables=variables,
            timeout=timeout,
            verbose=verbose,
            max_teardown_retry=max_teardown_retry,
            result=result,
        )
    elif main_failed and skip_teardown_on_failure:
        logger.info("TEARDOWN skipped (--skip-teardown-on-failure)")

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


def _log_endpoints_summary_table(endpoint_results: list[EndpointRunResult]) -> None:
    if not endpoint_results:
        return

    col_endpoint = max(len("endpoint"), *(len(r.endpoint) for r in endpoint_results))
    col_status = max(len("статус"), *(len(r.status) for r in endpoint_results))

    logger.info("")
    logger.info(_SEPARATOR)
    logger.info("ИТОГОВАЯ ТАБЛИЦА")
    logger.info(_SEPARATOR)
    logger.info(
        f"{'endpoint'.ljust(col_endpoint)} | "
        f"{'статус'.ljust(col_status)} | "
        "не прошли (test_id)"
    )
    logger.info("-" * (col_endpoint + col_status + 28))

    for result in endpoint_results:
        failed_ids = (
            ", ".join(str(test_id) for test_id in result.failed_test_ids)
            if result.failed_test_ids
            else "-"
        )
        logger.info(
            f"{result.endpoint.ljust(col_endpoint)} | "
            f"{result.status.ljust(col_status)} | "
            f"{failed_ids}"
        )

    logger.info(_SEPARATOR)


def _finalize_run(
    *,
    log_file: str,
    summary: RunSummary,
    endpoint_results: list[EndpointRunResult],
    elapsed_sec: float,
    endpoints_count: int,
) -> None:
    _log_endpoints_summary_table(endpoint_results)
    _write_run_summary(log_file, summary, elapsed_sec, endpoints_count)


def _write_ollama_run_report(
    *,
    args: argparse.Namespace,
    log_path: Path,
    summary: RunSummary,
    endpoint_results: list[EndpointRunResult],
    failed_scenarios: list[FailedScenarioRecord],
    elapsed_sec: float,
    endpoints_count: int,
) -> Path | None:
    ollama = OllamaOrchestrator.from_cli(args.ollama_log)
    report_path = build_ollama_report_path(
        "run",
        endpoints=args.endpoint,
        dir_prefixes=args.dir,
    )
    context = build_run_analysis_context(
        failures=failed_scenarios,
        summary=summary,
        endpoint_results=endpoint_results,
        run_log_path=log_path,
        elapsed_sec=elapsed_sec,
        endpoints_count=endpoints_count,
    )
    body = ollama.analyze_run(context)
    return ollama.write_report(report_path, body, context=context, kind="run")


def _complete_run(
    *,
    args: argparse.Namespace,
    log_path: Path,
    summary: RunSummary,
    endpoint_results: list[EndpointRunResult],
    failed_scenarios: list[FailedScenarioRecord],
    elapsed_sec: float,
    endpoints_count: int,
) -> Path | None:
    _finalize_run(
        log_file=str(log_path),
        summary=summary,
        endpoint_results=endpoint_results,
        elapsed_sec=elapsed_sec,
        endpoints_count=endpoints_count,
    )
    if not args.ollama_log:
        return None
    return _write_ollama_run_report(
        args=args,
        log_path=log_path,
        summary=summary,
        endpoint_results=endpoint_results,
        failed_scenarios=failed_scenarios,
        elapsed_sec=elapsed_sec,
        endpoints_count=endpoints_count,
    )


def _load_scenarios(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list):
        raise ValueError(f"Ожидался JSON-массив сценариев: {path}")
    return data


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_teardown_retry < 0:
        raise SystemExit("--max-teardown-retry должен быть >= 0")
    # Путь лога: --log-file, иначе logs/run_<datetime>_<scope>.log
    # scope берётся из -d / -e (как в gen/clear), чтобы логи запусков не смешивались
    log_path = resolve_cli_log_file(
        args.log_file,
        "run",
        endpoints=args.endpoint,
        dir_prefixes=args.dir,
    )
    configure_logging(verbose=args.verbose, log_file=log_path)

    started_at = time.time()
    env_file = load_env_file()
    base_url = _resolve_base_url(args.base_url, env_file)
    tests_dir = Path(args.tests_dir)
    session = _build_http_session(env_file)
    summary = RunSummary()
    endpoint_results: list[EndpointRunResult] = []
    failed_scenarios: list[FailedScenarioRecord] = []

    logger.info("Запуск тестов REST API")
    logger.info(f"Base URL : {base_url}")
    logger.info(f"Tests dir: {tests_dir.resolve()}")
    logger.info(f"Log file : {log_path.resolve()}")
    logger.info(f"Verbose  : {args.verbose}")
    logger.info(f"Teardown retries: {args.max_teardown_retry}")
    logger.info(f"Recover already exists: {args.recover_already_exists}")
    logger.info(f"Ollama log : {args.ollama_log}")
    logger.info(_SEPARATOR)

    with open("openapi.json", encoding="utf-8") as file:
        openapi_data = json.load(file)

    post_endpoints = discover_post_endpoints(openapi_data)
    endpoints = resolve_run_endpoints(
        requested=args.endpoint,
        dir_prefixes=args.dir,
        all_endpoints=post_endpoints,
    )

    if args.dir:
        logger.info(
            f"Префикс(ы): {', '.join(_normalize_prefix(p) for p in args.dir)} "
            f"→ {len(endpoints)} эндпоинтов"
        )
    elif args.endpoint:
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
            endpoint_results.append(
                EndpointRunResult(endpoint=endpoint, status="SKIPPED"),
            )
            logger.warning(f"Файл тестов не найден, пропуск: {test_file}")
            continue
        scenarios = _load_scenarios(test_file)
        endpoint_files.append((endpoint, test_file, scenarios))
        global_scenario_total += len(scenarios)

    if not endpoint_files:
        logger.error("Нет тестов для запуска")
        _complete_run(
            args=args,
            log_path=log_path,
            summary=summary,
            endpoint_results=endpoint_results,
            failed_scenarios=failed_scenarios,
            elapsed_sec=time.time() - started_at,
            endpoints_count=len(endpoints),
        )
        return 1

    for endpoint_index, (endpoint, test_file, scenarios) in enumerate(
        endpoint_files, 1,
    ):
        endpoint_started = time.time()
        endpoint_passed = 0
        endpoint_failed = 0
        endpoint_failed_test_ids: list[int] = []

        _log_endpoint_block_start(
            endpoint,
            test_file,
            len(scenarios),
            endpoint_index,
            len(endpoint_files),
        )

        for scenario in scenarios:
            global_scenario_index += 1

            scenario_result = _run_scenario(
                scenario=scenario,
                session=session,
                base_url=base_url,
                endpoint=endpoint,
                verbose=args.verbose,
                timeout=args.timeout,
                skip_teardown_on_failure=args.skip_teardown_on_failure,
                max_teardown_retry=args.max_teardown_retry,
                recover_already_exists=args.recover_already_exists,
                scenario_index=global_scenario_index,
                scenario_total=global_scenario_total,
            )

            summary.total_scenarios += 1
            summary.total_steps += len(scenario_result.steps)
            summary.passed_steps += sum(1 for s in scenario_result.steps if s.passed)
            summary.failed_steps += sum(1 for s in scenario_result.steps if not s.passed)

            status_label = "PASS" if scenario_result.passed else "FAIL"
            print(
                f"[{global_scenario_index}/{global_scenario_total}] "
                f"{endpoint} #{scenario.get('test_id', '?')}  {status_label}",
                flush=True,
            )

            if scenario_result.passed:
                summary.passed_scenarios += 1
                endpoint_passed += 1
            else:
                summary.failed_scenarios += 1
                endpoint_failed += 1
                endpoint_failed_test_ids.append(scenario_result.test_id)
                failed_scenarios.append(
                    FailedScenarioRecord(
                        result=scenario_result,
                        scenario=copy.deepcopy(scenario),
                    ),
                )
                exit_code = 1
                if args.stop_on_failure:
                    logger.error("Остановка по --stop-on-failure")
                    _log_endpoint_block_end(
                        endpoint,
                        passed=endpoint_passed,
                        failed=endpoint_failed,
                        elapsed_sec=time.time() - endpoint_started,
                    )
                    endpoint_results.append(
                        EndpointRunResult(
                            endpoint=endpoint,
                            status="FAIL" if endpoint_failed else "PASS",
                            failed_test_ids=endpoint_failed_test_ids,
                        ),
                    )
                    ollama_report = _complete_run(
                        args=args,
                        log_path=log_path,
                        summary=summary,
                        endpoint_results=endpoint_results,
                        failed_scenarios=failed_scenarios,
                        elapsed_sec=time.time() - started_at,
                        endpoints_count=len(endpoints),
                    )
                    if ollama_report:
                        print(f"Ollama-отчёт: {ollama_report.as_posix()}")
                    return exit_code

        _log_endpoint_block_end(
            endpoint,
            passed=endpoint_passed,
            failed=endpoint_failed,
            elapsed_sec=time.time() - endpoint_started,
        )
        endpoint_results.append(
            EndpointRunResult(
                endpoint=endpoint,
                status="FAIL" if endpoint_failed else "PASS",
                failed_test_ids=endpoint_failed_test_ids,
            ),
        )

    elapsed = time.time() - started_at
    ollama_report = _complete_run(
        args=args,
        log_path=log_path,
        summary=summary,
        endpoint_results=endpoint_results,
        failed_scenarios=failed_scenarios,
        elapsed_sec=elapsed,
        endpoints_count=len(endpoints),
    )

    print("")
    print(
        f"Готово за {elapsed:.2f} с | "
        f"сценариев PASS/FAIL: {summary.passed_scenarios}/{summary.failed_scenarios} | "
        f"лог: {log_path.as_posix()}"
    )
    if ollama_report:
        print(f"Ollama-отчёт: {ollama_report.as_posix()}")
    if summary.skipped_files:
        print(f"Пропущено файлов: {len(summary.skipped_files)}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
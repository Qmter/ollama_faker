#!/usr/bin/env python3
"""Предочистка маршрутизатора: выполняет teardown из tests/*.json."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from main import (
    discover_post_endpoints,
    load_env_file,
    resolve_run_endpoints,
)
from run_tests import (
    StepResult,
    _build_http_session,
    _execute_teardown_step,
    _load_scenarios,
    _log_step,
    _resolve_base_url,
    _response_text,
    _teardown_succeeded,
    configure_logging,
    endpoint_to_test_file,
)

logger = logging.getLogger("CLEAR")

_SEPARATOR = "=" * 80


def teardown_step_fingerprint(step: dict) -> str:
    return json.dumps(
        {
            "endpoint": step["endpoint"],
            "method": step.get("method", "POST").upper(),
            "payload": step.get("payload", {}),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def infer_teardown_priority(step: dict) -> int:
    """Приоритет как в main.py: меньше → раньше."""
    endpoint = step.get("endpoint", "").rstrip("/")
    payload = step.get("payload", {})

    if endpoint.endswith("/tunnel/delete"):
        return 10
    if endpoint.endswith("/vlan/delete") or endpoint.endswith("/eth_vlan/delete"):
        return 11
    if endpoint.endswith(("/bonding/delete", "/loopback/delete", "/bridge/delete")):
        return 10
    if endpoint == "/vrf" and payload.get("action") == "delete":
        return 100
    if endpoint.startswith("/dhcp/"):
        return 100
    return 50


def collect_unique_teardown_steps(scenarios: list[dict]) -> list[dict]:
    seen: set[str] = set()
    steps: list[dict] = []
    for scenario in scenarios:
        for step in scenario.get("teardown", []):
            fingerprint = teardown_step_fingerprint(step)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            steps.append(step)
    return steps


def sort_teardown_steps(steps: list[dict]) -> list[dict]:
    indexed = list(enumerate(steps))
    indexed.sort(
        key=lambda item: (
            infer_teardown_priority(item[1]),
            item[1].get("endpoint", ""),
            teardown_step_fingerprint(item[1]),
            item[0],
        ),
    )
    return [step for _, step in indexed]


def cleanup_step_succeeded(step: StepResult) -> bool:
    if _teardown_succeeded(step):
        return True
    if step.status_code == 404:
        return True
    text = _response_text(step)
    return "not found" in text or "is not found" in text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Предочистка: выполнить teardown из tests/*.json перед run_tests.py",
    )
    parser.add_argument(
        "-e",
        "--endpoint",
        nargs="+",
        metavar="PATH",
        help="Эндпоинт или список эндпоинтов (POST). Без -e/-d — все с тестами",
    )
    parser.add_argument(
        "-d",
        "--dir",
        nargs="+",
        metavar="PREFIX",
        help="Префикс пути (напр. -d /interfaces)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Подробный лог в clear.log",
    )
    parser.add_argument(
        "--base-url",
        metavar="URL",
        help="Базовый URL API (иначе API_BASE_URL из .env)",
    )
    parser.add_argument(
        "--tests-dir",
        default="tests",
        metavar="DIR",
        help="Каталог с JSON-сценариями (по умолчанию: tests)",
    )
    parser.add_argument(
        "--log-file",
        default="clear.log",
        metavar="FILE",
        help="Файл лога (по умолчанию: clear.log)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SEC",
        help="Таймаут HTTP-запроса в секундах",
    )
    parser.add_argument(
        "--max-teardown-retry",
        type=int,
        default=3,
        metavar="N",
        help="Повторов при ошибке на один teardown (по умолчанию: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только показать шаги, без HTTP-запросов",
    )
    return parser.parse_args(argv)


def _load_all_scenarios(
    endpoints: list[str],
    tests_dir: Path,
) -> tuple[list[dict], list[str]]:
    all_scenarios: list[dict] = []
    missing_files: list[str] = []
    for endpoint in endpoints:
        test_file = endpoint_to_test_file(endpoint, tests_dir)
        if not test_file.is_file():
            missing_files.append(str(test_file))
            logger.warning(f"Файл тестов не найден, пропуск: {test_file}")
            continue
        all_scenarios.extend(_load_scenarios(test_file))
    return all_scenarios, missing_files


def _print_step_line(index: int, total: int, step: dict, *, ok: bool) -> None:
    method = step.get("method", "POST").upper()
    endpoint = step["endpoint"]
    label = "OK" if ok else "FAIL"
    print(
        f"[{index}/{total}] TEARDOWN {method} {endpoint}  {label}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = parse_args(argv)
    if args.max_teardown_retry < 0:
        raise SystemExit("--max-teardown-retry должен быть >= 0")

    configure_logging(verbose=args.verbose, log_file=args.log_file)
    started_at = time.time()
    env_file = load_env_file()
    base_url = _resolve_base_url(args.base_url, env_file)
    tests_dir = Path(args.tests_dir)

    logger.info("Предочистка маршрутизатора (teardown из тестов)")
    logger.info(f"Base URL : {base_url}")
    logger.info(f"Tests dir: {tests_dir.resolve()}")
    logger.info(f"Log file : {Path(args.log_file).resolve()}")
    logger.info(f"Dry run  : {args.dry_run}")
    logger.info(_SEPARATOR)

    with open("openapi.json", encoding="utf-8") as file:
        openapi_data = json.load(file)

    post_endpoints = discover_post_endpoints(openapi_data)
    endpoints = resolve_run_endpoints(
        requested=args.endpoint,
        dir_prefixes=args.dir,
        all_endpoints=post_endpoints,
    )

    scenarios, missing_files = _load_all_scenarios(endpoints, tests_dir)
    if not scenarios:
        logger.error("Нет сценариев для очистки")
        if missing_files:
            for path in missing_files:
                logger.error(f"  • {path}")
        return 1

    raw_steps = collect_unique_teardown_steps(scenarios)
    steps = sort_teardown_steps(raw_steps)

    logger.info(
        f"Сценариев: {len(scenarios)}, уникальных teardown: {len(steps)} "
        f"(из {sum(len(s.get('teardown', [])) for s in scenarios)} всего)"
    )

    if args.dry_run:
        for index, step in enumerate(steps, 1):
            method = step.get("method", "POST").upper()
            endpoint = step["endpoint"]
            payload = json.dumps(step.get("payload", {}), ensure_ascii=False)
            line = f"[{index}/{len(steps)}] {method} {endpoint}  {payload}"
            logger.info(line)
            print(line, flush=True)
        elapsed = time.time() - started_at
        print(
            f"\nDry-run: {len(steps)} шагов, лог: {args.log_file} "
            f"({elapsed:.2f} сек.)",
            flush=True,
        )
        return 0

    session = _build_http_session(env_file)
    variables: dict[str, Any] = {}
    failed = 0

    for index, step_def in enumerate(steps, 1):
        result = _execute_teardown_step(
            session=session,
            base_url=base_url,
            step_def=step_def,
            step_index=index,
            variables=variables,
            timeout=args.timeout,
            verbose=args.verbose,
            max_retries=args.max_teardown_retry,
        )
        ok = cleanup_step_succeeded(result)
        _print_step_line(index, len(steps), step_def, ok=ok)
        if not ok:
            failed += 1
            if not args.verbose:
                _log_step(True, result)

    elapsed = time.time() - started_at
    logger.info(_SEPARATOR)
    logger.info(
        f"Готово за {elapsed:.2f} сек. | шагов OK/FAIL: {len(steps) - failed}/{failed}"
    )

    print(
        f"\nГотово за {elapsed:.2f} сек. | teardown OK/FAIL: "
        f"{len(steps) - failed}/{failed} | лог: {args.log_file}",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

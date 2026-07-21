#!/usr/bin/env python3
"""Предочистка маршрутизатора: list → delete по правилам из cleanup.json."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import requests

from main import _replace_placeholders, load_env_file
from run_tests import (
    StepResult,
    _build_http_session,
    _execute_teardown_step,
    _log_step,
    _resolve_base_url,
    _response_text,
    _teardown_succeeded,
    configure_logging,
)
from log_paths import resolve_cli_log_file

logger = logging.getLogger("CLEAR")

_SEPARATOR = "=" * 80
_DEFAULT_CONFIG = Path("cleanup.json")


def load_cleanup_config(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"Файл конфигурации не найден: {path}")
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: ожидался объект JSON")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise SystemExit(f"{path}: нужен непустой массив rules")
    return data


def parse_cleanup_rules(
    config: dict,
    *,
    only_names: list[str] | None = None,
) -> list[dict]:
    """Нормализует rules; optional filter by name. defaults.skip мержится в каждое правило."""
    wanted = {name.strip() for name in (only_names or []) if name.strip()}
    defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
    default_skip = [str(x) for x in (defaults.get("skip") or [])]
    default_skip_prefix = [str(x) for x in (defaults.get("skip_prefix") or [])]
    parsed: list[dict] = []
    for index, raw in enumerate(config.get("rules") or []):
        if not isinstance(raw, dict):
            logger.warning("cleanup.rules[%d]: пропуск (не объект)", index)
            continue
        name = raw.get("name") or f"rule_{index}"
        if wanted and name not in wanted:
            continue
        list_cfg = raw.get("list")
        delete_cfg = raw.get("delete")
        if not isinstance(list_cfg, dict) or not isinstance(delete_cfg, dict):
            logger.warning("cleanup.rules[%s]: нужны list и delete", name)
            continue
        if not list_cfg.get("endpoint") or not delete_cfg.get("endpoint"):
            logger.warning("cleanup.rules[%s]: list/delete.endpoint обязательны", name)
            continue
        skip = default_skip + [str(x) for x in (raw.get("skip") or [])]
        skip_prefix = default_skip_prefix + [
            str(x) for x in (raw.get("skip_prefix") or [])
        ]
        # dedupe, preserve order
        skip = list(dict.fromkeys(skip))
        skip_prefix = list(dict.fromkeys(skip_prefix))
        parsed.append({
            "name": str(name),
            "priority": int(raw.get("priority", 50)),
            "list": list_cfg,
            "delete": delete_cfg,
            "skip": skip,
            "skip_prefix": skip_prefix,
        })
    parsed.sort(key=lambda rule: (rule["priority"], rule["name"]))
    return parsed


def _get_by_path(obj: Any, path: str) -> Any:
    """Достаёт значение по dotted-пути (result.ipv4)."""
    if not path or not path.strip():
        return obj
    current = obj
    for part in path.split("."):
        if not part:
            continue
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _item_matches_filter(item: Any, item_filter: dict | None) -> bool:
    if not item_filter:
        return True
    if not isinstance(item, dict):
        return False
    for key, expected in item_filter.items():
        if item.get(key) != expected:
            return False
    return True


def extract_list_items(
    response_body: Any,
    list_cfg: dict,
) -> list[Any]:
    """
    Достаёт элементы для удаления из ответа list.

    Простой массив скаляров:
      items_path: "result.ipv4" → ["acl1", "acl2"]

    Массив объектов + фильтр + поле-массив (interfaces/list):
      items_path: "result.interfaces"
      item_filter: {"category": "vlan"}
      item_values: "ifname" → ["vlan100", ...]
    """
    items_path = list_cfg.get("items_path") or "result"
    node = _get_by_path(response_body, str(items_path))
    if node is None:
        return []

    item_filter = list_cfg.get("item_filter")
    item_values = list_cfg.get("item_values")

    if not isinstance(node, list):
        return []

    if item_filter or item_values:
        collected: list[Any] = []
        for entry in node:
            if not _item_matches_filter(entry, item_filter if isinstance(item_filter, dict) else None):
                continue
            if not item_values:
                collected.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            value = entry.get(item_values)
            if isinstance(value, list):
                collected.extend(value)
            elif value is not None:
                collected.append(value)
        return collected

    return list(node)


def should_skip_item(
    item: Any,
    *,
    skip: list[str],
    skip_prefix: list[str],
) -> bool:
    if isinstance(item, dict):
        return False
    text = str(item)
    if text in skip:
        return True
    return any(text.startswith(prefix) for prefix in skip_prefix if prefix)


def build_delete_steps_for_items(
    delete_cfg: dict,
    items: list[Any],
    *,
    skip: list[str] | None = None,
    skip_prefix: list[str] | None = None,
) -> list[dict]:
    """Собирает delete-шаги: payload с {{item}} / {{item.field}}."""
    skip = skip or []
    skip_prefix = skip_prefix or []
    endpoint = delete_cfg["endpoint"]
    method = str(delete_cfg.get("method", "POST")).upper()
    template = delete_cfg.get("payload", {})
    steps: list[dict] = []

    for item in items:
        if should_skip_item(item, skip=skip, skip_prefix=skip_prefix):
            logger.debug("skip item=%r", item)
            continue
        variables: dict[str, Any] = {"item": item}
        if isinstance(item, dict):
            variables.update(item)
        payload = _replace_placeholders(template, variables)
        steps.append({
            "endpoint": endpoint,
            "method": method,
            "payload": payload,
            "note": f"cleanup item={item!r}",
        })
    return steps


def cleanup_step_succeeded(step: StepResult) -> bool:
    if _teardown_succeeded(step):
        return True
    if step.status_code == 404:
        return True
    text = _response_text(step)
    return "not found" in text or "is not found" in text


def _fetch_list(
    *,
    session: requests.Session,
    base_url: str,
    list_cfg: dict,
    timeout: float,
) -> Any:
    endpoint = list_cfg["endpoint"]
    method = str(list_cfg.get("method", "GET")).upper()
    query = list_cfg.get("query") if isinstance(list_cfg.get("query"), dict) else None
    url = f"{base_url}{endpoint}"
    response = session.request(
        method=method,
        url=url,
        params=query,
        timeout=timeout,
    )
    try:
        body = response.json()
    except Exception:
        body = {"_raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(
            f"LIST {method} {endpoint} → HTTP {response.status_code}: {body!r}"
        )
    return body


def collect_cleanup_steps(
    *,
    session: requests.Session | None,
    base_url: str,
    rules: list[dict],
    timeout: float,
    dry_run: bool = False,
) -> list[dict]:
    """Для каждого rule: list → items → delete steps (по priority)."""
    all_steps: list[dict] = []
    for rule in rules:
        name = rule["name"]
        list_cfg = rule["list"]
        if dry_run and session is None:
            logger.info(
                "[dry-run] rule=%s LIST %s %s (items неизвестны без HTTP)",
                name,
                list_cfg.get("method", "GET"),
                list_cfg["endpoint"],
            )
            all_steps.append({
                "endpoint": rule["delete"]["endpoint"],
                "method": rule["delete"].get("method", "POST"),
                "payload": rule["delete"].get("payload", {}),
                "note": f"dry-run rule={name} (нужен HTTP list для item'ов)",
                "_dry_run_rule": name,
            })
            continue

        assert session is not None
        logger.info(
            "LIST %s %s  (rule=%s)",
            list_cfg.get("method", "GET"),
            list_cfg["endpoint"],
            name,
        )
        body = _fetch_list(
            session=session,
            base_url=base_url,
            list_cfg=list_cfg,
            timeout=timeout,
        )
        items = extract_list_items(body, list_cfg)
        logger.info("rule=%s: найдено %d item(s)", name, len(items))
        steps = build_delete_steps_for_items(
            rule["delete"],
            items,
            skip=rule.get("skip") or [],
            skip_prefix=rule.get("skip_prefix") or [],
        )
        for step in steps:
            step["note"] = f"rule={name}; {step.get('note', '')}".strip("; ")
        all_steps.extend(steps)
    return all_steps


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Предочистка: GET list → для каждого объекта POST delete "
            "(правила в cleanup.json)"
        ),
    )
    parser.add_argument(
        "--config",
        default=str(_DEFAULT_CONFIG),
        metavar="FILE",
        help=f"Файл правил (по умолчанию: {_DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "-r",
        "--rule",
        nargs="+",
        metavar="NAME",
        help="Выполнить только указанные rules (name из cleanup.json)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Подробный лог (logs/clear_*.log)",
    )
    parser.add_argument(
        "--base-url",
        metavar="URL",
        help="Базовый URL API (иначе API_BASE_URL из .env)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        metavar="FILE",
        help="Файл лога (по умолчанию: logs/clear_<datetime>.log)",
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
        help="Повторов при ошибке на один delete (по умолчанию: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать list/delete без выполнения delete (list всё равно вызывается)",
    )
    parser.add_argument(
        "--dry-run-config",
        action="store_true",
        help="Только показать правила из конфига, без HTTP",
    )
    return parser.parse_args(argv)


def _print_step_line(index: int, total: int, step: dict, *, ok: bool) -> None:
    method = step.get("method", "POST").upper()
    endpoint = step["endpoint"]
    label = "OK" if ok else "FAIL"
    print(
        f"[{index}/{total}] DELETE {method} {endpoint}  {label}",
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

    log_path = resolve_cli_log_file(
        args.log_file,
        "clear",
        endpoints=args.rule,
        dir_prefixes=None,
    )
    configure_logging(verbose=args.verbose, log_file=log_path)
    started_at = time.time()
    env_file = load_env_file()
    base_url = _resolve_base_url(args.base_url, env_file)
    config_path = Path(args.config)

    logger.info("Предочистка маршрутизатора (list → delete)")
    logger.info(f"Base URL : {base_url}")
    logger.info(f"Config   : {config_path.resolve()}")
    logger.info(f"Log file : {log_path.resolve()}")
    logger.info(f"Dry run  : {args.dry_run or args.dry_run_config}")
    logger.info(_SEPARATOR)

    config = load_cleanup_config(config_path)
    rules = parse_cleanup_rules(config, only_names=args.rule)
    if not rules:
        logger.error("Нет правил cleanup для выполнения")
        return 1

    logger.info("Правил: %d (%s)", len(rules), ", ".join(r["name"] for r in rules))

    if args.dry_run_config:
        for rule in rules:
            line = (
                f"rule={rule['name']} priority={rule['priority']} "
                f"LIST {rule['list'].get('method', 'GET')} {rule['list']['endpoint']} "
                f"→ DELETE {rule['delete'].get('method', 'POST')} {rule['delete']['endpoint']}"
            )
            logger.info(line)
            print(line, flush=True)
        return 0

    session = _build_http_session(env_file)
    try:
        steps = collect_cleanup_steps(
            session=session,
            base_url=base_url,
            rules=rules,
            timeout=args.timeout,
            dry_run=False,
        )
    except Exception as exc:
        logger.error("Ошибка LIST: %s", exc)
        print(f"Ошибка LIST: {exc}", flush=True)
        return 1

    logger.info("Delete-шагов: %d", len(steps))

    if args.dry_run:
        for index, step in enumerate(steps, 1):
            payload = json.dumps(step.get("payload", {}), ensure_ascii=False)
            line = (
                f"[{index}/{len(steps)}] "
                f"{step.get('method', 'POST')} {step['endpoint']}  {payload}"
            )
            logger.info(line)
            print(line, flush=True)
        elapsed = time.time() - started_at
        print(
            f"\nDry-run: {len(steps)} delete, лог: {log_path.as_posix()} "
            f"({elapsed:.2f} сек.)",
            flush=True,
        )
        return 0

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
        f"\nГотово за {elapsed:.2f} сек. | delete OK/FAIL: "
        f"{len(steps) - failed}/{failed} | лог: {log_path.as_posix()}",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

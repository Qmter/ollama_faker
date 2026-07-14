"""
Общие пути логов для main.py / run_tests.py / clear_for_tests.py.

Формат имени файла:
    logs/<prefix>_YYYYMMDD_HHMMSS_<scope>.log

Примеры:
    logs/gen_20260714_091500_all.log
    logs/run_20260714_091500_interfaces.log
    logs/clear_20260714_091500_dns_client.log

prefix:
    gen   — генерация тестов (main.py)
    run   — прогон тестов (run_tests.py)
    clear — предочистка (clear_for_tests.py)

scope (что запускали):
    all              — без -e и без -d (весь набор)
    interfaces       — флаг -d /interfaces
    dns_client       — флаг -e /dns/client
"""

from __future__ import annotations

import re
import time
from pathlib import Path

# Каталог, куда складываем все логи (создаётся автоматически при первом запуске).
LOG_DIR = Path("logs")


def sanitize_log_scope_token(value: str) -> str:
    """
    Превращает путь API в безопасный кусок имени файла.

    Зачем: в имени файла нельзя оставлять '/' и прочие спецсимволы.
    Как: убираем края, '/' → '_', остальное «не буквы/цифры» → '_'.

    Примеры:
        "/interfaces"              → "interfaces"
        "/dns/server/zone/master"  → "dns_server_zone_master"
        "///"                      → "root"  (пустой результат после очистки)
    """
    # Убираем пробелы по краям и ведущие/хвостовые слэши: "/interfaces/" → "interfaces"
    token = value.strip().strip("/")
    # Слэши пути заменяем на подчёркивание: "dns/client" → "dns_client"
    token = token.replace("/", "_").replace("\\", "_")
    # Всё, что не буква/цифра/точка/дефис/_ — тоже в '_', чтобы имя было «чистым»
    token = re.sub(r"[^\w.-]+", "_", token)
    # Лишние '_' по краям убираем; если строка пустая — ставим запасное имя
    return token.strip("_") or "root"


def resolve_log_scope(
    *,
    endpoints: list[str] | None = None,
    dir_prefixes: list[str] | None = None,
) -> str:
    """
    Собирает «хвост» имени лога (scope) из аргументов CLI.

    Приоритет:
      1) -d / префиксы каталогов  → например "interfaces"
      2) -e / список эндпоинтов   → один путь или первые 3 + _andNmore
      3) ничего не передали       → "all"

    Почему -d выше -e: при запуске «по директории» scope должен отражать
    префикс целиком, а не каждый эндпоинт отдельным токеном.
    """
    # Режим: python ... -d /interfaces  →  scope = "interfaces"
    if dir_prefixes:
        return "_".join(sanitize_log_scope_token(p) for p in dir_prefixes)

    # Режим: python ... -e /dns/client [/other ...]
    if endpoints:
        # Один эндпоинт — короткое и точное имя
        if len(endpoints) == 1:
            return sanitize_log_scope_token(endpoints[0])
        # Несколько: берём первые три, чтобы имя файла не раздувалось
        scope = "_".join(
            sanitize_log_scope_token(ep) for ep in endpoints[:3]
        )
        # Если эндпоинтов больше трёх — помечаем хвост: ..._and2more
        if len(endpoints) > 3:
            scope = f"{scope}_and{len(endpoints) - 3}more"
        return scope

    # Без -e и -d: полный прогон / полная генерация
    return "all"


def build_log_path(
    prefix: str,
    *,
    endpoints: list[str] | None = None,
    dir_prefixes: list[str] | None = None,
    timestamp: str | None = None,
) -> Path:
    """
    Собирает полный путь к файлу лога и создаёт каталог logs/ при необходимости.

    Аргументы:
        prefix      — "gen" | "run" | "clear" (какой скрипт пишет лог)
        endpoints   — список из -e (опционально)
        dir_prefixes — список из -d (опционально)
        timestamp   — своя метка времени (для тестов); иначе — сейчас

    Возвращает:
        Path, например logs/run_20260714_091500_interfaces.log
    """
    # Метка времени в имени: удобно сортировать и не перезаписывать старые логи
    ts = timestamp or time.strftime("%Y%m%d_%H%M%S")
    # Хвост имени: all / interfaces / dns_client / ...
    scope = resolve_log_scope(endpoints=endpoints, dir_prefixes=dir_prefixes)
    # parents=True — создаст и родительские каталоги; exist_ok — не ошибка, если уже есть
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{prefix}_{ts}_{scope}.log"


def resolve_cli_log_file(
    explicit: str | None,
    prefix: str,
    *,
    endpoints: list[str] | None = None,
    dir_prefixes: list[str] | None = None,
) -> Path:
    """
    Решает, какой файл лога использовать при старте CLI-скрипта.

    Логика:
        --log-file custom.log   → пишем ровно туда (ручной override)
        --log-file не указан    → авто-путь через build_log_path(...)

    Так run_tests / clear_for_tests оставляют удобный дефолт в logs/,
    но при отладке можно указать любой путь вручную.
    """
    # Пользователь явно задал файл — не трогаем, не подмешиваем datetime/scope
    if explicit:
        return Path(explicit)
    # Иначе — стандартная схема logs/<prefix>_<datetime>_<scope>.log
    return build_log_path(
        prefix,
        endpoints=endpoints,
        dir_prefixes=dir_prefixes,
    )

"""Ollama: анализ проваленных тестов по одному + отчёт на русском."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("OLLAMA")

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_CACHE_DIR = ".ollama_cache"
PROMPT_VERSION = "v5"

# Три категории по ТЗ пользователя
CLASSIFICATIONS = (
    "TEST_SETUP",   # ошибка теста/генератора: setup, неполный payload от coverage
    "ROUTER",       # ошибка маршрутизатора (не генератора)
    "BAD_DATA",     # в payload есть значения, но они неверны для технологии
)

CLASSIFICATION_RU = {
    "TEST_SETUP": "Ошибка теста/генератора",
    "ROUTER": "Ошибка маршрутизатора",
    "BAD_DATA": "Некорректные данные в payload",
}

# Серьёзность для финального раздела
SEVERITY_SERIOUS = "serious"
SEVERITY_MINOR = "minor"

SEVERITY_RU = {
    SEVERITY_SERIOUS: "Критические",
    SEVERITY_MINOR: "Некритические",
}

SYSTEM_PROMPT_RU = (
    "Ты senior QA/инженер по REST API сетевых маршрутизаторов. "
    "Отвечай ТОЛЬКО на русском. "
    "Анализируешь РОВНО ОДИН упавший тест. "
    "НЕ пересказывай HTTP-статус и текст ошибки устройства — они уже в логе. "
    "Сравни coverage_keys и payload: чего не хватает / что лишнее / что несовместимо. "
    "Дай корневую причину и пошаговое исправление "
    "(генератор, dependencies.json, mock_data, coverage, OpenAPI или баг API). "
    "TEST_SETUP — генератор/setup собрал тест неполно или неверно "
    "(в т.ч. coverage только mode без связанных полей). "
    "BAD_DATA — поля есть, но значения неверны для технологии. "
    "ROUTER — вина устройства. serious|minor."
)

_MAX_BODY_CHARS = 2500
_MAX_SCHEMA_CHARS = 3000
_OPENAPI_SCHEMA_CACHE: dict[str, dict | None] = {}

# Текст диагноза не должен быть копипастом раннера/JSON ответа
_NOISE_REASON_RE = re.compile(
    r"(ожидался\s+http|получен\s+http|errcode|\"result\"\s*:|\"request\"\s*:|"
    r"status mismatch|\[обрезано\])",
    re.IGNORECASE,
)


def _truncate_json(value: Any, max_chars: int = _MAX_BODY_CHARS) -> Any:
    if value is None:
        return None
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= max_chars:
        return value
    return f"{text[:max_chars]}… [обрезано, всего {len(text)} символов]"


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}…"


def _is_noisy_diagnosis(text: str) -> bool:
    if not text or not text.strip():
        return True
    if _NOISE_REASON_RE.search(text):
        return True
    # Длинный JSON-кусок вместо диагноза
    if text.count("{") >= 2 or '"errCode"' in text or '"errcode"' in text.lower():
        return True
    return False


def _clean_diagnosis(text: str, *, fallback: str, max_chars: int = 160) -> str:
    cleaned = " ".join(str(text or "").split())
    if _is_noisy_diagnosis(cleaned):
        cleaned = fallback
    return _truncate_text(cleaned, max_chars)


def _payload_field_paths(payload: Any, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            paths.update(_payload_field_paths(value, path))
    elif isinstance(payload, list):
        for item in payload[:3]:
            paths.update(_payload_field_paths(item, prefix))
    return paths


def _coverage_vs_payload_note(
    coverage_keys: list[str],
    payload: Any,
) -> str:
    """Коротко: что coverage просил vs что реально ушло в запросе."""
    payload_paths = _payload_field_paths(payload)
    covered_fields: list[str] = []
    for key in coverage_keys:
        if key.startswith("__"):
            covered_fields.append(key)
            continue
        field = key.split("=", 1)[0].strip()
        covered_fields.append(field)
    missing_related: list[str] = []
    # Типовые связки: mode без destination
    mode_covered = any("mode" in f for f in covered_fields)
    has_destination = any(
        p == "destination" or p.endswith(".destination") for p in payload_paths
    )
    if mode_covered and not has_destination:
        missing_related.append("settings.destination (нужен для gre/gretap)")
    lines = [
        f"- Coverage задал: {', '.join(f'`{k}`' for k in coverage_keys) or '—'}",
        f"- В payload есть поля: "
        f"{', '.join(f'`{p}`' for p in sorted(payload_paths)[:20]) or '—'}",
    ]
    if missing_related:
        lines.append(
            "- Пробел coverage→payload: " + "; ".join(missing_related)
        )
    return "\n".join(lines)


def extract_err_codes(response_body: Any) -> list[str]:
    codes: list[str] = []
    if not isinstance(response_body, dict):
        return codes

    def _collect(node: Any) -> None:
        if isinstance(node, dict):
            err = node.get("errCode")
            if err is not None:
                if isinstance(err, list):
                    codes.extend(str(item) for item in err)
                else:
                    codes.append(str(err))
            for value in node.values():
                _collect(value)
        elif isinstance(node, list):
            for item in node:
                _collect(item)

    _collect(response_body)
    seen: set[str] = set()
    unique: list[str] = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            unique.append(code)
    return unique


def _response_text(step: dict[str, Any]) -> str:
    body = step.get("response_body")
    if isinstance(body, str):
        return body.lower()
    try:
        return json.dumps(body, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return str(body).lower()


def _step_error_text(step: dict[str, Any]) -> str:
    parts = [
        str(step.get("error") or ""),
        str(step.get("note") or ""),
        _response_text(step),
    ]
    return " ".join(parts).lower()


def serialize_step_result(step: Any) -> dict[str, Any]:
    if is_dataclass(step):
        data = asdict(step)
    elif isinstance(step, dict):
        data = dict(step)
    else:
        raise TypeError(f"Unsupported step type: {type(step)!r}")

    data["err_codes"] = extract_err_codes(data.get("response_body"))
    data["response_body"] = _truncate_json(data.get("response_body"))
    data["request_payload"] = _truncate_json(data.get("request_payload"))
    return data


def _unwrap_failure_bundle(item: Any) -> tuple[Any, dict | None]:
    if isinstance(item, dict) and "result" in item:
        return item["result"], item.get("scenario")
    if is_dataclass(item) and hasattr(item, "result"):
        return item.result, getattr(item, "scenario", None)
    return item, None


def serialize_scenario_result(result: Any, scenario: dict | None = None) -> dict[str, Any]:
    if is_dataclass(result):
        endpoint = result.endpoint
        test_id = result.test_id
        description = result.description
        coverage_keys = list(result.coverage_keys)
        steps = list(result.steps)
    elif isinstance(result, dict) and "endpoint" in result:
        endpoint = result["endpoint"]
        test_id = result.get("test_id", 0)
        description = result.get("description", "")
        coverage_keys = list(result.get("coverage_keys", []))
        steps = result.get("steps", [])
    else:
        raise TypeError(f"Unsupported scenario type: {type(result)!r}")

    all_steps = [serialize_step_result(step) for step in steps]
    failed_steps = [
        step for step in all_steps
        if not step.get("passed", True)
    ]
    payload: dict[str, Any] = {
        "endpoint": endpoint,
        "test_id": test_id,
        "description": description,
        "coverage_keys": coverage_keys,
        "failed_steps": failed_steps,
        "all_steps": all_steps,
    }
    if scenario is not None:
        payload["scenario_definition"] = {
            "setup": scenario.get("setup", []),
            "main_test": scenario.get("main_test", {}),
            "teardown": scenario.get("teardown", []),
        }
    return payload


def load_dependencies_config(path: str | Path = "dependencies.json") -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_file():
        return {}
    with config_path.open(encoding="utf-8") as file:
        return json.load(file)


def load_endpoint_request_schema(
    endpoint: str,
    openapi_path: str | Path = "openapi.json",
) -> dict[str, Any] | None:
    if endpoint in _OPENAPI_SCHEMA_CACHE:
        return _OPENAPI_SCHEMA_CACHE[endpoint]
    try:
        from resolve_scheme import ResolveScheme

        resolved = ResolveScheme.resolve_endpoint(
            str(openapi_path), endpoint, "post",
        )
        schema = (
            resolved["requestBody"]["content"]["application/json"]["schema"]
        )
        _OPENAPI_SCHEMA_CACHE[endpoint] = schema
        return schema
    except (SystemExit, Exception) as exc:
        if not isinstance(exc, SystemExit):
            logger.debug("Не удалось загрузить схему %s: %s", endpoint, exc)
        _OPENAPI_SCHEMA_CACHE[endpoint] = None
        return None


def _collect_relevant_fields(
    scenario: dict | None,
    coverage_keys: list[str],
) -> set[str]:
    fields: set[str] = set()
    if scenario:
        for step in (
            *scenario.get("setup", []),
            scenario.get("main_test", {}),
            *scenario.get("teardown", []),
        ):
            payload = step.get("payload") if isinstance(step, dict) else None
            if isinstance(payload, dict):
                fields.update(payload.keys())
    for key in coverage_keys:
        if "=" in key:
            fields.add(key.split("=", 1)[0].strip().split(".")[0])
        elif key and not key.startswith("__"):
            fields.add(key.split(".")[0])
    return fields


def shrink_schema_for_prompt(
    schema: dict[str, Any] | None,
    *,
    relevant_fields: set[str],
) -> dict[str, Any] | None:
    if not schema:
        return None
    shrunk: dict[str, Any] = {
        "type": schema.get("type"),
        "required": schema.get("required", []),
        "additionalProperties": schema.get("additionalProperties"),
    }
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        picked: dict[str, Any] = {}
        for name in sorted(relevant_fields | set(shrunk.get("required") or [])):
            if name in properties:
                picked[name] = properties[name]
        if not picked and properties:
            for name in list(properties)[:10]:
                picked[name] = properties[name]
        shrunk["properties"] = picked
    text = json.dumps(shrunk, ensure_ascii=False)
    if len(text) > _MAX_SCHEMA_CHARS:
        return {"note": _truncate_text(text, _MAX_SCHEMA_CHARS)}
    return shrunk


def extract_dependencies_snippet(
    endpoint: str,
    dependencies: dict[str, Any],
) -> dict[str, Any]:
    snippet: dict[str, Any] = {}
    endpoint_rules = dependencies.get("endpoint_rules", {})
    if isinstance(endpoint_rules, dict) and endpoint in endpoint_rules:
        snippet["endpoint_rules"] = endpoint_rules[endpoint]

    iface_rules = dependencies.get("interface_rules", {})
    if isinstance(iface_rules, dict):
        matched = {
            name: rule
            for name, rule in iface_rules.items()
            if isinstance(rule, dict)
            and endpoint in json.dumps(rule, ensure_ascii=False)
        }
        if matched:
            snippet["interface_rules"] = matched
    return snippet


def _schema_allows_value(field_schema: dict[str, Any], value: Any) -> bool | None:
    if not isinstance(field_schema, dict):
        return None
    if "enum" in field_schema:
        return value in field_schema["enum"]
    if isinstance(value, (int, float)):
        minimum = field_schema.get("minimum")
        maximum = field_schema.get("maximum")
        if minimum is not None and value < minimum:
            return False
        if maximum is not None and value > maximum:
            return False
        return True
    if field_schema.get("type") == "string" and isinstance(value, str):
        min_len = field_schema.get("minLength")
        max_len = field_schema.get("maxLength")
        if min_len is not None and len(value) < min_len:
            return False
        if max_len is not None and len(value) > max_len:
            return False
        return True
    return None


def heuristic_classify_failure(failure: dict[str, Any]) -> dict[str, Any]:
    """Эвристика до/вместо Ollama: диагноз + как исправить."""
    coverage_keys = failure.get("coverage_keys", [])
    scenario = failure.get("scenario_definition", {})
    main_test = scenario.get("main_test", {}) if isinstance(scenario, dict) else {}
    main_payload = main_test.get("payload", {}) if isinstance(main_test, dict) else {}
    if not isinstance(main_payload, dict):
        main_payload = {}
    # Иногда payload только в failed_steps
    if not main_payload:
        for step in failure.get("failed_steps", []):
            if str(step.get("phase", "")).lower() == "main":
                payload = step.get("request_payload")
                if isinstance(payload, dict):
                    main_payload = payload
                break
    schema = failure.get("openapi_request_schema") or {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    failed_steps = failure.get("failed_steps", [])
    coverage_text = " ".join(str(k) for k in coverage_keys)
    endpoint = str(failure.get("endpoint", ""))

    def _verdict(
        classification: str,
        short: str,
        reason: str,
        fix: str,
        *,
        severity: str,
        confidence: str = "средняя",
    ) -> dict[str, Any]:
        return {
            "classification": classification,
            "classification_ru": CLASSIFICATION_RU[classification],
            "severity": severity,
            "severity_ru": SEVERITY_RU[severity],
            "confidence": confidence,
            "short_reason_ru": _truncate_text(short, 140),
            "reason_ru": reason,
            "fix_ru": fix,
        }

    settings = main_payload.get("settings")
    if not isinstance(settings, dict):
        settings = {}

    # Tunnel gretap/gre без destination
    if "destination" in " ".join(
        _step_error_text(s) for s in failed_steps
    ) and (
        "tunnel" in endpoint
        or settings.get("mode") in ("gretap", "gre", "ipip", "sit")
        or "gretap" in coverage_text
        or "mode=" in coverage_text
    ):
        mode = settings.get("mode") or "?"
        has_source = bool(settings.get("source") or main_payload.get("source"))
        return _verdict(
            "TEST_SETUP",
            f"Coverage только mode={mode}, нет settings.destination",
            (
                f"Coverage покрыл `settings.mode={mode}`, но генератор не добавил "
                f"связанное поле `settings.destination` "
                f"(source={'есть' if has_source else 'нет'}, destination отсутствует). "
                f"Для режима {mode} устройство требует destination — это дыра "
                f"в генерации связанных полей, а не «случайные кривые данные»."
            ),
            (
                "1) В dependencies.json: field_couplings — при settings.mode "
                "in (gretap, gre, …) ensure settings.destination "
                "(from_mock / value).\n"
                "2) Добавить destination в mock_data.by_field.\n"
                "3) Не генерировать coverage только по mode без связанных полей "
                "(связка закроет пробел).\n"
                "4) Перегенерировать тесты tunnel/add и перепрогнать."
            ),
            severity=SEVERITY_SERIOUS,
            confidence="высокая",
        )

    # __minimal__ без обязательных полей
    if "__minimal__" in coverage_text:
        missing = [field for field in required if field not in main_payload]
        if missing:
            return _verdict(
                "TEST_SETUP",
                f"__minimal__ без обязательных полей: {missing}",
                (
                    f"Сценарий с coverage __minimal__ отправил payload без "
                    f"обязательных полей схемы: {missing}. "
                    f"Фактически в запросе: {sorted(main_payload.keys()) or 'пусто'}."
                ),
                (
                    f"1) Исправить генератор: для __minimal__ заполнять {missing} "
                    f"из mock_data/defaults.\n"
                    f"2) Либо отключить __minimal__ для `{endpoint}`.\n"
                    f"3) Перегенерировать тесты эндпоинта."
                ),
                severity=SEVERITY_SERIOUS,
                confidence="высокая",
            )

    for step in failed_steps:
        phase = str(step.get("phase", "")).lower()
        err_text = _step_error_text(step)
        request_payload = step.get("request_payload")
        if isinstance(request_payload, str):
            try:
                request_payload = json.loads(request_payload)
            except json.JSONDecodeError:
                request_payload = {}

        if phase == "teardown" and any(
            token in err_text
            for token in ("no such", "not found", "does not exist", "не найден", "already")
        ):
            return _verdict(
                "TEST_SETUP",
                "Teardown: ресурс уже удалён",
                "Teardown не нашёл ресурс — часто нормально после сбоя main/setup "
                "или повторного delete.",
                "Можно игнорировать. При желании: сделать teardown идемпотентным "
                "или не считать такой FAIL критичным.",
                severity=SEVERITY_MINOR,
                confidence="средняя",
            )

        if any(
            token in err_text
            for token in (
                "operation not supported",
                "not supported",
                "cannot identify nic",
                "не поддерживается",
            )
        ):
            return _verdict(
                "ROUTER",
                "Операция не поддерживается на этом железе",
                (
                    f"Устройство отклонило `{step.get('endpoint', '?')}` как "
                    f"неподдерживаемую на текущем NIC/платформе."
                ),
                (
                    "1) Исключить эндпоинт/тест на этой платформе.\n"
                    "2) Сузить DEVICE_* в .env под реальное железо.\n"
                    "3) Не считать багом генератора, если на другом устройстве PASS."
                ),
                severity=SEVERITY_MINOR,
                confidence="высокая",
            )

        if "invalid" in err_text and isinstance(request_payload, dict):
            for field, value in request_payload.items():
                field_schema = properties.get(field, {})
                allowed = _schema_allows_value(field_schema, value)
                if allowed is True:
                    return _verdict(
                        "ROUTER",
                        f"Устройство отвергло допустимое по схеме `{field}={value}`",
                        (
                            f"Поле `{field}={value}` проходит OpenAPI, но устройство "
                            f"отвечает ошибкой. Вероятен разрыв схемы и реального API "
                            f"или скрытый лимит устройства."
                        ),
                        (
                            f"1) Уточнить реальный допустимый диапазон `{field}` на устройстве.\n"
                            f"2) Сузить OpenAPI / mock_data под реальные лимиты.\n"
                            f"3) Если схема верна — эскалировать как баг маршрутизатора "
                            f"с этим payload."
                        ),
                        severity=SEVERITY_SERIOUS,
                        confidence="высокая",
                    )
                if allowed is False:
                    return _verdict(
                        "BAD_DATA",
                        f"`{field}={value}` вне ограничений схемы",
                        f"Сгенерировано значение вне schema limits для `{field}`.",
                        (
                            f"1) Исправить coverage/JSF для `{field}`.\n"
                            f"2) Добавить reserved_values / mock_data.\n"
                            f"3) Перегенерировать тесты."
                        ),
                        severity=SEVERITY_SERIOUS,
                        confidence="высокая",
                    )
                if field in ("tx_queue_len", "vid", "vlan", "mode", "mtu"):
                    return _verdict(
                        "BAD_DATA",
                        f"Устройство отклонило `{field}={value}`",
                        (
                            f"Значение `{field}={value}` не принято устройством "
                            f"(coverage={coverage_keys})."
                        ),
                        (
                            f"1) Подобрать реалистичные значения `{field}` в mock_data.\n"
                            f"2) Не брать крайние UINT/max из схемы, если устройство "
                            f"их не принимает.\n"
                            f"3) Перегенерировать и перепрогнать."
                        ),
                        severity=SEVERITY_SERIOUS,
                        confidence="средняя",
                    )

        if phase == "setup":
            return _verdict(
                "TEST_SETUP",
                f"Упал setup `{step.get('endpoint', '?')}`",
                (
                    f"До main не дошли: setup `{step.get('endpoint', '?')}` вернул ошибку. "
                    f"Значит lifecycle/зависимости для теста собраны неверно."
                ),
                (
                    "1) Проверить setup в dependencies.json / interface_rules.\n"
                    "2) Убедиться, что create идёт до зависимых шагов "
                    "(ip_address, shutdown и т.д.).\n"
                    "3) Перегенерировать сценарий."
                ),
                severity=SEVERITY_SERIOUS,
                confidence="средняя",
            )

        status_code = step.get("status_code")
        if status_code and int(status_code) >= 500:
            return _verdict(
                "ROUTER",
                f"HTTP {status_code} на {phase}",
                "Сервер вернул 5xx — вероятная ошибка прошивки/API маршрутизатора.",
                (
                    "1) Повторить тот же payload вручную.\n"
                    "2) Если стабильно воспроизводится — эскалировать баг API "
                    "с request/response из лога.\n"
                    "3) Генератор не трогать, пока не доказана ошибка теста."
                ),
                severity=SEVERITY_SERIOUS,
                confidence="средняя",
            )

        if "not found" in err_text and phase == "main":
            return _verdict(
                "TEST_SETUP",
                "Main: ресурс не найден",
                "Main обратился к несуществующему ресурсу (интерфейс/VLAN/MAC) — "
                "setup не создал нужное состояние или coverage ссылается на чужие данные.",
                (
                    "1) Сверить setup и bind_fields в dependencies.json.\n"
                    "2) Проверить mock_data для связанных полей.\n"
                    "3) Перегенерировать тест."
                ),
                severity=SEVERITY_SERIOUS,
                confidence="средняя",
            )

        if any(
            token in err_text
            for token in (
                "mac address",
                "mask pair",
                "invalid address",
                "peer_address",
                "wrong",
            )
        ):
            return _verdict(
                "BAD_DATA",
                "Некорректные сетевые данные в payload",
                (
                    "Устройство отвергло адрес/MAC/peer как невалидные для этой "
                    "технологии. Coverage, скорее всего, подставил несовместимую "
                    "комбинацию полей."
                ),
                (
                    "1) Исправить mock_data / связку полей (MAC+mask, peer+ip и т.п.).\n"
                    "2) Не комбинировать несовместимые coverage_keys.\n"
                    "3) Перегенерировать тесты."
                ),
                severity=SEVERITY_SERIOUS,
                confidence="средняя",
            )

    return _verdict(
        "TEST_SETUP",
        "Нужен ручной разбор",
        "Автоматически не удалось выделить корневую причину.",
        (
            "1) Смотреть request/response в детальном разборе.\n"
            "2) Сверить со схемой и dependencies.json.\n"
            "3) Запустить анализ с доступной Ollama (--ollama-log)."
        ),
        severity=SEVERITY_MINOR,
        confidence="низкая",
    )


def enrich_failure(
    item: Any,
    *,
    openapi_path: str | Path = "openapi.json",
    dependencies: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result, scenario = _unwrap_failure_bundle(item)
    enriched = serialize_scenario_result(result, scenario)
    endpoint = enriched["endpoint"]
    relevant_fields = _collect_relevant_fields(scenario, enriched["coverage_keys"])
    full_schema = load_endpoint_request_schema(endpoint, openapi_path)
    enriched["openapi_request_schema"] = shrink_schema_for_prompt(
        full_schema,
        relevant_fields=relevant_fields,
    )
    if dependencies:
        enriched["dependencies"] = extract_dependencies_snippet(endpoint, dependencies)
    enriched["heuristic"] = heuristic_classify_failure(enriched)
    # Слот под ответ Ollama по одному тесту
    enriched["ollama"] = None
    return enriched


def build_run_analysis_context(
    *,
    failures: list[Any],
    summary: Any,
    endpoint_results: list[Any],
    run_log_path: str | Path,
    elapsed_sec: float,
    endpoints_count: int,
    openapi_path: str | Path = "openapi.json",
    dependencies_path: str | Path = "dependencies.json",
) -> dict[str, Any]:
    dependencies = load_dependencies_config(dependencies_path)
    enriched_failures = [
        enrich_failure(
            item,
            openapi_path=openapi_path,
            dependencies=dependencies,
        )
        for item in failures
    ]

    if is_dataclass(summary):
        summary_data = asdict(summary)
    else:
        summary_data = dict(summary)

    endpoints_table = []
    for item in endpoint_results:
        if is_dataclass(item):
            endpoints_table.append(asdict(item))
        else:
            endpoints_table.append(dict(item))

    classification_counts: dict[str, int] = defaultdict(int)
    severity_counts: dict[str, int] = defaultdict(int)
    for failure in enriched_failures:
        h = failure.get("heuristic", {})
        classification_counts[h.get("classification", "TEST_SETUP")] += 1
        severity_counts[h.get("severity", SEVERITY_MINOR)] += 1

    return {
        "run_log_path": str(run_log_path),
        "elapsed_sec": round(elapsed_sec, 2),
        "endpoints_count": endpoints_count,
        "summary": summary_data,
        "endpoint_results": endpoints_table,
        "failures": enriched_failures,
        "failure_count": len(enriched_failures),
        "heuristic_summary": {
            label: classification_counts.get(label, 0)
            for label in CLASSIFICATIONS
        },
        "severity_summary": {
            SEVERITY_SERIOUS: severity_counts.get(SEVERITY_SERIOUS, 0),
            SEVERITY_MINOR: severity_counts.get(SEVERITY_MINOR, 0),
        },
    }


def build_generation_analysis_context(
    *,
    gen_log_path: str | Path,
    endpoints: list[str],
    elapsed_sec: float,
) -> dict[str, Any]:
    path = Path(gen_log_path)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""

    warnings: list[str] = []
    errors: list[str] = []
    coverage_gaps: list[str] = []

    for line in text.splitlines():
        if "| WARNING |" in line or "| WARNING" in line:
            warnings.append(line.strip())
        if "| ERROR |" in line or "| CRITICAL |" in line:
            errors.append(line.strip())
        lower = line.lower()
        if "missing coverage" in lower or "не покрыты" in lower:
            coverage_gaps.append(line.strip())

    return {
        "gen_log_path": str(gen_log_path),
        "endpoints": endpoints,
        "elapsed_sec": round(elapsed_sec, 2),
        "warning_count": len(warnings),
        "error_count": len(errors),
        "warnings": warnings[-40:],
        "errors": errors[-40:],
        "coverage_gaps": coverage_gaps[-30:],
    }


def _failure_verdict(failure: dict[str, Any]) -> dict[str, Any]:
    """Итоговый вердикт: Ollama поверх эвристики, если есть."""
    ollama = failure.get("ollama") or {}
    heuristic = failure.get("heuristic") or {}
    classification = ollama.get("classification") or heuristic.get("classification")
    if classification not in CLASSIFICATIONS:
        classification = heuristic.get("classification", "TEST_SETUP")
    severity = ollama.get("severity") or heuristic.get("severity", SEVERITY_MINOR)
    if severity not in (SEVERITY_SERIOUS, SEVERITY_MINOR):
        severity = SEVERITY_MINOR

    heuristic_short = heuristic.get("short_reason_ru") or "Нужен ручной разбор"
    heuristic_reason = heuristic.get("reason_ru") or heuristic_short
    heuristic_fix = heuristic.get("fix_ru") or (
        "Сверить coverage и payload; поправить генератор или dependencies.json."
    )

    raw_short = ollama.get("short_reason_ru") or heuristic_short
    raw_reason = ollama.get("reason_ru") or heuristic_reason
    short = _clean_diagnosis(str(raw_short), fallback=str(heuristic_short), max_chars=120)
    reason = _clean_diagnosis(str(raw_reason), fallback=str(heuristic_reason), max_chars=600)
    # Если Ollama вернула шум — полностью берём эвристику
    if _is_noisy_diagnosis(str(raw_short)) or _is_noisy_diagnosis(str(raw_reason)):
        short = _clean_diagnosis(str(heuristic_short), fallback=heuristic_short, max_chars=120)
        reason = str(heuristic_reason)
        fix = str(heuristic_fix)
        source = "heuristic"
        thinking = ""
    else:
        fix = str(ollama.get("fix_ru") or heuristic_fix)
        source = "ollama" if ollama.get("reason_ru") else "heuristic"
        thinking = str(ollama.get("thinking_ru") or "")
        if _is_noisy_diagnosis(thinking):
            thinking = ""

    if not fix.strip():
        fix = heuristic_fix

    return {
        "classification": classification,
        "classification_ru": CLASSIFICATION_RU.get(
            classification, classification,
        ),
        "severity": severity,
        "severity_ru": SEVERITY_RU.get(severity, severity),
        "short_reason_ru": short,
        "reason_ru": reason,
        "fix_ru": fix,
        "thinking_ru": thinking,
        "confidence": ollama.get("confidence") or heuristic.get("confidence", "?"),
        "source": source,
    }


def _primary_request_response(failure: dict[str, Any]) -> tuple[Any, Any, dict]:
    """Берём главный упавший шаг (main предпочтительнее)."""
    failed = failure.get("failed_steps") or []
    step = None
    for candidate in failed:
        if str(candidate.get("phase", "")).lower() == "main":
            step = candidate
            break
    if step is None and failed:
        step = failed[0]
    if step is None:
        return None, None, {}
    return step.get("request_payload"), step.get("response_body"), step


def _parse_ollama_json(text: str) -> dict[str, Any] | None:
    cleaned = _strip_markdown_fence(text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _normalize_ollama_verdict(raw: dict[str, Any], heuristic: dict[str, Any]) -> dict[str, Any]:
    classification = str(raw.get("classification", "")).upper().strip()
    # Синонимы от модели
    aliases = {
        "TEST_SETUP": "TEST_SETUP",
        "SETUP": "TEST_SETUP",
        "TEST": "TEST_SETUP",
        "GENERATOR": "TEST_SETUP",
        "ОШИБКА_ТЕСТА": "TEST_SETUP",
        "ROUTER": "ROUTER",
        "DEVICE": "ROUTER",
        "DEVICE_API": "ROUTER",
        "МАРШРУТИЗАТОР": "ROUTER",
        "BAD_DATA": "BAD_DATA",
        "DATA": "BAD_DATA",
        "НЕКОРРЕКТНЫЕ_ДАННЫЕ": "BAD_DATA",
    }
    classification = aliases.get(classification, classification)
    if classification not in CLASSIFICATIONS:
        classification = heuristic.get("classification", "TEST_SETUP")

    severity = str(raw.get("severity", "")).lower().strip()
    if severity in ("serious", "major", "critical", "серьезная", "серьёзная"):
        severity = SEVERITY_SERIOUS
    elif severity in ("minor", "ok", "low", "условно"):
        severity = SEVERITY_MINOR
    else:
        severity = heuristic.get("severity", SEVERITY_MINOR)

    reason = str(raw.get("reason_ru") or raw.get("reason") or "").strip()
    if not reason:
        reason = heuristic.get("reason_ru", "")
    short = str(raw.get("short_reason_ru") or raw.get("short_reason") or "").strip()
    if not short:
        short = heuristic.get("short_reason_ru") or _truncate_text(reason, 120)
    short = _clean_diagnosis(
        short,
        fallback=str(heuristic.get("short_reason_ru") or "Нужен разбор"),
        max_chars=120,
    )
    reason = _clean_diagnosis(
        reason,
        fallback=str(heuristic.get("reason_ru") or short),
        max_chars=600,
    )
    fix = str(
        raw.get("fix_ru")
        or raw.get("fix")
        or raw.get("how_to_fix")
        or ""
    ).strip()
    if not fix or _is_noisy_diagnosis(fix):
        fix = heuristic.get("fix_ru", "")

    thinking = str(
        raw.get("thinking_ru")
        or raw.get("analysis")
        or raw.get("diagnosis_ru")
        or ""
    ).strip()
    if _is_noisy_diagnosis(thinking):
        thinking = ""

    return {
        "classification": classification,
        "classification_ru": CLASSIFICATION_RU[classification],
        "severity": severity,
        "severity_ru": SEVERITY_RU[severity],
        "short_reason_ru": short,
        "reason_ru": reason,
        "fix_ru": fix,
        "confidence": str(raw.get("confidence") or "средняя"),
        "thinking_ru": thinking,
    }


def _build_single_failure_prompt(failure: dict[str, Any]) -> str:
    request, response, step = _primary_request_response(failure)
    compact = {
        "endpoint": failure["endpoint"],
        "test_id": failure["test_id"],
        "coverage_keys": failure.get("coverage_keys", []),
        "description": failure.get("description", ""),
        "failed_phase": step.get("phase") if step else None,
        "http_status": step.get("status_code") if step else None,
        "expected_status": step.get("expected_status") if step else None,
        "err_codes": step.get("err_codes") if step else [],
        "request": request,
        "response": response,
        "scenario": failure.get("scenario_definition"),
        "openapi_request_schema": failure.get("openapi_request_schema"),
        "dependencies": failure.get("dependencies"),
        "heuristic_hint": failure.get("heuristic"),
    }
    payload = json.dumps(compact, ensure_ascii=False, indent=2)
    return (
        "Проанализируй ОДИН упавший тест.\n\n"
        "ВАЖНО:\n"
        "- НЕ копируй и не пересказывай текст ошибки устройства.\n"
        "- Сравни request, response, coverage_keys и схему.\n"
        "- Найди корневую причину (чего не хватает / что лишнее / что несовместимо).\n"
        "- Дай пошаговую инструкцию исправления для разработчика генератора тестов.\n\n"
        "Классификация — ровно одно значение:\n"
        "- TEST_SETUP — ошибка теста/генератора: неполный setup, "
        "coverage без связанных полей (например mode без destination), "
        "пропущены шаги lifecycle\n"
        "- ROUTER — ошибка маршрутизатора: баг или реальное ограничение устройства\n"
        "- BAD_DATA — поля в payload ЕСТЬ, но значения неверны для технологии "
        "(невалидный MAC/IP/VID и т.п.)\n\n"
        "Серьёзность:\n"
        "- serious — нужно чинить\n"
        "- minor — условно «ну и ладно»\n\n"
        "Ответь ТОЛЬКО JSON-объектом без markdown:\n"
        "{\n"
        '  "classification": "TEST_SETUP|ROUTER|BAD_DATA",\n'
        '  "severity": "serious|minor",\n'
        '  "short_reason_ru": "краткий диагноз без копипаста ошибки",\n'
        '  "reason_ru": "корневая причина: что именно не так в тесте/схеме/устройстве",\n'
        '  "fix_ru": "пошаговая инструкция: 1) ... 2) ... 3) ...",\n'
        '  "thinking_ru": "ход рассуждения по фактам (без воды)",\n'
        '  "confidence": "высокая|средняя|низкая"\n'
        "}\n\n"
        "Пример хорошего short_reason_ru: "
        "«Для gretap в payload нет settings.destination».\n"
        "Пример плохого: «ожидался HTTP 200, получен HTTP 400 …».\n\n"
        f"Данные теста:\n{payload}\n"
    )


def format_run_report(context: dict[str, Any]) -> str:
    """Компактный отчёт: факты → диагноз → как исправить (без дублей)."""
    summary = context.get("summary", {})
    passed = summary.get("passed_scenarios", 0)
    failed = summary.get("failed_scenarios", 0)
    total = summary.get("total_scenarios", passed + failed)
    failures = context.get("failures", [])

    lines: list[str] = [
        "# Отчёт анализа прогона тестов",
        "",
        f"**Лог прогона:** `{context.get('run_log_path', '?')}`",
        f"**Время:** {context.get('elapsed_sec', '?')} с",
        "",
        "## Сводка",
        "",
        f"- **Прошло:** {passed}",
        f"- **Не прошло:** {failed}",
        f"- **Всего:** {total}",
        "",
    ]

    if not failures:
        lines.extend(["Все тесты прошли успешно.", ""])
        return "\n".join(lines)

    # Какие упали + краткая таблица диагнозов
    lines.append("## Упавшие тесты")
    lines.append("")
    lines.append("| Эндпоинт | # | Класс | Серьёзность | Диагноз |")
    lines.append("| --- | ---: | --- | --- | --- |")
    for failure in failures:
        v = _failure_verdict(failure)
        diagnosis = v["short_reason_ru"].replace("|", "\\|")
        lines.append(
            f"| `{failure['endpoint']}` | {failure['test_id']} | "
            f"{v['classification_ru']} | {v['severity_ru']} | {diagnosis} |"
        )
    lines.append("")

    class_counts: dict[str, int] = defaultdict(int)
    for failure in failures:
        class_counts[_failure_verdict(failure)["classification"]] += 1
    parts = [
        f"{CLASSIFICATION_RU[key]}: {class_counts.get(key, 0)}"
        for key in CLASSIFICATIONS
        if class_counts.get(key, 0)
    ]
    if parts:
        lines.append("**Итого по классам:** " + "; ".join(parts))
        lines.append("")

    lines.append("## Детальный разбор")
    lines.append("")
    for failure in failures:
        v = _failure_verdict(failure)
        request, response, step = _primary_request_response(failure)
        lines.append(f"### `{failure['endpoint']}` — тест #{failure['test_id']}")
        lines.append("")
        lines.append(
            f"**Класс:** {v['classification_ru']} · "
            f"**Серьёзность:** {v['severity_ru']} · "
            f"**Источник:** {v['source']}"
        )
        if step:
            err_codes = step.get("err_codes") or []
            err_part = f", errCode: {', '.join(str(c) for c in err_codes)}" if err_codes else ""
            lines.append(
                f"**Факт:** {step.get('phase', '?')} → "
                f"HTTP {step.get('status_code', '?')} "
                f"(ожидали {step.get('expected_status', '?')}{err_part})"
            )

        if failure.get("coverage_keys") or request is not None:
            lines.append("")
            lines.append("**Coverage vs payload:**")
            lines.append(
                _coverage_vs_payload_note(
                    list(failure.get("coverage_keys") or []),
                    request,
                )
            )

        lines.append("")
        lines.append("**Запрос:**")
        lines.append("```json")
        lines.append(
            json.dumps(request, ensure_ascii=False, indent=2)
            if request is not None else "null"
        )
        lines.append("```")
        lines.append("")
        lines.append("**Ответ:**")
        lines.append("```json")
        lines.append(
            json.dumps(response, ensure_ascii=False, indent=2)
            if response is not None else "null"
        )
        lines.append("```")

        lines.append("")
        lines.append("**Диагноз:**")
        lines.append(v["reason_ru"] or "_нет_")
        if v.get("thinking_ru"):
            lines.append("")
            lines.append("**Рассуждение:**")
            lines.append(v["thinking_ru"])
        lines.append("")
        lines.append("**Как исправить:**")
        lines.append(v.get("fix_ru") or "_нет_")
        lines.append("")

    serious = []
    minor = []
    for failure in failures:
        v = _failure_verdict(failure)
        row = (
            f"`{failure['endpoint']}` #{failure['test_id']} — {v['short_reason_ru']}"
        )
        if v["severity"] == SEVERITY_SERIOUS:
            serious.append(row)
        else:
            minor.append(row)

    lines.append("## Итог")
    lines.append("")
    lines.append("### Критические")
    lines.append("")
    if serious:
        lines.extend(f"- {row}" for row in serious)
    else:
        lines.append("_Нет_")
    lines.append("")
    lines.append("### Некритические")
    lines.append("")
    if minor:
        lines.extend(f"- {row}" for row in minor)
    else:
        lines.append("_Нет_")
    lines.append("")

    return "\n".join(lines)


def _format_generation_fallback_report(context: dict[str, Any]) -> str:
    lines = [
        "# Отчёт анализа генерации тестов",
        "",
        f"**Лог генерации:** `{context['gen_log_path']}`",
        f"**Эндпоинтов:** {len(context.get('endpoints', []))} | "
        f"**Время:** {context['elapsed_sec']} с",
        "",
        "## Сводка",
        "",
        f"- WARNING: {context.get('warning_count', 0)}",
        f"- ERROR/CRITICAL: {context.get('error_count', 0)}",
        "",
    ]
    if context.get("errors"):
        lines.extend(["## Ошибки", ""])
        lines.extend(f"- {line}" for line in context["errors"])
        lines.append("")
    if context.get("warnings"):
        lines.extend(["## Предупреждения", ""])
        lines.extend(f"- {line}" for line in context["warnings"])
        lines.append("")
    return "\n".join(lines)


def _build_generation_analysis_prompt(context: dict[str, Any]) -> str:
    payload = json.dumps(context, ensure_ascii=False, indent=2)
    return (
        "Ты анализируешь лог генерации REST API тестов.\n"
        "Напиши на русском Markdown-отчёт: статус, ошибки, пробелы покрытия, рекомендации.\n\n"
        f"Данные:\n{payload}\n"
    )


def _strip_markdown_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json|markdown|md)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


class OllamaOrchestrator:
    """Клиент Ollama: анализ каждого FAIL по одному + сборка отчёта."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.1,
        timeout_sec: int = 180,
        cache_dir: str | Path = DEFAULT_CACHE_DIR,
    ):
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout_sec = timeout_sec
        self.cache_dir = Path(cache_dir)
        self._available: bool | None = None

    @classmethod
    def from_cli(cls, use_ollama: bool) -> OllamaOrchestrator:
        orchestrator = cls(enabled=use_ollama)
        if not use_ollama:
            return orchestrator
        if orchestrator.is_available():
            logger.info("Ollama: доступна (анализ по одному FAIL)")
        else:
            logger.warning(
                "Ollama запрошена, но недоступна — отчёт по эвристикам."
            )
        return orchestrator

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        if self._available is not None:
            return self._available
        try:
            import requests

            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            self._available = resp.status_code == 200
        except Exception as exc:
            logger.warning("Ollama недоступна: %s", exc)
            self._available = False
        return self._available

    def _cache_key(self, prompt: str) -> str:
        payload = f"{PROMPT_VERSION}|{self.model}|{self.temperature}|{prompt}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _read_cache(self, key: str) -> str | None:
        path = self.cache_dir / f"{key}.txt"
        if path.is_file():
            logger.debug("Cache hit: %s", key[:12])
            return path.read_text(encoding="utf-8")
        return None

    def _write_cache(self, key: str, value: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{key}.txt").write_text(value, encoding="utf-8")

    def generate(
        self,
        prompt: str,
        *,
        use_cache: bool = True,
        system: str | None = None,
    ) -> str:
        if not self.is_available():
            raise RuntimeError("Ollama is not available")

        cache_key = self._cache_key(f"{system or ''}|{prompt}")
        if use_cache:
            cached = self._read_cache(cache_key)
            if cached is not None:
                return cached

        import requests

        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        if system:
            payload["system"] = system

        logger.debug(
            "Ollama request: model=%s, prompt_len=%d",
            self.model, len(prompt),
        )
        resp = requests.post(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        text = _strip_markdown_fence(resp.json().get("response", ""))
        if use_cache and text:
            self._write_cache(cache_key, text)
        return text

    def analyze_single_failure(
        self,
        failure: dict[str, Any],
        *,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Отправляет в Ollama один FAIL (+ схема) и возвращает вердикт."""
        heuristic = failure.get("heuristic") or {}
        if not self.is_available():
            return _normalize_ollama_verdict({}, heuristic)

        prompt = _build_single_failure_prompt(failure)
        try:
            raw_text = self.generate(
                prompt,
                use_cache=use_cache,
                system=SYSTEM_PROMPT_RU,
            )
            parsed = _parse_ollama_json(raw_text)
            if parsed is None:
                # Модель ответила текстом — сохраняем как reason
                return _normalize_ollama_verdict(
                    {
                        "classification": heuristic.get("classification"),
                        "severity": heuristic.get("severity"),
                        "reason_ru": raw_text[:1500],
                        "thinking_ru": raw_text[:1500],
                        "short_reason_ru": _truncate_text(raw_text, 140),
                    },
                    heuristic,
                )
            return _normalize_ollama_verdict(parsed, heuristic)
        except Exception as exc:
            logger.warning(
                "Ollama fail %s #%s: %s",
                failure.get("endpoint"),
                failure.get("test_id"),
                exc,
            )
            return _normalize_ollama_verdict({}, heuristic)

    def analyze_run(
        self,
        context: dict[str, Any],
        *,
        use_cache: bool = True,
    ) -> str:
        """Анализирует каждый FAIL по одному, затем собирает отчёт."""
        failures = context.get("failures", [])
        total = len(failures)
        if total and self.is_available():
            logger.info("Ollama: анализ %d упавших тестов по одному…", total)
            for index, failure in enumerate(failures, 1):
                logger.info(
                    "Ollama [%d/%d]: %s #%s",
                    index,
                    total,
                    failure.get("endpoint"),
                    failure.get("test_id"),
                )
                print(
                    f"Ollama [{index}/{total}] "
                    f"{failure.get('endpoint')} #{failure.get('test_id')}…",
                    flush=True,
                )
                failure["ollama"] = self.analyze_single_failure(
                    failure, use_cache=use_cache,
                )
        return format_run_report(context)

    def analyze_generation(
        self,
        context: dict[str, Any],
        *,
        use_cache: bool = True,
    ) -> str:
        if self.is_available():
            try:
                prompt = _build_generation_analysis_prompt(context)
                return self.generate(
                    prompt,
                    use_cache=use_cache,
                    system=SYSTEM_PROMPT_RU,
                )
            except Exception as exc:
                logger.warning("Ollama analyze_generation failed: %s", exc)
        return _format_generation_fallback_report(context)

    def write_report(
        self,
        report_path: str | Path,
        body: str,
        *,
        context: dict[str, Any] | None = None,
        kind: str = "run",
    ) -> Path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        header_lines = [
            f"<!-- ollama_faker {kind} analysis {PROMPT_VERSION} -->",
            f"<!-- generated: {time.strftime('%Y-%m-%d %H:%M:%S')} -->",
        ]
        if context:
            if kind == "run" and context.get("run_log_path"):
                header_lines.append(f"<!-- source: {context['run_log_path']} -->")
            if kind == "gen" and context.get("gen_log_path"):
                header_lines.append(f"<!-- source: {context['gen_log_path']} -->")
        content = "\n".join(header_lines) + "\n\n" + body.strip() + "\n"
        path.write_text(content, encoding="utf-8")
        logger.info("Ollama-отчёт: %s", path.as_posix())
        return path

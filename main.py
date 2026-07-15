from __future__ import annotations

import json
import os
import re
import copy
import time
import logging
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from jsf import JSF
from ollama_orchestrator import OllamaOrchestrator
from resolve_scheme import ResolveScheme
from test_paths import endpoint_to_test_file
from log_paths import build_log_path

# =============================================================================
# ГЛОБАЛЬНАЯ НАСТРОЙКА ЛОГИРОВАНИЯ
# =============================================================================
# Именованный логгер процесса генерации (видно в каждой строке лога как MAIN).
logger = logging.getLogger("MAIN")

# Единый формат строк: время | уровень | имя логгера | сообщение
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Файл лога по умолчанию до вызова configure_logging() в main().
# Имя: logs/gen_<datetime>_all.log (пока не передали -e/-d).
_CURRENT_LOG_FILE = build_log_path("gen")


def build_generation_log_path(
    *,
    endpoints: list[str] | None = None,
    dir_prefixes: list[str] | None = None,
) -> Path:
    """
    Обёртка над общим build_log_path для генератора.
    Всегда префикс "gen" → logs/gen_<datetime>_<scope>.log
    """
    return build_log_path("gen", endpoints=endpoints, dir_prefixes=dir_prefixes)


class _ListLogHandler(logging.Handler):
    """
    Handler для параллельных воркеров (ProcessPoolExecutor).

    Воркер не пишет в файл напрямую (конкуренция процессов опасна):
    складывает строки в list, а главный процесс потом дописывает блоком.
    """

    def __init__(self, buffer: list[str]):
        super().__init__()
        self._buffer = buffer
        self.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        # Одна готовая строка лога + перевод строки — как в обычном FileHandler
        self._buffer.append(self.format(record) + "\n")


@dataclass
class _EndpointTaskResult:
    """Результат генерации одного эндпоинта из пула воркеров."""

    endpoint: str
    log_lines: list[str]  # накопленный лог воркера (вставить в общий файл)
    error: BaseException | None = None


def configure_logging(
    debug: bool = False,
    *,
    filemode: str = "w",
    log_file: Path | str | None = None,
) -> Path:
    """
    Инициализирует корневой logging на файл генерации.

    debug=True (-v)  → DEBUG (подробности по схеме, mock_data и т.д.)
    filemode="w"     → новый файл на каждый запуск (не дописываем к старому)
    log_file         → явный путь; если None, остаётся _CURRENT_LOG_FILE
    """
    global _CURRENT_LOG_FILE
    if log_file is not None:
        _CURRENT_LOG_FILE = Path(log_file)
        # На случай logs/ или вложенного каталога из --custom path
        _CURRENT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        filename=str(_CURRENT_LOG_FILE),
        filemode=filemode,
        level=level,
        format=_LOG_FORMAT,
        datefmt=_LOG_DATE_FORMAT,
        force=True,  # переконфигурировать, если logging уже кем-то настроен
        encoding="utf-8",
    )
    return _CURRENT_LOG_FILE


def get_generation_log_file() -> Path:
    """Текущий файл лога генерации (удобно в тестах / отладке)."""
    return _CURRENT_LOG_FILE


def _configure_worker_capture_logging(verbose: bool) -> list[str]:
    """
    В дочернем процессе: все logger.* → только в память (list), не в файл.

    Главный процесс потом вызовет _append_log_block() и допишет буфер
    в общий logs/gen_*.log без гонок записи.
    """
    buffer: list[str] = []
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.handlers.clear()  # убираем унаследованные FileHandler из родителя
    root.setLevel(level)
    root.addHandler(_ListLogHandler(buffer))
    # Дочерние логгеры (MAIN и др.) пусть прокидывают в root, без своих handlers
    for logger_name in list(logging.root.manager.loggerDict):
        child = logging.getLogger(logger_name)
        child.handlers.clear()
        child.propagate = True
        child.setLevel(level)
    return buffer


def _flush_log_handlers() -> None:
    """Сбрасывает буферы handlers на диск перед ручной допиской в файл."""
    for handler in logging.root.handlers:
        handler.flush()


def _append_log_block(lines: list[str]) -> None:
    """Дописывает блок строк от воркера в конец общего файла генерации."""
    if not lines:
        return
    _flush_log_handlers()
    with open(_CURRENT_LOG_FILE, "a", encoding="utf-8") as log_file:
        log_file.writelines(lines)


def _write_generation_summary(started_at: float, endpoint_count: int) -> None:
    """
    Финальная строка в лог после всей генерации (включая параллельных воркеров).

    Пишем напрямую в файл: к моменту finally обычные logger.info уже могут
    быть неинформативны (воркеры/handlers), а summary всегда нужен в конце.
    """
    _flush_log_handlers()
    elapsed = time.time() - started_at
    message = (
        f"Генерация завершена. Общее время: {elapsed:.2f} сек. "
        f"(эндпоинтов: {endpoint_count})"
    )
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{timestamp} | INFO    | MAIN            | {message}\n"
    with open(_CURRENT_LOG_FILE, "a", encoding="utf-8") as log_file:
        log_file.write(line)


# =============================================================================
# ПРЕПРОЦЕССИНГ СХЕМЫ ДЛЯ JSF
# =============================================================================
def _infer_json_schema_type_from_consts(consts: list) -> str | None:
    """Тип схемы из значений const (oneOf[const,…] → enum)."""
    if not consts:
        return None
    py_types = {type(value) for value in consts}
    if py_types == {bool} or (py_types <= {bool} and bool in py_types):
        # bool раньше int: isinstance(True, int) == True в Python
        if all(isinstance(value, bool) for value in consts):
            return "boolean"
    if py_types <= {int, bool} and all(isinstance(v, int) and not isinstance(v, bool) for v in consts):
        return "integer"
    if py_types <= {int, float, bool} and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in consts
    ):
        return "number"
    if py_types == {str}:
        return "string"
    if py_types == {type(None)}:
        return "null"
    return None


def preprocess_schema_for_jsf(schema: dict) -> dict:
    if not isinstance(schema, dict):
        return schema
    new_schema = copy.deepcopy(schema)
    
    # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ ДЛЯ JSF:
    # Если есть properties или required, но нет type, JSF ломается.
    # Добавляем type: object явно.
    if ('properties' in new_schema or 'required' in new_schema) and 'type' not in new_schema:
        new_schema['type'] = 'object'
        logger.debug(f"Добавлен type: object для схемы с properties/required")

    for keyword in ['oneOf', 'anyOf']:
        if keyword in new_schema:
            options = new_schema[keyword]
            consts = [opt['const'] for opt in options if isinstance(opt, dict) and 'const' in opt]
            is_all_consts = all('const' in opt for opt in options if isinstance(opt, dict))
            
            if is_all_consts and consts:
                new_schema['enum'] = consts
                del new_schema[keyword]
                # type по типу const: иначе 1024 + type:string → всегда FAIL
                if 'type' not in new_schema:
                    inferred = _infer_json_schema_type_from_consts(consts)
                    if inferred:
                        new_schema['type'] = inferred
                logger.debug(
                    f"{keyword} с const → enum: {consts}"
                    + (f", type={new_schema.get('type')}" if new_schema.get('type') else "")
                )
            else:
                has_null = any(isinstance(o, dict) and o.get('type') == 'null' for o in options)
                non_null_opts = [o for o in options if not (isinstance(o, dict) and o.get('type') == 'null')]
                if has_null and len(non_null_opts) == 1:
                    inner = preprocess_schema_for_jsf(copy.deepcopy(non_null_opts[0]))
                    new_schema.clear()
                    new_schema.update(inner)
                    new_schema['x-nullable'] = True
                    logger.debug(f"Раскрыт nullable {keyword}")
                else:
                    new_schema[keyword] = [preprocess_schema_for_jsf(opt) for opt in options]

    if 'properties' in new_schema:
        for k, v in new_schema['properties'].items():
            new_schema['properties'][k] = preprocess_schema_for_jsf(v)
    if 'items' in new_schema:
        new_schema['items'] = preprocess_schema_for_jsf(new_schema['items'])
        
    return new_schema

# =============================================================================
# ПОКРЫТИЕ ЗНАЧЕНИЙ ПОЛЕЙ (enum / min-max / boolean / pattern)
# =============================================================================

def _schema_allows_null(schema: dict) -> bool:
    if schema.get('x-nullable'):
        return True
    field_type = schema.get('type')
    return isinstance(field_type, list) and 'null' in field_type


def _expand_nullable_schema(schema: dict) -> dict:
    """Преобразует x-nullable в oneOf с null для jsonschema."""
    if not isinstance(schema, dict):
        return schema
    schema = copy.deepcopy(schema)

    if schema.get('x-nullable'):
        base = {k: v for k, v in schema.items() if k != 'x-nullable'}
        return {'oneOf': [_expand_nullable_schema(base), {'type': 'null'}]}

    for key in ('properties', 'patternProperties'):
        if key in schema and isinstance(schema[key], dict):
            schema[key] = {k: _expand_nullable_schema(v) for k, v in schema[key].items()}
    if 'items' in schema and isinstance(schema['items'], dict):
        schema['items'] = _expand_nullable_schema(schema['items'])
    if isinstance(schema.get('additionalProperties'), dict):
        schema['additionalProperties'] = _expand_nullable_schema(schema['additionalProperties'])
    for key in ('oneOf', 'anyOf', 'allOf'):
        if key in schema:
            schema[key] = [
                _expand_nullable_schema(b) if isinstance(b, dict) else b
                for b in schema[key]
            ]
    return schema


def _is_valid_for_schema(value, schema: dict) -> bool:
    if value is None and _schema_allows_null(schema):
        return True
    try:
        import jsonschema
        jsonschema.validate(instance=value, schema=_expand_nullable_schema(schema))
        return True
    except Exception:
        return False


def _first_concrete_value(prop_schema: dict):
    """Первое осмысленное не-null значение свойства (без полного collect_test_values)."""
    if _schema_has_composition(prop_schema):
        for branch in _iter_schema_branches(prop_schema):
            val = _first_concrete_value(branch)
            if val is not None:
                return val
        return None

    if prop_schema.get('const') is not None:
        return prop_schema['const']
    if prop_schema.get('enum'):
        return prop_schema['enum'][0]

    field_type = _resolve_schema_type(prop_schema)

    if field_type == 'boolean':
        return True
    if field_type in ('integer', 'number'):
        if 'minimum' in prop_schema:
            return prop_schema['minimum']
        if 'maximum' in prop_schema:
            return prop_schema['maximum']
    if field_type == 'object':
        return _filled_object(prop_schema)
    if field_type == 'array':
        if prop_schema.get('prefixItems'):
            return _generate_prefix_items_array(prop_schema)
        items = prop_schema.get('items', {})
        if _resolve_schema_type(items) == 'object':
            inner = _filled_object(items)
            return [inner] if inner is not None else []
        try:
            return [_jsf_generate(items)]
        except Exception:
            return []
    try:
        return _jsf_generate(prop_schema)
    except Exception:
        return None


def _filled_object(schema: dict):
    """Object хотя бы с одним заполненным полем — для осмысленных тестов вместо {}."""
    if not isinstance(schema, dict):
        return None

    if _schema_has_composition(schema):
        for branch in _iter_schema_branches(schema):
            inner = _filled_object(branch)
            if inner:
                return inner
        return None

    if _resolve_schema_type(schema) != 'object':
        return None

    result = _minimal_object_composed(schema)
    if result:
        return result

    for prop_name, prop_schema in schema.get('properties', {}).items():
        val = _first_concrete_value(prop_schema)
        if val is None:
            continue
        candidate = {prop_name: val}
        if _is_valid_for_schema(candidate, schema):
            return _coerce_payload_to_schema(candidate, schema)

    try:
        generated = _jsf_generate(schema)
        if isinstance(generated, dict) and generated:
            return generated
    except Exception:
        pass
    return None


def _append_null_if_allowed(schema: dict, values: list) -> list:
    if _schema_allows_null(schema) and None not in values:
        values.append(None)
    return values


def _resolve_schema_type(schema: dict):
    field_type = schema.get('type')
    if isinstance(field_type, list):
        non_null = [t for t in field_type if t != 'null']
        return non_null[0] if non_null else None
    return field_type


def _generate_prefix_items_array(schema: dict) -> list:
    """JSON Schema prefixItems (кортеж-массив). JSF это не поддерживает."""
    result = []
    for item_schema in schema.get('prefixItems', []):
        if not isinstance(item_schema, dict):
            result.append(None)
            continue
        item_type = _resolve_schema_type(item_schema)
        if item_type == 'object' or _schema_has_composition(item_schema):
            val = _filled_object(item_schema) or _minimal_object_composed(item_schema) or {}
        else:
            vals = collect_test_values(item_schema)
            val = next((v for v in vals if v is not None), None)
            if val is None:
                try:
                    val = _jsf_generate(item_schema)
                except Exception:
                    val = None
        result.append(val)
    return result


def _generate_array_value(schema: dict) -> list:
    if schema.get('prefixItems'):
        return _generate_prefix_items_array(schema)
    items = schema.get('items', {})
    if _resolve_schema_type(items) == 'object':
        return [_minimal_object_composed(items)]
    return [_jsf_generate(items)]


def _jsf_generate(schema: dict):
    if isinstance(schema, dict) and schema.get('prefixItems'):
        return _coerce_payload_to_schema(_generate_prefix_items_array(schema), schema)
    try:
        value = JSF(schema).generate()
        return _coerce_payload_to_schema(value, schema)
    except Exception:
        if _resolve_schema_type(schema) == 'array':
            return _coerce_payload_to_schema(_generate_array_value(schema), schema)
        raise


def _coerce_payload_to_schema(value, schema: dict):
    """Приводит значения к типам схемы (0/1 → false/true для boolean и т.п.)."""
    if not isinstance(schema, dict):
        return value

    if isinstance(value, (dict, list)):
        value = copy.deepcopy(value)

    if 'oneOf' in schema:
        if isinstance(value, dict):
            # Не прогоняем payload через ВСЕ ветки — только через совместимую
            branch = None
            for field_name, field_value in value.items():
                branch = _find_oneof_branch_for_field(schema, field_name, field_value)
                if branch is not None and _branch_accepts_field_value(
                    branch, field_name, field_value,
                ):
                    break
            if branch is None:
                branch = next(
                    (
                        b for b in schema['oneOf']
                        if isinstance(b, dict) and b.get('type') != 'null'
                    ),
                    None,
                )
            if branch is not None:
                value = _coerce_payload_to_schema(value, branch)
        return value

    field_type = _resolve_schema_type(schema)

    if field_type == 'boolean' and isinstance(value, int) and value in (0, 1):
        return bool(value)

    if field_type == 'object' and isinstance(value, dict):
        props = schema.get('properties', {})
        for key in list(value.keys()):
            if key in props:
                value[key] = _coerce_payload_to_schema(value[key], props[key])
        return value

    if field_type == 'array' and isinstance(value, list):
        items = schema.get('items', {})
        return [_coerce_payload_to_schema(item, items) for item in value]

    return value


def _parse_schema_path(path: str) -> list:
    """
    Разбирает путь поля схемы в сегменты.
    'port[].igmp_snooping' → ['port', '[]', 'igmp_snooping']
    """
    segments = []
    for part in path.split('.'):
        if not part:
            continue
        if part.endswith('[]'):
            name = part[:-2]
            if name:
                segments.append(name)
            segments.append('[]')
        else:
            segments.append(part)
    return segments


def _iter_schema_branches(schema: dict):
    if not isinstance(schema, dict):
        return
    for keyword in ('oneOf', 'anyOf', 'allOf'):
        for branch in schema.get(keyword, []):
            if isinstance(branch, dict) and branch.get('type') != 'null':
                yield branch


def _property_schema_in_node(schema: dict, prop_name: str) -> dict | None:
    """Ищет под-схему свойства в properties или внутри oneOf/anyOf/allOf."""
    if not isinstance(schema, dict):
        return None

    props = schema.get('properties', {})
    if prop_name in props:
        return props[prop_name]

    for branch in _iter_schema_branches(schema):
        found = _property_schema_in_node(branch, prop_name)
        if found is not None:
            return found

    return None


def _schema_at_path(schema: dict, path: str) -> dict | None:
    """Возвращает под-схему по dotted-пути (port[].autonegotiation.on)."""
    if not path:
        return schema

    current = schema
    for seg in _parse_schema_path(path):
        if seg == '[]':
            if not isinstance(current, dict) or _resolve_schema_type(current) != 'array':
                return None
            current = current.get('items', {})
            continue

        if not isinstance(current, dict):
            return None

        nxt = _property_schema_in_node(current, seg)
        if nxt is None:
            return None
        current = nxt

    return current if isinstance(current, dict) else None


def _find_oneof_branch_for_field(schema: dict, field_name: str, field_value=None) -> dict | None:
    """
    Находит ветку oneOf/anyOf для поля.
    Учитывает const/enum в ветке (discriminated unions: key_type=rsa|dsa|…).
    """
    candidates = []
    for branch in _iter_schema_branches(schema):
        props = branch.get('properties', {})
        if field_name in props or field_name in branch.get('required', []):
            candidates.append(branch)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    if field_value is not None:
        matching = [
            branch for branch in candidates
            if _branch_accepts_field_value(branch, field_name, field_value)
        ]
        if matching:
            return matching[0]

    return candidates[0]


def _branch_accepts_field_value(branch: dict, field_name: str, field_value) -> bool:
    """Совместимо ли значение с ограничениями ветки (const/enum / not.required)."""
    if not isinstance(branch, dict):
        return False
    not_block = branch.get("not")
    if isinstance(not_block, dict):
        forbidden = not_block.get("required") or []
        if field_name in forbidden:
            return False

    prop_schema = branch.get("properties", {}).get(field_name)
    if not isinstance(prop_schema, dict):
        return True
    if "const" in prop_schema:
        return prop_schema["const"] == field_value
    if "enum" in prop_schema:
        return field_value in prop_schema["enum"]
    for keyword in ("oneOf", "anyOf"):
        options = [
            opt for opt in prop_schema.get(keyword, [])
            if isinstance(opt, dict)
        ]
        if options and all("const" in opt for opt in options):
            return field_value in [opt["const"] for opt in options]
    return True


def _select_composition_branch(
    schema: dict,
    field_name: str | None = None,
    field_value=None,
) -> dict | None:
    """Ветка composition для сборки payload: по полю/значению или первая."""
    if not _schema_has_composition(schema):
        return None
    if field_name:
        branch = _find_oneof_branch_for_field(schema, field_name, field_value)
        if branch is not None:
            return branch
    return next(_iter_schema_branches(schema), None)


def _apply_branch_not_constraints(payload: dict, branch: dict | None) -> dict:
    """Убирает поля, запрещённые веткой (not.required)."""
    if not isinstance(payload, dict) or not isinstance(branch, dict):
        return payload
    not_block = branch.get("not")
    if not isinstance(not_block, dict):
        return payload
    for name in not_block.get("required") or []:
        payload.pop(name, None)
    return payload


def _schema_has_composition(schema: dict) -> bool:
    return any(k in schema for k in ('oneOf', 'anyOf', 'allOf'))


_NUMERIC_BOUNDS = ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum')
_STRING_BOUNDS = ('minLength', 'maxLength')
_VLAN_ID_MIN = 2
_VLAN_ID_MAX = 4094
_VID_RANGE_LIST_RE = re.compile(
    r"^([1-9][0-9]{0,3}([\-][1-9][0-9]{0,3})?)"
    r"([,][1-9][0-9]{0,3}([\-][1-9][0-9]{0,3})?)*$"
)


def _is_vid_range_list_schema(schema: dict) -> bool:
    if _resolve_schema_type(schema) != 'string':
        return False
    description = (schema.get('description') or '').lower()
    if 'vlan id range' in description or 'vlan id list' in description:
        return True
    pattern = schema.get('pattern', '')
    return (
        r"[1-9][0-9]{0,3}([\\-][1-9][0-9]{0,3})?" in pattern
        and r"[\,]" in pattern
    )


def _is_semantically_valid_vid_range_list(value: str) -> bool:
    """Regex OpenAPI не гарантирует low<=high в сегментах вида 45-68."""
    if not isinstance(value, str) or not _VID_RANGE_LIST_RE.fullmatch(value):
        return False
    try:
        for part in value.split(','):
            part = part.strip()
            if not part:
                return False
            if '-' in part:
                low_text, high_text = part.split('-', 1)
                low, high = int(low_text), int(high_text)
                if low > high or low < _VLAN_ID_MIN or high > _VLAN_ID_MAX:
                    return False
            else:
                number = int(part)
                if number < _VLAN_ID_MIN or number > _VLAN_ID_MAX:
                    return False
        return True
    except (TypeError, ValueError):
        return False


def _vid_range_list_test_values() -> list[str]:
    return [
        "2",
        "45-68",
        "100",
        "2-10",
        "4091",
        "2,45-68,4091",
        "10-20,100",
    ]


def _validate_instance_semantics(instance, schema: dict) -> tuple[bool, str]:
    if not isinstance(schema, dict):
        return True, ""

    if _is_vid_range_list_schema(schema):
        if isinstance(instance, str) and not _is_semantically_valid_vid_range_list(instance):
            return False, f"некорректный VID_RANGE_LIST: {instance!r}"
        return True, ""

    schema_type = _resolve_schema_type(schema)
    if schema_type == 'object' and isinstance(instance, dict):
        properties = schema.get('properties', {})
        for key, value in instance.items():
            if key in properties:
                ok, message = _validate_instance_semantics(value, properties[key])
                if not ok:
                    return False, f"{key}: {message}"
        return True, ""

    if schema_type == 'array' and isinstance(instance, list):
        items = schema.get('items', {})
        if isinstance(items, dict):
            for index, item in enumerate(instance):
                ok, message = _validate_instance_semantics(item, items)
                if not ok:
                    return False, f"[{index}]: {message}"
        return True, ""

    return True, ""
_ARRAY_BOUNDS = ('minItems', 'maxItems')


def _pinned_subschemas(schema: dict, constraint_keys: tuple) -> list:
    """
    Для каждого ограничения в схеме строит под-схему с зафиксированной границей.
    Значения генерируются JSF по этим под-схемам — без захардкоженных констант.
    """
    subschemas = []
    for key in constraint_keys:
        if key not in schema:
            continue
        bound = schema[key]
        sub = copy.deepcopy(schema)

        if key in _NUMERIC_BOUNDS:
            for k in _NUMERIC_BOUNDS:
                sub.pop(k, None)
            if key in ('minimum', 'maximum'):
                sub['minimum'] = bound
                sub['maximum'] = bound
            else:
                sub[key] = bound
        elif key in _STRING_BOUNDS:
            sub['minLength'] = bound
            sub['maxLength'] = bound
        elif key in _ARRAY_BOUNDS:
            sub['minItems'] = bound
            sub['maxItems'] = bound

        subschemas.append(sub)
    return subschemas


def _generate_values_from_schemas(schemas: list) -> list:
    values = []
    for sub in schemas:
        try:
            value = _jsf_generate(sub)
            if (
                _is_vid_range_list_schema(sub)
                and isinstance(value, str)
                and not _is_semantically_valid_vid_range_list(value)
            ):
                continue
            if value not in values:
                values.append(value)
        except Exception:
            pass
    return values


def _find_branch_for_property(schema: dict, prop_name: str) -> dict | None:
    """Ветка composition, в properties которой есть prop_name."""
    for branch in _iter_schema_branches(schema):
        if prop_name in branch.get('properties', {}):
            return branch
    return None


def _minimal_prop_value(prop_schema: dict):
    if _resolve_schema_type(prop_schema) == 'object':
        return _minimal_object_composed(prop_schema)
    if _resolve_schema_type(prop_schema) == 'array':
        return _generate_array_value(prop_schema)
    concrete = _first_concrete_value(prop_schema)
    if concrete is not None:
        return concrete
    try:
        return _jsf_generate(prop_schema)
    except Exception:
        return None


def _minimal_object_composed(schema: dict, exclude: str | None = None) -> dict:
    """
    Минимальный валидный object: required из properties + одна ветка oneOf/anyOf/allOf.
    Поддерживает схемы вида {required: [acl_name], properties: {acl_name}, oneOf: [...]}.
    Для discriminated oneOf пинит const discriminator из ветки, а не JSF-мусор.
    """
    if not isinstance(schema, dict) or _resolve_schema_type(schema) != 'object':
        return {}

    props = schema.get('properties', {})
    result = {}

    if not _schema_has_composition(schema):
        for req in schema.get('required', []):
            if req == exclude:
                continue
            if req in props:
                result[req] = _minimal_prop_value(props[req])
        return _coerce_payload_to_schema(result, schema)

    branch = None
    if exclude and exclude not in props:
        branch = _find_oneof_branch_for_field(schema, exclude, None)
    if branch is None:
        branch = next(_iter_schema_branches(schema), None)
    if branch is None:
        for req in schema.get('required', []):
            if req == exclude:
                continue
            if req in props:
                result[req] = _minimal_prop_value(props[req])
        return _coerce_payload_to_schema(result, schema)

    branch_props = branch.get('properties', {})
    for req in schema.get('required', []):
        if req == exclude:
            continue
        if req in branch_props:
            result[req] = _minimal_prop_value(branch_props[req])
        elif req in props:
            result[req] = _minimal_prop_value(props[req])

    branch_only = len(branch_props) == 1 and not props

    if branch_only:
        wrap_key = next(iter(branch_props))
        if wrap_key == exclude:
            inner = {}
        else:
            inner = _minimal_object_composed(branch_props[wrap_key], exclude=None)
        result[wrap_key] = inner
        return _coerce_payload_to_schema(result, schema)

    branch_obj = _minimal_object_composed(branch, exclude=exclude if exclude not in props else None)
    for key, val in branch_obj.items():
        branch_prop = branch_props.get(key, {})
        if isinstance(branch_prop, dict) and (
            'const' in branch_prop or 'enum' in branch_prop
        ):
            result[key] = val
        else:
            result.setdefault(key, val)
    result = _apply_branch_not_constraints(result, branch)
    return _coerce_payload_to_schema(result, schema)


def _minimal_object(schema: dict, exclude: str | None = None) -> dict:
    """Минимальный object по схеме: все required-поля, кроме exclude."""
    return _minimal_object_composed(schema, exclude=exclude)


def _assign_object_value(prop_schema: dict, value):
    """Подставляет value в object-поле, дополняя обязательные поля из схемы."""
    if value is None:
        return None
    if isinstance(value, dict) and _resolve_schema_type(prop_schema) == 'object':
        base = _minimal_object_composed(prop_schema)
        base.update(value)
        return _coerce_payload_to_schema(base, prop_schema)
    return value


def _build_field_container(schema: dict, field: str, value) -> dict:
    """
    Объект на уровне schema с field=value и обязательными соседями.
    Для root oneOf выбирает совместимую ветку и пинит discriminator (const/enum),
    чтобы JSF не подставлял мусор в key_type и т.п.
    """
    props = schema.get('properties', {})
    result = {}
    branch = _select_composition_branch(schema, field, value)

    for req in schema.get('required', []):
        if req == field:
            continue
        if branch and req in branch.get('properties', {}):
            result[req] = _minimal_prop_value(branch['properties'][req])
        elif req in props:
            result[req] = _minimal_prop_value(props[req])

    if field in props:
        result[field] = _assign_object_value(props[field], value)
        if branch:
            branch_obj = _minimal_object_composed(branch)
            for key, val in branch_obj.items():
                if key == field:
                    continue
                branch_prop = branch.get('properties', {}).get(key, {})
                # Discriminator / ограниченные поля ветки перекрывают JSF из required
                if isinstance(branch_prop, dict) and (
                    'const' in branch_prop or 'enum' in branch_prop
                ):
                    result[key] = val
                else:
                    result.setdefault(key, val)
            result = _apply_branch_not_constraints(result, branch)
        return _coerce_payload_to_schema(result, schema)

    if branch:
        branch_obj = _minimal_object_composed(branch, exclude=field)
        branch_obj[field] = value
        result.update(branch_obj)
        result = _apply_branch_not_constraints(result, branch)
        return _coerce_payload_to_schema(result, schema)

    result[field] = value
    return _coerce_payload_to_schema(result, schema)


def _attach_nested(parent_schema: dict, prop_name: str, nested_value) -> dict:
    """Вкладывает nested_value в parent_schema по prop_name, заполняя соседей."""
    if prop_name in parent_schema.get('properties', {}):
        obj = _minimal_object_composed(parent_schema, exclude=prop_name)
        prop_schema = parent_schema['properties'][prop_name]
        obj[prop_name] = _assign_object_value(prop_schema, nested_value)
        return obj

    if _find_branch_for_property(parent_schema, prop_name):
        return {prop_name: nested_value}

    return {prop_name: nested_value}


def _build_payload_for_path(root_schema: dict, path: str, value) -> dict:
    """Собирает пейлоад с нуля по пути поля, выбирая нужные ветки composition."""
    segments = _parse_schema_path(path)
    if not segments or segments[-1] == '[]':
        return {}

    field_schema = _schema_at_path(root_schema, path)
    if field_schema:
        value = _coerce_payload_to_schema(value, field_schema)

    if len(segments) == 1:
        return _build_field_container(root_schema, segments[0], value)

    if len(segments) >= 2 and segments[-2] == '[]':
        array_field = segments[0]
        item_schema = _schema_at_path(root_schema, _join_schema_path(segments[:-1])) or {}
        item = _build_field_container(item_schema, segments[-1], value)
        return {array_field: [item]}

    def descend(schema: dict, segs: list):
        if segs[0] == '[]':
            item_schema = schema.get('items', {}) if _resolve_schema_type(schema) == 'array' else schema
            return descend(item_schema, segs[1:])

        if len(segs) == 1:
            return _build_field_container(schema, segs[0], value)

        prop = segs[0]
        if len(segs) > 1 and segs[1] == '[]':
            array_schema = _property_schema_in_node(schema, prop) or {}
            item_schema = array_schema.get('items', {})
            nested = descend(item_schema, segs[2:])
            return {prop: [nested]}

        child_schema = _property_schema_in_node(schema, prop) or {}
        nested = descend(child_schema, segs[1:])
        return _attach_nested(schema, prop, nested)

    return descend(root_schema, segments)


def collect_test_values(field_schema: dict) -> list:
    """
    Набор значений для тестирования поля — только из ограничений схемы.
    enum → все варианты; boolean → true/false; границы → под-схемы + JSF.
    nullable → null; object без required → {} и заполненный вариант.
    """
    schema = copy.deepcopy(field_schema)

    # const раньше composition: discriminator (key_type: const "rsa")
    if 'const' in schema:
        return _append_null_if_allowed(schema, [schema['const']])

    if _schema_has_composition(schema):
        constrained: list = []
        unconstrained: list = []
        has_null = False
        for keyword in ('oneOf', 'anyOf'):
            for branch in schema.get(keyword, []):
                if isinstance(branch, dict) and branch.get('type') == 'null':
                    has_null = True
        for branch in _iter_schema_branches(schema):
            bucket = (
                constrained
                if isinstance(branch, dict) and (
                    'const' in branch or 'enum' in branch
                )
                else unconstrained
            )
            for candidate in collect_test_values(branch):
                if candidate not in bucket:
                    bucket.append(candidate)
        # Рядом с const/enum discriminator'ами не тащим JSF со «голого» string
        values = constrained if constrained else unconstrained
        if has_null and None not in values:
            values.append(None)
        if values:
            return _append_null_if_allowed(schema, values)

    field_type = _resolve_schema_type(schema)

    if schema.get('enum'):
        return _append_null_if_allowed(schema, list(dict.fromkeys(schema['enum'])))

    if field_type == 'boolean':
        if 'const' in schema:
            return _append_null_if_allowed(schema, [schema['const']])
        return _append_null_if_allowed(schema, [True, False])

    if field_type in ('integer', 'number'):
        subs = _pinned_subschemas(schema, _NUMERIC_BOUNDS)
        subs.append(schema)
        return _append_null_if_allowed(schema, _generate_values_from_schemas(subs))

    if field_type == 'string':
        if _is_vid_range_list_schema(schema):
            values = list(_vid_range_list_test_values())
            for _ in range(8):
                try:
                    candidate = _jsf_generate(schema)
                    if (
                        isinstance(candidate, str)
                        and _is_semantically_valid_vid_range_list(candidate)
                        and candidate not in values
                    ):
                        values.append(candidate)
                except Exception:
                    pass
            return _append_null_if_allowed(schema, values)
        subs = _pinned_subschemas(schema, _STRING_BOUNDS)
        subs.append(schema)
        return _append_null_if_allowed(schema, _generate_values_from_schemas(subs))

    if field_type == 'object':
        values = []
        if _is_valid_for_schema({}, schema):
            values.append({})
        filled = _filled_object(schema)
        if filled is not None and filled not in values:
            values.append(filled)
        if 'oneOf' in schema:
            for branch in schema['oneOf']:
                if isinstance(branch, dict) and branch.get('type') == 'null':
                    continue
                try:
                    candidate = _jsf_generate(branch)
                    if candidate not in values:
                        values.append(candidate)
                except Exception:
                    pass
        if schema.get('required'):
            minimal = _minimal_object_composed(schema)
            if minimal not in values:
                values.append(minimal)
        if values:
            return _append_null_if_allowed(schema, values)
        return _append_null_if_allowed(schema, [{}])

    if field_type == 'array':
        subs = _pinned_subschemas(schema, _ARRAY_BOUNDS)
        if subs:
            subs.append(schema)
            return _append_null_if_allowed(schema, _generate_values_from_schemas(subs))
        if schema.get('prefixItems'):
            sample = _generate_prefix_items_array(schema)
            return _append_null_if_allowed(schema, [sample])
        items = schema.get('items')
        if items:
            if _resolve_schema_type(items) == 'object':
                return _append_null_if_allowed(schema, [[_minimal_object_composed(items)]])
            try:
                return _append_null_if_allowed(schema, [[_jsf_generate(items)]])
            except Exception:
                pass
        return _append_null_if_allowed(schema, [])

    return _append_null_if_allowed(schema, _generate_values_from_schemas([schema]))


def _join_schema_path(segments: list) -> str:
    out = ""
    for seg in segments:
        if seg == "[]":
            out += "[]"
        elif out:
            out += "." + seg
        else:
            out = seg
    return out


def _assign_at_path(obj: dict, path_segments: list, value):
    if not path_segments:
        return

    current = obj
    idx = 0
    while idx < len(path_segments) - 1:
        seg = path_segments[idx]
        if seg == "[]":
            idx += 1
            continue
        if idx + 1 < len(path_segments) and path_segments[idx + 1] == "[]":
            if seg not in current or not isinstance(current[seg], list) or not current[seg]:
                current[seg] = [{}]
            current = current[seg][0]
            idx += 2
        else:
            if seg not in current or not isinstance(current[seg], dict):
                current[seg] = {}
            current = current[seg]
            idx += 1

    last = path_segments[-1]
    if last != "[]":
        current[last] = value


def set_field_test_value(obj: dict, root_schema: dict, path: str, value):
    """Тест одного поля: пересборка валидного пейлоада по пути в схеме."""
    built = _build_payload_for_path(root_schema, path, value)
    obj.clear()
    obj.update(built)


def set_nested_value(obj: dict, path: str, value, root_schema: dict | None = None):
    if root_schema:
        set_field_test_value(obj, root_schema, path, value)
        return

    segments = _parse_schema_path(path)
    if not segments:
        return

    current = obj
    idx = 0
    while idx < len(segments) - 1:
        seg = segments[idx]
        if seg == '[]':
            idx += 1
            continue
        if idx + 1 < len(segments) and segments[idx + 1] == '[]':
            if seg not in current or not isinstance(current[seg], list) or not current[seg]:
                current[seg] = [{}]
            current = current[seg][0]
            idx += 2
        else:
            if seg not in current or not isinstance(current[seg], dict):
                current[seg] = {}
            current = current[seg]
            idx += 1

    last = segments[-1]
    if last != '[]':
        current[last] = value


def _payload_fingerprint(payload) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _validate_payload(payload, schema: dict) -> tuple[bool, str]:
    try:
        import jsonschema
        jsonschema.validate(instance=payload, schema=_expand_nullable_schema(schema))
    except ImportError:
        pass
    except Exception as e:
        message = str(e).split("\n", 1)[0]
        return False, message

    ok, message = _validate_instance_semantics(payload, schema)
    if not ok:
        return False, message
    return True, ""


def _schema_allows_explicit_null(prop_schema: dict) -> bool:
    """Поле допускает JSON null (oneOf/type union) — устройство может требовать ключ явно."""
    if _schema_allows_null(prop_schema):
        return True
    for keyword in ("oneOf", "anyOf"):
        for branch in prop_schema.get(keyword, []):
            if isinstance(branch, dict) and branch.get("type") == "null":
                return True
    return False


def _enrich_optional_null_fields(payload: dict, schema: dict) -> dict:
    """Добавляет optional-поля со значением null, если схема это допускает."""
    if _resolve_schema_type(schema) != "object":
        return payload
    result = copy.deepcopy(payload)
    for name, prop_schema in schema.get("properties", {}).items():
        if name in result or name in schema.get("required", []):
            continue
        if _schema_allows_explicit_null(prop_schema):
            result[name] = None
    return result


def build_minimal_payload(schema: dict) -> dict:
    """Пейлоад только с required-полями с учётом composition."""
    if _resolve_schema_type(schema) != 'object':
        return _coerce_payload_to_schema(_jsf_generate(schema), schema)
    minimal = _minimal_object_composed(schema)
    return _enrich_optional_null_fields(minimal, schema)


@dataclass
class PayloadCoverage:
    payload: dict
    coverage_keys: list[str]


def _format_coverage_key(path: str, value) -> str:
    return f"{path}={json.dumps(value, sort_keys=True, ensure_ascii=False)}"


def _coverage_sort_key(coverage_keys: list[str]) -> str:
    if not coverage_keys:
        return "\uffff"
    return "|".join(sorted(coverage_keys))


def _merge_coverage_records(records: dict[str, PayloadCoverage], record: PayloadCoverage) -> None:
    fingerprint = _payload_fingerprint(record.payload)
    if fingerprint not in records:
        records[fingerprint] = PayloadCoverage(
            copy.deepcopy(record.payload),
            list(record.coverage_keys),
        )
        return
    existing = records[fingerprint]
    for key in record.coverage_keys:
        if key not in existing.coverage_keys:
            existing.coverage_keys.append(key)


def generate_value_coverage_payloads(schema: dict, *, compact: bool = False) -> list[PayloadCoverage]:
    """
    Генерирует пейлоады для покрытия enum/boolean/границ.
    Для action oneOf — покрытие по веткам rule type без дубля add/delete.
    compact=True — большие enum (>10) сворачивает до 3 значений.
    """
    field_schemas = ResolveScheme.extract_field_schemas(schema)
    path_values = _filter_coverage_paths(
        {
            path: collect_test_values(field_schema)
            for path, field_schema in field_schemas.items()
            if not path.endswith("[]")
        },
        field_schemas,
    )
    all_paths = set(path_values)

    slices = _discover_coverage_slices(schema)
    if not slices:
        return _generate_flat_coverage(schema, path_values, field_schemas, compact)

    logger.info(
        f"Покрытие по веткам: {len(slices)} slice(s)"
        + (" (compact enum)" if compact else "")
    )

    payloads: list[PayloadCoverage] = []
    seen_payloads: set = set()
    covered_targets: set = set()

    for slc in slices:
        slice_payloads = _generate_slice_coverage(
            schema, slc, path_values, field_schemas,
            seen_payloads, covered_targets, compact,
        )
        payloads.extend(slice_payloads)
        logger.debug(f"Slice {slc.label}: +{len(slice_payloads)} пейлоадов")

    expected_targets: set = set()
    for path, values in path_values.items():
        if _is_mirror_delete_path(path, all_paths):
            continue
        limited = _limit_values_for_compact(values, field_schemas[path], compact)
        for value in limited:
            expected_targets.add(_coverage_target_key(path, value))

    for slc in slices:
        if not slc.full_coverage and slc.rule_key:
            key = _coverage_target_key(f"__slice__.{slc.label}", "minimal")
            expected_targets.add(key)

    reportable_covered = {
        k for k in covered_targets
        if not k[0].startswith("__slice__")
        and not _is_mirror_delete_path(k[0], all_paths)
    }
    missing_targets = expected_targets - reportable_covered
    if missing_targets:
        real_missing = {t for t in missing_targets if not t[0].startswith("__slice__")}
        if real_missing:
            logger.warning(
                f"Не покрыто целевых значений: {len(real_missing)} "
                f"из {len(expected_targets)}"
            )
            for path, value_json in sorted(real_missing)[:10]:
                logger.warning(f"  • {path} = {value_json}")
            if len(real_missing) > 10:
                logger.warning(f"  … и ещё {len(real_missing) - 10}")

    for slc in slices:
        if not slc.full_coverage and slc.rule_key:
            covered_targets.add(_coverage_target_key(f"__slice__.{slc.label}", "minimal"))

    logger.info(
        f"Покрытие значений: {len(payloads)} пейлоадов, "
        f"целей {len(reportable_covered)}/{len(expected_targets)}"
    )
    return payloads


def dedupe_payloads(records: list[PayloadCoverage]) -> list[PayloadCoverage]:
    """Удаляет дубликаты пейлоадов, объединяя coverage_keys."""
    merged: dict[str, PayloadCoverage] = {}
    for record in records:
        _merge_coverage_records(merged, record)
    return list(merged.values())


# =============================================================================
# ИЗВЛЕЧЕНИЕ ВСЕХ ПОЛЕЙ ИЗ СХЕМЫ
# =============================================================================
def extract_all_fields(schema):
    """
    Извлекает все поля из схемы.
    Возвращает множество всех полей.
    Например, ({"name": "test", "age": 20}) -> {"name", "age"}
    """
    fields = set() # Создаем пустое множество для результата
    if not isinstance(schema, dict):
        return fields
    if "properties" in schema and isinstance(schema["properties"], dict):
        for prop_name, prop_schema in schema["properties"].items():
            fields.add(prop_name)
            fields.update(extract_all_fields(prop_schema))
    if "items" in schema and isinstance(schema["items"], dict):
        fields.update(extract_all_fields(schema["items"]))
    for keyword in ["anyOf", "oneOf", "allOf"]:
        if keyword in schema and isinstance(schema[keyword], list):
            for item in schema[keyword]:
                if isinstance(item, dict):
                    fields.update(extract_all_fields(item))
    return fields


# =============================================================================
# ПОКРЫТИЕ ПО ВЕТКАМ oneOf (action × rule type, без дубля add/delete)
# =============================================================================
_ACTION_VERBS = ("add", "delete", "modify", "clear")


@dataclass(frozen=True)
class CoverageSlice:
    label: str
    verb: str
    rule_key: str | None
    full_coverage: bool


def _discover_rule_branch_keys(rule_schema: dict) -> list[str]:
    """Ключи взаимоисключающих rule-веток (dpi, protocol, …)."""
    if not isinstance(rule_schema, dict):
        return []
    keys: set[str] = set()
    for branch in _iter_schema_branches(rule_schema):
        for item in branch.get("anyOf", []):
            if not isinstance(item, dict):
                continue
            req = item.get("required") or []
            if req:
                keys.add(req[0])
        if branch.get("properties"):
            keys.update(branch["properties"].keys())
        for req in branch.get("required", []):
            keys.add(req)
    return sorted(k for k in keys if k not in ("not", "rule"))


def _discover_coverage_slices(schema: dict) -> list[CoverageSlice]:
    action_schema = schema.get("properties", {}).get("action")
    if not isinstance(action_schema, dict):
        return []
    action_branches = list(_iter_schema_branches(action_schema))
    if not action_branches:
        return []

    slices: list[CoverageSlice] = []
    seen: set[str] = set()

    for action_branch in action_branches:
        verb = action_branch.get("title")
        if not verb or verb not in action_branch.get("properties", {}):
            verb = next(
                (v for v in _ACTION_VERBS if v in action_branch.get("properties", {})),
                None,
            )
        if not verb:
            continue

        verb_obj = action_branch["properties"][verb]
        rule_schema = verb_obj.get("properties", {}).get("rule")
        rule_keys = _discover_rule_branch_keys(rule_schema) if rule_schema else []

        if not rule_keys:
            label = f"action.{verb}"
            if label not in seen:
                seen.add(label)
                slices.append(CoverageSlice(label, verb, None, True))
            continue

        shared_label = f"action.{verb}/_shared"
        if shared_label not in seen:
            seen.add(shared_label)
            slices.append(CoverageSlice(shared_label, verb, None, verb == "add"))

        for rule_key in rule_keys:
            label = f"action.{verb}/{rule_key}"
            if label in seen:
                continue
            seen.add(label)
            slices.append(CoverageSlice(label, verb, rule_key, verb == "add"))

    return slices


def build_coverage_expectations(schema: dict, *, compact: bool = False) -> set[str]:
    """Ожидаемые ключи покрытия значений для эндпоинта."""
    field_schemas = ResolveScheme.extract_field_schemas(schema)
    path_values = _filter_coverage_paths(
        {
            path: collect_test_values(field_schema)
            for path, field_schema in field_schemas.items()
            if not path.endswith("[]")
        },
        field_schemas,
    )
    all_paths = set(path_values)
    expected: set[str] = set()
    for path, values in path_values.items():
        if _is_mirror_delete_path(path, all_paths):
            continue
        for value in _limit_values_for_compact(values, field_schemas[path], compact):
            expected.add(_format_coverage_key(path, value))
    for slc in _discover_coverage_slices(schema):
        if not slc.full_coverage and slc.rule_key:
            expected.add(_format_coverage_key(f"__slice__.{slc.label}", "minimal"))
    return expected


def _path_belongs_to_rule_branch(path: str, rule_key: str) -> bool:
    token = f".rule.{rule_key}"
    return token + "." in path or path.endswith(token)


def _mirror_add_path(delete_path: str) -> str | None:
    if not delete_path.startswith("action.delete."):
        return None
    return "action.add." + delete_path[len("action.delete.") :]


def _is_mirror_delete_path(path: str, all_paths: set[str]) -> bool:
    add_path = _mirror_add_path(path)
    return bool(add_path and add_path in all_paths)


def _path_in_slice(path: str, slc: CoverageSlice) -> bool:
    prefix = f"action.{slc.verb}"
    if not path.startswith(prefix):
        return False

    if slc.rule_key is None:
        if slc.label.endswith("/_shared"):
            return ".rule." not in path
        return True

    if _path_belongs_to_rule_branch(path, slc.rule_key):
        return True
    if re.match(rf"^action\.{slc.verb}\.(acl_name|index)$", path):
        return False
    if path in (f"action.{slc.verb}", f"action.{slc.verb}.rule"):
        return slc.full_coverage
    return False


def _slice_anchor_path(
    slc: CoverageSlice, field_schemas: dict, path_values: dict,
) -> str | None:
    if slc.rule_key:
        candidates = sorted(
            p for p in path_values
            if _path_in_slice(p, slc) and _path_belongs_to_rule_branch(p, slc.rule_key)
        )
        return candidates[0] if candidates else None
    for p in sorted(path_values):
        if _path_in_slice(p, slc):
            return p
    return None


def _filter_coverage_paths(path_values: dict, field_schemas: dict) -> dict:
    """Убирает родительские пути, если покрываются дочерними (action → action.add.*)."""
    all_paths = set(path_values)
    filtered = {}
    for path, values in path_values.items():
        prefix = path + "."
        if any(p.startswith(prefix) for p in all_paths):
            continue
        if path.endswith(".rule") and any(
            p.startswith(prefix) for p in all_paths
        ):
            continue
        cleaned = [
            v for v in values
            if not (isinstance(v, dict) and v == {} and not _is_valid_for_schema({}, field_schemas[path]))
        ]
        if cleaned:
            filtered[path] = cleaned
    return filtered


def _minimal_payload_for_slice(
    schema: dict, slc: CoverageSlice, field_schemas: dict, path_values: dict,
) -> dict:
    if slc.label.endswith("/_shared"):
        for path in sorted(path_values):
            match = re.match(rf"^action\.{slc.verb}\.rule\.([^.]+)\.", path)
            if match:
                sub = CoverageSlice(
                    f"{slc.verb}/{match.group(1)}", slc.verb, match.group(1), True,
                )
                return _minimal_payload_for_slice(schema, sub, field_schemas, path_values)

    anchor = _slice_anchor_path(slc, field_schemas, path_values)
    if anchor and path_values.get(anchor):
        return _build_payload_for_path(schema, anchor, path_values[anchor][0])
    if slc.rule_key:
        leaf = next(
            (
                p for p in sorted(path_values)
                if _path_belongs_to_rule_branch(p, slc.rule_key)
                and p.startswith(f"action.{slc.verb}")
            ),
            None,
        )
        if leaf and path_values.get(leaf):
            return _build_payload_for_path(schema, leaf, path_values[leaf][0])
    return build_minimal_payload(schema)


def _limit_values_for_compact(values: list, field_schema: dict, compact: bool) -> list:
    if not compact or len(values) <= 10:
        return values
    if field_schema.get("enum") and len(values) > 10:
        mid = values[len(values) // 2]
        return [values[0], mid, values[-1]]
    return values


def _coverage_target_key(path: str, value) -> tuple:
    return (path, json.dumps(value, sort_keys=True, ensure_ascii=False))


def _mark_covered(covered: set, path: str, value, mirror_paths: bool = True) -> None:
    covered.add(_coverage_target_key(path, value))
    if mirror_paths:
        if path.startswith("action.add."):
            mirror = "action.delete." + path[len("action.add.") :]
            covered.add(_coverage_target_key(mirror, value))
        elif path.startswith("action.delete."):
            mirror = _mirror_add_path(path)
            if mirror:
                covered.add(_coverage_target_key(mirror, value))


def _generate_slice_coverage(
    schema: dict,
    slc: CoverageSlice,
    path_values: dict,
    field_schemas: dict,
    seen_payloads: set,
    covered_targets: set,
    compact: bool,
) -> list[PayloadCoverage]:
    payloads: list[PayloadCoverage] = []
    records: dict[str, PayloadCoverage] = {}

    def _add(
        payload,
        label="",
        coverage_keys: list[str] | None = None,
        path=None,
        value=None,
        mirror=True,
    ):
        payload = _coerce_payload_to_schema(payload, schema)
        keys = list(coverage_keys or [])
        if path is not None:
            keys.append(_format_coverage_key(path, value))
        fingerprint = _payload_fingerprint(payload)
        if fingerprint in seen_payloads:
            if path is not None:
                _mark_covered(covered_targets, path, value, mirror_paths=mirror)
            if keys:
                _merge_coverage_records(records, PayloadCoverage(payload, keys))
            return True
        valid, reason = _validate_payload(payload, schema)
        if not valid:
            logger.debug(f"Пропуск [{slc.label}] ({label}): {reason}")
            return False
        seen_payloads.add(fingerprint)
        _merge_coverage_records(records, PayloadCoverage(copy.deepcopy(payload), keys))
        if path is not None:
            _mark_covered(covered_targets, path, value, mirror_paths=mirror)
        return True

    slice_paths = {
        p: _limit_values_for_compact(v, field_schemas[p], compact)
        for p, v in path_values.items()
        if _path_in_slice(p, slc)
    }

    slice_minimal_key = f"__slice_minimal__:{slc.label}"

    if not slc.full_coverage:
        minimal = _minimal_payload_for_slice(schema, slc, field_schemas, path_values)
        _add(
            minimal,
            f"minimal/{slc.label}",
            coverage_keys=[slice_minimal_key],
            mirror=False,
        )
        return list(records.values())

    minimal = _minimal_payload_for_slice(schema, slc, field_schemas, path_values)
    _add(minimal, f"minimal/{slc.label}", coverage_keys=[slice_minimal_key])

    for path, test_values in sorted(slice_paths.items()):
        for value in test_values:
            variant = copy.deepcopy(minimal)
            set_field_test_value(variant, schema, path, value)
            _add(variant, f"{path}={value!r}", path=path, value=value)

    payloads.extend(records.values())
    return payloads


def _generate_flat_coverage(
    schema: dict, path_values: dict, field_schemas: dict, compact: bool,
) -> list[PayloadCoverage]:
    """Legacy: одно flat-покрытие для простых схем без action oneOf."""
    minimal_base = build_minimal_payload(schema)
    records: dict[str, PayloadCoverage] = {}
    seen: set = set()
    covered_targets: set = set()
    skipped = 0

    def _add(
        payload,
        label="",
        coverage_keys: list[str] | None = None,
        path=None,
        value=None,
    ):
        nonlocal skipped
        payload = _coerce_payload_to_schema(payload, schema)
        keys = list(coverage_keys or [])
        if path is not None:
            keys.append(_format_coverage_key(path, value))
        fingerprint = _payload_fingerprint(payload)
        if fingerprint in seen:
            if path is not None:
                covered_targets.add(_coverage_target_key(path, value))
            if keys:
                _merge_coverage_records(records, PayloadCoverage(payload, keys))
            return True
        valid, reason = _validate_payload(payload, schema)
        if not valid:
            skipped += 1
            logger.debug(f"Пропуск ({label}): {reason}")
            return False
        seen.add(fingerprint)
        _merge_coverage_records(records, PayloadCoverage(copy.deepcopy(payload), keys))
        if path is not None:
            covered_targets.add(_coverage_target_key(path, value))
        return True

    _add(minimal_base, "minimal", coverage_keys=["__minimal__"])

    for path, test_values in sorted(path_values.items()):
        limited = _limit_values_for_compact(test_values, field_schemas[path], compact)
        for value in limited:
            variant = copy.deepcopy(minimal_base)
            set_field_test_value(variant, schema, path, value)
            _add(variant, f"{path}={value!r}", path=path, value=value)

    expected_targets = {
        _coverage_target_key(path, value)
        for path, values in path_values.items()
        for value in _limit_values_for_compact(values, field_schemas[path], compact)
    }
    missing_targets = expected_targets - covered_targets
    if missing_targets:
        logger.warning(
            f"Не покрыто целевых значений: {len(missing_targets)} "
            f"из {len(expected_targets)}"
        )
    logger.info(
        f"Покрытие значений: {len(records)} пейлоадов, "
        f"целей {len(covered_targets)}/{len(expected_targets)}"
        + (f", пропущено попыток: {skipped}" if skipped else "")
    )
    return list(records.values())


# =============================================================================
# СБОР КЛЮЧЕЙ ИЗ ПЕЙЛОАДА
# =============================================================================
def get_payload_fields(payload):
    """
    Извлекает все поля из пейлоада.
    Возвращает множество всех полей.
    Например, ({"name": "test", "age": 20}) -> {"name", "age"}
    """
    keys = set() # Создаем пустое множество для результата
    if isinstance(payload, dict):
        for k, v in payload.items(): # Для каждого ключа и значения
            keys.add(k) # Добавляем ключ в множество
            keys.update(get_payload_fields(v)) # Обновляем множество ключей
    elif isinstance(payload, list): # Если пейлоад список
        for item in payload:
            keys.update(get_payload_fields(item)) # Обновляем множество ключей
    return keys # Возвращаем множество ключей


# =============================================================================
# ПОИСК ЗАВИСИМОСТЕЙ В ПЕЙЛОАДЕ
# =============================================================================
def scan_payload_for_dependencies(payload, dep_map, path=""):
    """
    Ищет зависимости в пейлоаде.
    Возвращает словарь всех зависимостей.
    Это работает так: если в пейлоаде есть ключ, который есть в dep_map, то добавляем его в found.
    Затем рекурсивно ищем зависимости в значениях. И так для всех значений.
    """
    found = {} # Создаем пустой словарь для результата
    if isinstance(payload, dict):
        for k, v in payload.items(): # Для каждого ключа и значения
            new_path = f"{path}.{k}" if path else k
            if k in dep_map: # Если ключ есть в dep_map
                found[new_path] = {"field": k, "value": v, "config": dep_map[k]}
                logger.debug(f"Найдена зависимость: {new_path} → {k}") # Логируем найденную зависимость
            found.update(scan_payload_for_dependencies(v, dep_map, new_path)) # Обновляем словарь зависимостей
    elif isinstance(payload, list): # Если пейлоад список
        for i, item in enumerate(payload): # Для каждого элемента
            found.update(scan_payload_for_dependencies(item, dep_map, f"{path}[{i}]")) # Обновляем словарь зависимостей
    return found # Возвращаем словарь зависимостей


# =============================================================================
# ACTION-AWARE LIFECYCLE (main_test action → setup/teardown)
# =============================================================================
def _extract_main_action(payload: dict) -> tuple[str | None, dict]:
    """
    Извлекает глагол action из main_test payload.
    Поддерживает: "action": "add" и "action": {"add": {...}}.
    """
    if not isinstance(payload, dict):
        return None, {}

    action = payload.get("action")
    if isinstance(action, str):
        return action.lower(), {
            k: v for k, v in payload.items()
            if k != "action" and not isinstance(v, (dict, list))
        }

    if isinstance(action, dict):
        for verb in _ACTION_VERBS:
            if verb not in action:
                continue
            data = action[verb]
            if isinstance(data, dict):
                return verb, data
            if data is not None and not isinstance(data, list):
                return verb, {verb: data}
            return verb, {}

    return None, {}


def _collect_bind_vars(obj, into: dict | None = None) -> dict:
    """Собирает скалярные поля из action-блока для {{placeholder}}."""
    into = into if into is not None else {}
    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(val, dict):
                _collect_bind_vars(val, into)
            elif isinstance(val, list):
                for item in val:
                    _collect_bind_vars(item, into)
            elif val is not None:
                into[key] = val
    return into


def _collect_endpoint_bind_vars(
    payload: dict,
    action_data: dict,
    bind_fields: list[str],
) -> dict:
    """
    Плейсхолдеры для endpoint_rules: поля из action.* + top-level (ifnames, …).
    """
    bind_vars = _collect_bind_vars(action_data) if action_data else {}
    if not bind_fields:
        return bind_vars
    for key in bind_fields:
        if key in bind_vars or key not in payload:
            continue
        val = payload[key]
        if isinstance(val, dict):
            _collect_bind_vars(val, bind_vars)
        elif val is not None:
            bind_vars[key] = val
    return {k: bind_vars[k] for k in bind_fields if k in bind_vars}


def _fill_bind_fields_from_mock_data(
    bind_vars: dict,
    bind_fields: list[str],
    mock_by_field: dict[str, list],
) -> dict:
    """Дополняет bind_fields значениями из mock_data.by_field, если поля нет в payload."""
    if not bind_fields or not mock_by_field:
        return bind_vars
    for key in bind_fields:
        if key in bind_vars:
            continue
        values = mock_by_field.get(key)
        if values:
            bind_vars[key] = values[0]
            logger.debug(
                f"bind_fields: {key}={values[0]!r} из mock_data.by_field",
            )
    return bind_vars


def _inject_synthetic_field_dependencies(
    deps: dict,
    variables: dict,
    *,
    target_endpoint: str,
    main_action: str | None,
    dep_map: dict,
    mock_by_field: dict[str, list],
    synthetic_fields: dict[str, tuple[str, ...]] | None = None,
) -> None:
    """
    Добавляет field_mappings для полей bind_fields, отсутствующих в main_test payload.
    Например, delete/modify filter_ipv4 без acl_name → lifecycle ACL из field_mappings.
    """
    if not main_action or not mock_by_field:
        return
    if main_action not in ("delete", "modify"):
        return

    defaults = synthetic_fields or {}
    endpoint_key = target_endpoint.rstrip("/")
    fields = defaults.get(endpoint_key, ())
    if not fields:
        return

    present_fields = {info["field"] for info in deps.values()}
    for field in fields:
        if field in present_fields or field not in dep_map:
            continue
        values = mock_by_field.get(field)
        if not values:
            continue
        value = values[0]
        variables[field] = value
        deps[f"_synthetic.{field}"] = {
            "field": field,
            "value": value,
            "config": dep_map[field],
        }
        logger.debug(
            f"Синтетическая зависимость {field}={value!r} для {main_action} "
            f"на {endpoint_key}",
        )


def _lifecycle_endpoint_from_config(config: dict, phase: str) -> str | None:
    """Endpoint из setup/teardown или legacy create/delete."""
    alt = "create" if phase == "setup" else "delete"
    step = config.get(phase) or config.get(alt)
    if isinstance(step, str):
        return step
    if isinstance(step, dict):
        return step.get("endpoint")
    if isinstance(step, list) and step:
        first = step[0]
        return first.get("endpoint") if isinstance(first, dict) else None
    return None


def _is_prerequisite_field(config: dict, target_endpoint: str) -> bool:
    """Зависимость на другом эндпоинте (vrf/acl для filter), не ресурс main_test."""
    target = target_endpoint.rstrip("/")
    setup_ep = _lifecycle_endpoint_from_config(config, "setup")
    teardown_ep = _lifecycle_endpoint_from_config(config, "teardown")
    for ep in (setup_ep, teardown_ep):
        if ep and ep.rstrip("/") != target:
            return True
    return False


def _target_matches_skip_pattern(target_endpoint: str, pattern: str) -> bool:
    """
    Совпадение target с skip_targets:
    - точное: /dns/server/zone/master/add
    - префикс: /dns/server/zone/slave/* (звёздочка только в конце)
    """
    target = target_endpoint.rstrip("/")
    pattern = pattern.rstrip("/")
    if pattern.endswith("*"):
        prefix = pattern[:-1].rstrip("/")
        return target == prefix or target.startswith(f"{prefix}/")
    return target == pattern


def _should_skip_field_mapping(config: dict, target_endpoint: str) -> bool:
    """field_mapping не применяется к перечисленным эндпоинтам (skip_targets)."""
    for pattern in config.get("skip_targets", ()):
        if isinstance(pattern, str) and _target_matches_skip_pattern(
            target_endpoint, pattern,
        ):
            return True
    return False


_TEARDOWN_PRIORITY_KEY = "_teardown_priority"
# Меньше → раньше в teardown (интерфейсы до VRF и прочих prerequisite)
_TEARDOWN_PRIORITY_INTERFACE = 10
_TEARDOWN_PRIORITY_DEFAULT = 50
_TEARDOWN_PRIORITY_PREREQUISITE = 100

_SETUP_PRIORITY_KEY = "_setup_priority"
# Меньше → раньше в setup. Явный setup_priority в dependencies.json перекрывает всё ниже.
_SETUP_PHASE_ORDER: dict[str, int] = {
    "interface": 5,        # создание ifname / slave до bond и enslave
    "prerequisite": 10,    # vrf, bond add на другом path, enslave capability
    "field": 30,           # field_mappings (enslave без отдельного endpoint)
    "endpoint": 40,
    "auto": 50,
}
_SETUP_PRIORITY_DEFAULT = 50


def _resolve_teardown_priority(
    config: dict,
    target_endpoint: str,
    default: int = _TEARDOWN_PRIORITY_DEFAULT,
) -> int:
    if (explicit := config.get("teardown_priority")) is not None:
        return int(explicit)
    if _is_prerequisite_field(config, target_endpoint):
        return _TEARDOWN_PRIORITY_PREREQUISITE
    return default


def _append_teardown_step(scenario: dict, step: dict, priority: int) -> None:
    step = copy.deepcopy(step)
    step[_TEARDOWN_PRIORITY_KEY] = priority
    scenario["teardown"].append(step)


def _sort_scenario_teardown(scenario: dict) -> None:
    indexed = list(enumerate(scenario["teardown"]))
    indexed.sort(
        key=lambda item: (
            item[1].pop(_TEARDOWN_PRIORITY_KEY, _TEARDOWN_PRIORITY_DEFAULT),
            item[0],
        ),
    )
    scenario["teardown"] = [step for _, step in indexed]


def _setup_action_rank(step: dict) -> int:
    """Внутри одной фазы: POST …/add раньше прочих шагов (capability, shutdown, …)."""
    endpoint = step.get("endpoint", "").rstrip("/")
    if endpoint.endswith("/add"):
        return 0
    return 1


def _is_field_mapping_lifecycle_config(config: dict) -> bool:
    """field_mappings (enslave, vrf, …), не interface_rules (create/delete/prefix)."""
    if not isinstance(config, dict):
        return False
    if config.get("prefix") or config.get("pattern"):
        return False
    if isinstance(config.get("create"), str) or isinstance(config.get("delete"), str):
        return False
    return bool(config.get("setup") or config.get("teardown"))


def _resolve_setup_priority(
    step: dict,
    *,
    config: dict | None = None,
    phase: str = "field",
    target_endpoint: str | None = None,
) -> int:
    if (explicit := step.get("setup_priority")) is not None:
        return int(explicit)
    if config and (explicit := config.get("setup_priority")) is not None:
        return int(explicit)
    if (
        config
        and target_endpoint
        and phase != "interface"
        and _is_field_mapping_lifecycle_config(config)
        and _is_prerequisite_field(config, target_endpoint)
    ):
        phase = "prerequisite"
    phase_base = _SETUP_PHASE_ORDER.get(phase, _SETUP_PRIORITY_DEFAULT)
    return phase_base * 10 + _setup_action_rank(step)


def _append_setup_step(
    scenario: dict,
    step: dict,
    *,
    priority: int | None = None,
    config: dict | None = None,
    phase: str = "field",
    target_endpoint: str | None = None,
) -> None:
    step = copy.deepcopy(step)
    step[_SETUP_PRIORITY_KEY] = (
        priority
        if priority is not None
        else _resolve_setup_priority(
            step, config=config, phase=phase, target_endpoint=target_endpoint,
        )
    )
    scenario["setup"].append(step)


def _sort_scenario_setup(scenario: dict) -> None:
    indexed = list(enumerate(scenario["setup"]))
    indexed.sort(
        key=lambda item: (
            item[1].pop(_SETUP_PRIORITY_KEY, _SETUP_PRIORITY_DEFAULT),
            item[0],
        ),
    )
    scenario["setup"] = [step for _, step in indexed]


def _field_lifecycle_phases(main_action: str | None, config: dict, target_endpoint: str) -> set:
    """
    Какие фазы lifecycle нужны для field_mapping с учётом action в main_test.
    prerequisite → setup + teardown всегда.
    ресурс того же эндпоинта: add→teardown, delete→setup, modify→оба.
    """
    if _is_prerequisite_field(config, target_endpoint):
        return {"setup", "teardown"}

    if main_action == "add":
        return {"teardown"}
    if main_action == "delete":
        return {"setup"}
    if main_action == "modify":
        return {"setup", "teardown"}
    return {"setup", "teardown"}


def _apply_field_lifecycle(
    scenario,
    target_endpoint,
    field,
    value,
    config,
    main_action,
    variables,
    phases: set,
    *,
    setup_phase: str = "field",
    env_file: dict | None = None,
    vid_pool: _VidPool | None = None,
    endpoint_rules: dict | None = None,
):
    """Setup/teardown для одного field_mapping с учётом phases."""
    if config.get("optional") and value in (None, "", []):
        return

    lifecycle = _resolve_field_lifecycle(config, field)
    var_name = f"created_{field}"

    if "setup" in phases:
        _append_lifecycle_setup(
            scenario, target_endpoint, field, value,
            lifecycle, variables, var_name, main_action=main_action,
            setup_phase=setup_phase,
            field_config=config,
            env_file=env_file,
            vid_pool=vid_pool,
            endpoint_rules=endpoint_rules,
        )
    else:
        variables.setdefault(var_name, value)

    if "teardown" in phases:
        teardown_priority = _resolve_teardown_priority(config, target_endpoint)
        _append_lifecycle_teardown(
            scenario, field, value, lifecycle, variables,
            teardown_priority=teardown_priority,
            env_file=env_file,
            vid_pool=vid_pool,
        )


_SCALAR_ACTION_TYPES = ("integer", "number", "string")


def _find_id_field_for_scalar_delete(add_schema: dict, delete_scalar_schema: dict) -> str | None:
    """ID-поле в action.add, тип которого совпадает со скалярным action.delete."""
    delete_type = _resolve_schema_type(delete_scalar_schema)
    if delete_type not in _SCALAR_ACTION_TYPES:
        return None

    branches = (
        list(_iter_schema_branches(add_schema))
        if _schema_has_composition(add_schema)
        else [add_schema]
    )
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()

    for branch in branches:
        if _resolve_schema_type(branch) != "object":
            continue
        required = set(branch.get("required", []))
        for name, prop_schema in branch.get("properties", {}).items():
            if name in seen:
                continue
            if _resolve_schema_type(prop_schema) != delete_type:
                continue
            if _schema_has_composition(prop_schema):
                continue
            score = 10 if name in required else 0
            lower = name.lower()
            if lower.endswith("_name") or lower.endswith("_id") or lower in ("id", "name"):
                score += 5
            candidates.append((score, name))
            seen.add(name)

    if not candidates:
        return None
    candidates.sort(key=lambda item: -item[0])
    return candidates[0][1]


def detect_scalar_delete_action_pattern(schema: dict) -> dict | None:
    """
    Паттерн: action.add — object, action.delete — скаляр (ID ресурса).
    Возвращает метаданные для авто setup/teardown на том же эндпоинте.
    """
    action_schema = schema.get("properties", {}).get("action")
    if not isinstance(action_schema, dict):
        return None

    branches = list(_iter_schema_branches(action_schema))
    if not branches and action_schema.get("properties"):
        branches = [action_schema]

    delete_inner = None
    add_inner = None

    for branch in branches:
        props = branch.get("properties", {})
        if "delete" in props:
            ds = props["delete"]
            if (
                _resolve_schema_type(ds) in _SCALAR_ACTION_TYPES
                and not _schema_has_composition(ds)
            ):
                delete_inner = ds
        if "add" in props:
            ad = props["add"]
            if _resolve_schema_type(ad) == "object" or _schema_has_composition(ad):
                add_inner = ad

    if delete_inner is None or add_inner is None:
        return None

    id_field = _find_id_field_for_scalar_delete(add_inner, delete_inner)
    if not id_field:
        return None

    return {
        "id_field": id_field,
        "add_inner_schema": add_inner,
    }


def _scalar_delete_value_from_payload(payload: dict) -> object | None:
    action = payload.get("action")
    if not isinstance(action, dict) or "delete" not in action:
        return None
    value = action["delete"]
    if isinstance(value, (dict, list)):
        return None
    return value


def _id_value_from_add_payload(payload: dict, id_field: str) -> object | None:
    action = payload.get("action")
    if not isinstance(action, dict) or "add" not in action:
        return None
    add_data = action["add"]
    if not isinstance(add_data, dict):
        return None
    return add_data.get(id_field)


def _build_auto_add_setup_payload(add_inner_schema: dict, id_field: str, id_value) -> dict:
    branch = _find_oneof_branch_for_field(add_inner_schema, id_field, id_value)
    if branch is None:
        branch = (
            next(_iter_schema_branches(add_inner_schema), None)
            if _schema_has_composition(add_inner_schema)
            else add_inner_schema
        )
    add_obj = _minimal_object_composed(branch)
    add_obj[id_field] = id_value
    add_obj = _coerce_payload_to_schema(add_obj, branch)
    return {"action": {"add": add_obj}}


def _field_mapping_covers_scalar_delete(
    deps: dict, pattern: dict, target_endpoint: str, main_action: str | None,
) -> bool:
    """field_mappings на том же эндпоинте уже покрывает lifecycle для ID-поля."""
    id_field = pattern["id_field"]
    for dep_info in deps.values():
        if dep_info["field"] != id_field:
            continue
        config = dep_info["config"]
        if _is_prerequisite_field(config, target_endpoint):
            continue
        phases = _field_lifecycle_phases(main_action, config, target_endpoint)
        if main_action == "add" and "teardown" in phases:
            return True
        if main_action == "delete" and "setup" in phases:
            return True
    return False


def _apply_auto_scalar_delete_lifecycle(
    scenario, target_endpoint, main_action, payload, pattern, endpoint_rules,
    variables, deps,
):
    """Авто setup/teardown: add→delete(id), delete(id)→minimal add."""
    if not pattern or not main_action:
        return

    if _field_mapping_covers_scalar_delete(deps, pattern, target_endpoint, main_action):
        logger.debug(
            f"Auto lifecycle пропущен: field_mappings покрывает {pattern['id_field']}"
        )
        return

    rules = _get_endpoint_rules(target_endpoint, endpoint_rules) or {}
    action_rules = rules.get(main_action, {})
    id_field = pattern["id_field"]
    add_inner_schema = pattern["add_inner_schema"]

    if main_action == "add":
        if _as_lifecycle_list(action_rules.get("teardown")):
            return
        id_value = _id_value_from_add_payload(payload, id_field)
        if id_value is None:
            return
        scenario["teardown"].append({
            "endpoint": target_endpoint,
            "method": "POST",
            "payload": {"action": {"delete": id_value}},
            "note": f"Auto-teardown: delete {id_field}={id_value}",
        })
        scenario["teardown"][-1][_TEARDOWN_PRIORITY_KEY] = _TEARDOWN_PRIORITY_DEFAULT
        variables[id_field] = id_value
        logger.debug(f"Auto-teardown scalar delete: {id_field}={id_value}")

    elif main_action == "delete":
        if _as_lifecycle_list(action_rules.get("setup")):
            return
        delete_value = _scalar_delete_value_from_payload(payload)
        if delete_value is None:
            return
        setup_payload = _build_auto_add_setup_payload(
            add_inner_schema, id_field, delete_value,
        )
        _append_setup_step(
            scenario,
            {
                "endpoint": target_endpoint,
                "method": "POST",
                "payload": setup_payload,
                "expected_status": 200,
                "note": f"Auto-setup: add {id_field}={delete_value}",
            },
            phase="auto",
        )
        variables["delete"] = delete_value
        variables[id_field] = delete_value
        logger.debug(f"Auto-setup before scalar delete: {id_field}={delete_value}")


_ENDPOINT_RULES_META_KEYS = frozenset({
    "bind_fields",
    "teardown_priority",
    "lifecycle_key_field",
    "requirements",
})


def _normalize_endpoint_rules_path(path: str) -> str:
    return _normalize_endpoint(path).rstrip("/") or "/"


def _endpoint_under_rules_prefix(endpoint: str, prefix: str) -> bool:
    """True, если endpoint — дочерний путь prefix (не сам prefix)."""
    ep = _normalize_endpoint_rules_path(endpoint)
    px = _normalize_endpoint_rules_path(prefix)
    return ep != px and (ep == px or ep.startswith(f"{px}/"))


def _lifecycle_step_fingerprint(step: dict) -> str:
    return json.dumps(
        {
            "endpoint": step.get("endpoint"),
            "method": step.get("method", "POST").upper(),
            "payload": step.get("payload", {}),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _dedupe_lifecycle_steps(steps: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for step in steps:
        fp = _lifecycle_step_fingerprint(step)
        if fp in seen:
            continue
        seen.add(fp)
        result.append(step)
    return result


def _merge_lifecycle_phase_values(*values) -> list[dict] | None:
    steps: list[dict] = []
    for value in values:
        steps.extend(_as_lifecycle_list(value))
    if not steps:
        return None
    return _dedupe_lifecycle_steps(steps)


def _store_merged_lifecycle_phase(merged: dict, phase: str, steps: list[dict] | None) -> None:
    if not steps:
        merged.pop(phase, None)
        return
    merged[phase] = steps[0] if len(steps) == 1 else steps


def _merge_endpoint_action_blocks(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, block in override.items():
        if key in _ENDPOINT_RULES_META_KEYS or key in ("setup", "teardown"):
            continue
        if not isinstance(block, dict):
            continue
        if key not in result or not isinstance(result[key], dict):
            result[key] = copy.deepcopy(block)
            continue
        merged_block = copy.deepcopy(result[key])
        for phase in ("setup", "teardown"):
            combined = _merge_lifecycle_phase_values(
                merged_block.get(phase),
                block.get(phase),
            )
            _store_merged_lifecycle_phase(merged_block, phase, combined)
        result[key] = merged_block
    return result


def _merge_endpoint_rules(*rule_dicts: dict) -> dict | None:
    """
    Сливает правила endpoint_rules: parent (prefix) → child (exact).
    setup/teardown конкатенируются и дедуплицируются; meta — child перекрывает parent.
    """
    merged: dict | None = None
    for rules in rule_dicts:
        if not isinstance(rules, dict) or not rules:
            continue
        if merged is None:
            merged = copy.deepcopy(rules)
            continue
        bind_fields = list(dict.fromkeys(
            list(merged.get("bind_fields", [])) + list(rules.get("bind_fields", [])),
        ))
        if bind_fields:
            merged["bind_fields"] = bind_fields
        else:
            merged.pop("bind_fields", None)
        for meta in ("teardown_priority", "lifecycle_key_field", "requirements"):
            if rules.get(meta) is not None:
                merged[meta] = rules[meta]
        for phase in ("setup", "teardown"):
            combined = _merge_lifecycle_phase_values(
                merged.get(phase),
                rules.get(phase),
            )
            _store_merged_lifecycle_phase(merged, phase, combined)
        merged = _merge_endpoint_action_blocks(merged, rules)
    return merged


def _collect_endpoint_rules_prefixes(
    target_endpoint: str,
    endpoint_rules: dict,
) -> list[str]:
    """Родительские ключи endpoint_rules, чей путь — префикс target (короче → раньше)."""
    target = _normalize_endpoint_rules_path(target_endpoint)
    prefixes: list[str] = []
    for key in endpoint_rules:
        if not isinstance(key, str):
            continue
        norm_key = _normalize_endpoint_rules_path(key)
        if _endpoint_under_rules_prefix(target, norm_key):
            prefixes.append(norm_key)
    prefixes.sort(key=lambda item: (len(item), item))
    return prefixes


def _get_endpoint_rules(target_endpoint: str, endpoint_rules: dict) -> dict | None:
    """
    Правила для эндпоинта: merge всех prefix-родителей + exact-ключ.
    Пример: /telnet/port ← /telnet + /telnet/port (если есть).
    """
    if not endpoint_rules:
        return None
    target = _normalize_endpoint_rules_path(target_endpoint)
    parts: list[dict] = []
    for prefix in _collect_endpoint_rules_prefixes(target, endpoint_rules):
        block = endpoint_rules.get(prefix) or endpoint_rules.get(f"{prefix}/")
        if isinstance(block, dict):
            parts.append(block)
    exact = endpoint_rules.get(target) or endpoint_rules.get(f"{target}/")
    if isinstance(exact, dict):
        parts.append(exact)
    if not parts:
        return None
    return _merge_endpoint_rules(*parts)


def _endpoint_rules_top_level_lifecycle(rules: dict) -> dict:
    """Top-level setup/teardown на ключе эндпоинта (не action-блок)."""
    return {
        phase: rules[phase]
        for phase in ("setup", "teardown")
        if phase in rules
    }


def _should_skip_endpoint_rules_lifecycle_step(
    phase: str,
    step_endpoint: str,
    target_endpoint: str,
    main_action: str | None,
) -> bool:
    """
    Не дублировать lifecycle на том же эндпоинте, что и main_test:
    - setup /…/add при main add (или flat add без action);
    - teardown /…/delete при main delete (или flat delete без action);
    - action.delete на том же path — setup add оставляем (datetime/dst).
    """
    if step_endpoint.rstrip("/") != target_endpoint.rstrip("/"):
        return False
    if phase == "setup":
        return main_action != "delete"
    if phase == "teardown":
        if main_action == "delete":
            return True
        return main_action is None and target_endpoint.rstrip("/").endswith("/delete")
    return False


def _endpoint_rules_action_lifecycle(rules: dict, action_key: str | None) -> dict | None:
    """Блок setup/teardown для конкретного action (on, add first, a, mx, …)."""
    if not action_key:
        return None
    block = rules.get(action_key)
    if not isinstance(block, dict):
        return None
    if "setup" not in block and "teardown" not in block:
        return None
    return block


def _resolve_endpoint_rules_action_key(
    rules: dict,
    main_action: str | None,
    bind_vars: dict,
) -> tuple[str | None, str | None]:
    """
    Ключ блока rules для lifecycle:
    - main_action из payload (on, add, …);
    - иначе lifecycle_key_field (entry_type для entry/delete).
    """
    if main_action:
        return main_action, "action"
    key_field = rules.get("lifecycle_key_field")
    if not key_field:
        return None, None
    key_value = bind_vars.get(key_field)
    if key_value is None:
        return None, None
    return str(key_value).lower(), key_field


def _apply_endpoint_rules_lifecycle(
    scenario,
    target_endpoint,
    payload,
    main_action,
    action_data,
    endpoint_rules,
    variables,
    mock_by_field: dict[str, list] | None = None,
):
    """
    Setup/teardown из endpoint_rules:
    - top-level setup/teardown применяется всегда;
    - дополнительно — rules[main_action] или rules[lifecycle_key_field];
    - lifecycle_key_field: поле payload (entry_type) для выбора блока a/mx/ns/….
    """
    rules = _get_endpoint_rules(target_endpoint, endpoint_rules)
    if not rules:
        return

    bind_fields = rules.get("bind_fields", [])
    if bind_fields:
        bind_vars = _collect_endpoint_bind_vars(payload, action_data, bind_fields)
        bind_vars = _fill_bind_fields_from_mock_data(
            bind_vars, bind_fields, mock_by_field or {},
        )
    elif main_action:
        bind_vars = _collect_bind_vars(action_data) if action_data else {}
    else:
        bind_vars = _collect_bind_vars(payload) if isinstance(payload, dict) else {}
    variables.update(bind_vars)

    teardown_priority = int(
        rules.get("teardown_priority", _TEARDOWN_PRIORITY_DEFAULT),
    )

    lifecycle_sources: list[tuple[dict, str]] = []
    if top := _endpoint_rules_top_level_lifecycle(rules):
        lifecycle_sources.append((top, target_endpoint))
    action_key, key_label = _resolve_endpoint_rules_action_key(
        rules, main_action, bind_vars,
    )
    if action_block := _endpoint_rules_action_lifecycle(rules, action_key):
        if key_label == "action":
            scope = f"action {action_key} on {target_endpoint}"
        else:
            scope = f"{key_label}={action_key} on {target_endpoint}"
        lifecycle_sources.append((action_block, scope))

    for lifecycle_source, scope_label in lifecycle_sources:
        for phase in ("setup", "teardown"):
            for step_def in _as_lifecycle_list(lifecycle_source.get(phase)):
                if not isinstance(step_def, dict) or "endpoint" not in step_def:
                    continue
                if _should_skip_endpoint_rules_lifecycle_step(
                    phase,
                    step_def["endpoint"],
                    target_endpoint,
                    main_action,
                ):
                    logger.debug(
                        f"Self-skip endpoint {phase}: тестируем {target_endpoint}, "
                        f"пропускаю {step_def['endpoint']}",
                    )
                    continue
                step = copy.deepcopy(step_def)
                if "note" not in step:
                    step["note"] = f"{phase}: {scope_label}"
                step_priority = (
                    teardown_priority if phase == "teardown" else None
                )
                _append_custom_lifecycle_step(
                    scenario, phase, step, variables,
                    teardown_priority=step_priority,
                    setup_phase="endpoint",
                    target_endpoint=target_endpoint,
                )
                logger.debug(
                    f"Endpoint {phase} ({scope_label}): {step['endpoint']}",
                )


# =============================================================================
# ПОДСТАНОВКА ПЕРЕМЕННЫХ В ОБЪЕКТЕ (рекурсивно)
# =============================================================================
_PLACEHOLDER_CONTEXT_KEY = "__placeholder_context__"
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
_WHOLE_PLACEHOLDER_RE = re.compile(r"^\{\{\s*([^}]+?)\s*\}\}$")


def _get_nested_placeholder_value(obj, path: str):
    """Читает скалярное значение из вложенного dict по dotted-пути (settings.source)."""
    current = obj
    for part in path.split("."):
        if not part or not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    if isinstance(current, (dict, list)):
        return None
    return current


_PLACEHOLDER_MISSING = object()


def _resolve_placeholder_value(name: str, variables: dict, context: dict | None):
    """Возвращает скалярное значение плейсхолдера с исходным типом."""
    name = name.strip()
    if not name or name.startswith("__"):
        return _PLACEHOLDER_MISSING
    if name in variables:
        return variables[name]
    if context is None:
        return _PLACEHOLDER_MISSING
    if "." in name:
        value = _get_nested_placeholder_value(context, name)
    elif name in context:
        value = context[name]
    else:
        return _PLACEHOLDER_MISSING
    return value


def _resolve_placeholder(name: str, variables: dict, context: dict | None) -> str | None:
    value = _resolve_placeholder_value(name, variables, context)
    if value is _PLACEHOLDER_MISSING:
        return None
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    return str(value)


def _replace_placeholders(obj, variables, context: dict | None = None):
    """
    Заменяет {{var}} и {{ var }} на реальные значения в словаре/списке/строке.
    Плоские имена берутся из variables; dotted-пути ({{settings.source}}) — из
    variables или из context (пейлоад main_test), если передан.
    """
    if context is None:
        context = variables.get(_PLACEHOLDER_CONTEXT_KEY)

    if isinstance(obj, dict):
        return {
            k: _replace_placeholders(v, variables, context)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_replace_placeholders(item, variables, context) for item in obj]
    if isinstance(obj, str):
        whole = _WHOLE_PLACEHOLDER_RE.match(obj)
        if whole:
            resolved = _resolve_placeholder_value(whole.group(1), variables, context)
            if resolved is not _PLACEHOLDER_MISSING:
                return resolved
            return obj

        def _substitute(match: re.Match) -> str:
            resolved = _resolve_placeholder(match.group(1), variables, context)
            return resolved if resolved is not None else match.group(0)

        return _PLACEHOLDER_RE.sub(_substitute, obj)
    return obj


_ETH_VLAN_IFNAME_RE = re.compile(r"^eth(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$")
_VLAN_IFNAME_RE = re.compile(r"^vlan(\d+)$")


class _VidPool:
    """Пул VID из VID_RANGE (.env / os.environ) для интерфейсов без vid в имени."""

    def __init__(self, env_file: dict | None = None):
        self._values = _parse_vid_pool(env_file or {})
        self._index = 0

    def allocate(self) -> int:
        if not self._values:
            return 1
        value = self._values[self._index % len(self._values)]
        self._index += 1
        return value


def _parse_vid_pool(env_file: dict) -> list[int]:
    raw = os.environ.get("VID_RANGE") or env_file.get("VID_RANGE") or ""
    if not raw:
        return []
    values: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s.strip()), int(end_s.strip())
            values.extend(range(start, end + 1))
        else:
            values.append(int(part))
    return list(dict.fromkeys(v for v in values if 1 <= v <= 4094))


def _infer_vid_from_ifname(ifname: str) -> int | None:
    """VID из соглашения об именовании: vlan100 → 100, eth1.200 → 200."""
    if match := _VLAN_IFNAME_RE.fullmatch(ifname):
        return int(match.group(1))
    if match := _ETH_VLAN_IFNAME_RE.fullmatch(ifname):
        return int(match.group(2))
    return None


def _schema_declares_property(schema: dict | None, prop_name: str) -> bool:
    """Есть ли prop_name в properties схемы (включая ветки oneOf/anyOf/allOf)."""
    if not isinstance(schema, dict):
        return False
    if prop_name in schema.get("properties", {}):
        return True
    for branch in _iter_schema_branches(schema):
        if _schema_declares_property(branch, prop_name):
            return True
    return False


def _should_sync_vid_at_node(obj: dict, schema: dict | None) -> bool:
    """Синхронизировать vid только если поле уже в payload или объявлено в схеме."""
    if "vid" in obj:
        return True
    return _schema_declares_property(schema, "vid")


def _synchronize_vid_ifname_inplace(obj, schema: dict | None = None) -> None:
    """
    Согласует vid с ifname в дереве payload.
    vlan4092 ↔ vid 4092, eth1.200 ↔ vid 200 — иначе vlandb teardown не совпадает с main_test.
    vid добавляется/меняется только на узлах, где vid уже есть или объявлен в схеме.
    """
    if isinstance(obj, dict):
        ifname = obj.get("ifname")
        if (
            isinstance(ifname, str)
            and _should_sync_vid_at_node(obj, schema)
            and (inferred := _infer_vid_from_ifname(ifname)) is not None
        ):
            if obj.get("vid") != inferred:
                logger.debug(
                    "sync vid: %s → %s (ifname=%s)",
                    obj.get("vid"),
                    inferred,
                    ifname,
                )
            obj["vid"] = inferred
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        for key, value in obj.items():
            child_schema = props.get(key) if isinstance(props.get(key), dict) else None
            _synchronize_vid_ifname_inplace(value, child_schema)
    elif isinstance(obj, list):
        item_schema = None
        if isinstance(schema, dict) and _resolve_schema_type(schema) == "array":
            items = schema.get("items")
            if isinstance(items, dict):
                item_schema = items
        for item in obj:
            _synchronize_vid_ifname_inplace(item, item_schema)


def synchronize_vid_ifname(payload: dict, schema: dict | None = None) -> dict:
    """Копия payload с согласованными vid/ifname (только где vid уместен по схеме)."""
    result = copy.deepcopy(payload)
    _synchronize_vid_ifname_inplace(result, schema)
    return result


def _resolve_vid(
    ifname: str,
    *,
    env_file: dict | None = None,
    vid_pool: _VidPool | None = None,
) -> int:
    if (inferred := _infer_vid_from_ifname(ifname)) is not None:
        return inferred
    if vid_pool is not None:
        return vid_pool.allocate()
    pool = _parse_vid_pool(env_file or {})
    return pool[0] if pool else 1


def _enrich_interface_variables(
    vars_: dict,
    ifname: str,
    *,
    env_file: dict | None = None,
    vid_pool: _VidPool | None = None,
) -> None:
    """Добавляет vid/vlan для плейсхолдеров {{vid}} и {{vlan}} в lifecycle."""
    ctx = vars_.get(_PLACEHOLDER_CONTEXT_KEY)
    if (
        isinstance(ctx, dict)
        and ctx.get("ifname") == ifname
        and isinstance(ctx.get("vid"), int)
    ):
        vid = ctx["vid"]
    else:
        vid = _resolve_vid(ifname, env_file=env_file, vid_pool=vid_pool)
    vars_["vid"] = vid
    vars_["vlan"] = str(vid)


def _payload_template_uses_vid(payload) -> bool:
    if isinstance(payload, dict):
        if "vid" in payload:
            return True
        return any(_payload_template_uses_vid(v) for v in payload.values())
    if isinstance(payload, list):
        return any(_payload_template_uses_vid(item) for item in payload)
    if isinstance(payload, str) and payload.strip() == "{{vid}}":
        return True
    return False


def _lifecycle_vars(
    field_name,
    field_value,
    variables: dict | None = None,
    *,
    env_file: dict | None = None,
    vid_pool: _VidPool | None = None,
    template_ifname: str | None = None,
) -> dict:
    """Переменные для {{placeholder}} в setup/teardown."""
    result = {field_name: field_value}
    if field_name == "ifname":
        result["ifname"] = field_value
    if variables:
        result.update(variables)
    ifname_for_vid = template_ifname or (
        field_value if isinstance(field_value, str) else None
    )
    if ifname_for_vid:
        _enrich_interface_variables(
            result, ifname_for_vid, env_file=env_file, vid_pool=vid_pool,
        )
    return result


_VLAND_DB_ENDPOINT = "/interfaces/switchport/vlandb"


def _vlandb_step_templates(endpoint_rules: dict) -> tuple[dict | None, dict | None]:
    """Шаблоны add/delete vlandb из endpoint_rules (без хардкода payload)."""
    rule = endpoint_rules.get(_VLAND_DB_ENDPOINT, {})
    add_tpl = rule.get("delete", {}).get("setup")
    del_tpl = rule.get("add", {}).get("teardown")
    return add_tpl, del_tpl


def _ensure_vlandb_lifecycle(
    scenario: dict,
    variables: dict,
    endpoint_rules: dict,
    *,
    setup_phase: str = "interface",
    target_endpoint: str | None = None,
    field_config: dict | None = None,
    teardown_priority: int = 90,
) -> None:
    """Перед vlan/eth_vlan add — vlandb add; в teardown — vlandb delete для того же vid."""
    vid = variables.get("vid")
    if vid is None:
        return
    marker = f"_vlandb_tracked_{vid}"
    if variables.get(marker):
        return
    add_tpl, del_tpl = _vlandb_step_templates(endpoint_rules)
    if not add_tpl or not del_tpl:
        return
    variables[marker] = True
    _append_custom_lifecycle_step(
        scenario, "setup", add_tpl, variables,
        setup_phase=setup_phase,
        target_endpoint=target_endpoint,
        field_config=field_config,
    )
    _append_custom_lifecycle_step(
        scenario, "teardown", del_tpl, variables,
        teardown_priority=teardown_priority,
    )
    logger.debug(f"Auto vlandb lifecycle для vid={vid}")


def _append_custom_lifecycle_step(
    scenario,
    phase,
    step_def,
    variables,
    *,
    teardown_priority: int | None = None,
    setup_phase: str = "field",
    target_endpoint: str | None = None,
    field_config: dict | None = None,
):
    """Добавляет кастомный setup/teardown из dependencies.json."""
    payload_template = step_def.get("payload", {}) # Получаем шаблон пейлоада
    payload = _replace_placeholders(copy.deepcopy(payload_template), variables) # Заменяем значения в шаблоне пейлоада
    step = {
        "endpoint": step_def["endpoint"], # Добавляем endpoint в шаг
        "method": step_def.get("method", "POST").upper(),
        "payload": payload,
    }
    if phase == "setup":
        step["expected_status"] = step_def.get("expected_status", 200)
    if note := step_def.get("note"): # Если есть note
        step["note"] = note # Добавляем note в шаг
    if phase == "setup": # Если фаза setup
        if extract_var := step_def.get("extract_to_variable"): # Если есть extract_to_variable
            step["extract_to_variable"] = extract_var # Добавляем extract_to_variable в шаг
        if extract_path := step_def.get("response_extract"): # Если есть response_extract
            step["response_extract"] = extract_path # Добавляем response_extract в шаг
        _append_setup_step(
            scenario,
            step,
            priority=step_def.get("setup_priority"),
            config=field_config,
            phase=setup_phase,
            target_endpoint=target_endpoint,
        )
    else:
        _append_teardown_step(
            scenario,
            step,
            teardown_priority if teardown_priority is not None else _TEARDOWN_PRIORITY_DEFAULT,
        )


def _resolve_field_lifecycle(config: dict, field_name: str) -> dict:
    """
    Нормализует field_mappings к единому виду:
    setup/teardown (кастом) или create/delete (эндпоинт-строка).
    Поддерживает provider + action_create/action_delete.
    """
    result = {}
    for key in ("setup", "teardown", "create", "delete"):
        if config.get(key):
            result[key] = config[key] # Добавляем шаг в результат

    if action_create := config.get("action_create"):
        result["_legacy_create_action"] = action_create # Добавляем action_create в результат
    if action_delete := config.get("action_delete"):
        result["_legacy_delete_action"] = action_delete # Добавляем action_delete в результат

    if provider := config.get("provider"):
        if "create" not in result and "setup" not in result:
            result["create"] = provider # Добавляем create в результат
            result.setdefault("_legacy_create_action", config.get("action_create", "add"))
        if "delete" not in result and "teardown" not in result:
            result["delete"] = provider # Добавляем delete в результат
            result.setdefault("_legacy_delete_action", config.get("action_delete", "delete"))

    result["_extract"] = config.get("extract", f"data.{field_name}") # Добавляем extract в результат
    return result


def _as_lifecycle_list(value):
    """Один шаг или список шагов -> всегда список."""
    if value is None: # Если значение None, возвращаем пустой список
        return []
    if isinstance(value, list): # Если значение список, возвращаем значение
        return value
    return [value] # Возвращаем список с одним значением


def _default_create_step(endpoint, field_name, field_value, lifecycle):
    """Шаг create: legacy-формат или простой эндпоинт."""
    if legacy_action := lifecycle.get("_legacy_create_action"): # Если есть legacy_create_action
        payload = {"action": legacy_action, field_name: field_value} # Создаем payload
    else: # Если нет legacy_create_action
        payload = {field_name: field_value} # Создаем payload
    return {
        "endpoint": endpoint, # Добавляем endpoint в шаг
        "method": "POST", # Добавляем method в шаг
        "payload": payload, # Добавляем payload в шаг
        "expected_status": 200, # Добавляем expected_status в шаг
        "_default_create": True, # Добавляем _default_create в шаг
    }


def _default_delete_step(endpoint, field_name, field_value, lifecycle):
    """Шаг delete: legacy-формат или простой эндпоинт."""
    if legacy_action := lifecycle.get("_legacy_delete_action"): # Если есть legacy_delete_action
        payload = {"action": legacy_action, field_name: field_value} # Создаем payload
    else: # Если нет legacy_delete_action
        payload = {field_name: field_value} # Создаем payload
    return {
        "endpoint": endpoint, # Добавляем endpoint в шаг
        "method": "POST", # Добавляем method в шаг
        "payload": payload, # Добавляем payload в шаг
        "note": f"Cleanup {field_name}", # Добавляем note в шаг
        "_default_delete": True, # Добавляем _default_delete в шаг
    }


def _iter_setup_steps(lifecycle, field_name, field_value):
    """Шаги setup: кастомный setup или create (строка / список строк)."""
    if lifecycle.get("setup") is not None: # Если есть setup
        source = lifecycle["setup"] # Создаем source
    elif lifecycle.get("create") is not None: # Если есть create
        source = lifecycle["create"] # Создаем source
    else: # Если нет setup и create
        return # Возвращаем пустой генератор

    for item in _as_lifecycle_list(source): # Для каждого элемента в source
        if isinstance(item, str): # Если элемент строка, генерируем шаг create
            yield _default_create_step(item, field_name, field_value, lifecycle) # Генерируем шаг create
        elif isinstance(item, dict):
            yield item # Генерируем шаг


def _iter_teardown_steps(lifecycle, field_name, field_value):
    """Шаги teardown: кастомный teardown или delete (строка / список строк)."""
    if lifecycle.get("teardown") is not None: # Если есть teardown
        source = lifecycle["teardown"] # Создаем source
    elif lifecycle.get("delete") is not None: # Если есть delete
        source = lifecycle["delete"] # Создаем source
    else: # Если нет teardown и delete
        return # Возвращаем пустой генератор

    for item in _as_lifecycle_list(source): # Для каждого элемента в source
        if isinstance(item, str):
            yield _default_delete_step(item, field_name, field_value, lifecycle) # Генерируем шаг delete
        elif isinstance(item, dict):
            yield item # Генерируем шаг


def _normalize_lifecycle_requirements(value) -> frozenset[str]:
    """Нормализует requirements: ['setup', 'teardown'] → frozenset."""
    if not value:
        return frozenset()
    items = [value] if isinstance(value, str) else value
    return frozenset(
        item.strip().lower()
        for item in items
        if isinstance(item, str) and item.strip().lower() in ("setup", "teardown")
    )


def _append_lifecycle_setup(scenario, target_endpoint, field_name, field_value,
                            lifecycle, variables, var_name, main_action=None,
                            *, lifecycle_requirements: frozenset[str] | None = None,
                            template_ifname: str | None = None,
                            setup_phase: str = "field",
                            field_config: dict | None = None,
                            env_file: dict | None = None,
                            vid_pool: _VidPool | None = None,
                            endpoint_rules: dict | None = None):
    """Setup: один или несколько шагов (setup / create)."""
    steps = list(_iter_setup_steps(lifecycle, field_name, field_value))
    if not steps:
        return

    extract_assigned = False
    vars_ = _lifecycle_vars(
        field_name, field_value, variables,
        env_file=env_file, vid_pool=vid_pool, template_ifname=template_ifname,
    )
    if template_ifname is not None:
        vars_["ifname"] = template_ifname
    force_setup = bool(
        lifecycle_requirements and "setup" in lifecycle_requirements
    )
    needs_vlandb = any(_payload_template_uses_vid(step.get("payload", {})) for step in steps)

    for step_def in steps:
        endpoint = step_def["endpoint"]
        is_self = endpoint.rstrip("/") == target_endpoint.rstrip("/")
        if is_self and main_action != "delete" and not force_setup:
            logger.debug(
                f"Self-skip setup: тестируем {target_endpoint}, "
                f"пропускаю {endpoint}"
            )
            continue

        step = copy.deepcopy(step_def) # Копируем шаг
        if "extract_to_variable" in step:
            extract_assigned = True # Устанавливаем extract_assigned в True
        elif not extract_assigned: # Если нет extract_to_variable
            step["extract_to_variable"] = var_name # Устанавливаем extract_to_variable в var_name
            step["response_extract"] = lifecycle.get("_extract", f"data.{field_name}") # Устанавливаем response_extract в extract
            extract_assigned = True # Устанавливаем extract_assigned в True

        if step.pop("_default_create", False): # Если есть _default_create
            _append_setup_step(
                scenario,
                {
                    "endpoint": step["endpoint"],
                    "method": step.get("method", "POST").upper(),
                    "payload": step["payload"],
                    "expected_status": step.get("expected_status", 200),
                    "extract_to_variable": step["extract_to_variable"],
                    "response_extract": step["response_extract"],
                },
                phase=setup_phase,
                target_endpoint=target_endpoint,
                config=field_config,
            )
            logger.debug(f"Setup (create) для {field_name}={field_value} → {endpoint}") # Логируем setup (create)
        else: # Если нет _default_create
            _append_custom_lifecycle_step(
                scenario, "setup", step, vars_,
                setup_phase=setup_phase,
                target_endpoint=target_endpoint,
                field_config=field_config,
            ) # Добавляем кастомный шаг setup
            logger.debug(f"Кастомный setup для {field_name}={field_value} → {endpoint}") # Логируем кастомный setup

    if needs_vlandb and endpoint_rules:
        _ensure_vlandb_lifecycle(
            scenario, vars_, endpoint_rules,
            setup_phase=setup_phase,
            target_endpoint=target_endpoint,
            field_config=field_config,
        )
        variables.update({k: vars_[k] for k in ("vid", "vlan") if k in vars_})

    variables[var_name] = field_value


def _append_lifecycle_teardown(
    scenario,
    field_name,
    field_value,
    lifecycle,
    variables,
    *,
    lifecycle_requirements: frozenset[str] | None = None,
    teardown_priority: int = _TEARDOWN_PRIORITY_DEFAULT,
    template_ifname: str | None = None,
    env_file: dict | None = None,
    vid_pool: _VidPool | None = None,
):
    """Teardown: один или несколько шагов (teardown / delete)."""
    steps = list(_iter_teardown_steps(lifecycle, field_name, field_value))
    if not steps:
        if lifecycle_requirements and "teardown" in lifecycle_requirements:
            logger.warning(
                f"requirements содержит teardown, но шаги не заданы для {field_name}"
            )
        return

    vars_ = _lifecycle_vars(
        field_name, field_value, variables,
        env_file=env_file, vid_pool=vid_pool, template_ifname=template_ifname,
    )
    if template_ifname is not None:
        vars_["ifname"] = template_ifname

    for step_def in steps: # Для каждого шага
        endpoint = step_def["endpoint"] # Получаем endpoint
        step = copy.deepcopy(step_def) # Копируем шаг

        if step.pop("_default_delete", False):
            _append_teardown_step(
                scenario,
                {
                    "endpoint": step["endpoint"],
                    "method": step.get("method", "POST").upper(),
                    "payload": step["payload"],
                    "note": step.get("note", f"Cleanup {field_name}"),
                },
                teardown_priority,
            )
            logger.debug(f"Teardown (delete) для {field_name}={field_value} → {endpoint}")
        else:
            _append_custom_lifecycle_step(
                scenario, "teardown", step, vars_,
                teardown_priority=teardown_priority,
            )
            logger.debug(f"Кастомный teardown для {field_name}={field_value} → {endpoint}")

# =============================================================================
# АВТО-ОПРЕДЕЛЕНИЕ ИНТЕРФЕЙСОВ (pattern / prefix, порядок правил важен)
# =============================================================================
# Ключи, значения которых не сканируются как ifname (enum/типы, не имена интерфейсов)
_SKIP_INTERFACE_VALUE_KEYS = frozenset({
    "action",
    "mode_type",
    "mode",
    "type",
    "chain",
    "protocol",
    "dpi_protocol",
    "adm_state",
    "method",
})


def _matches_interface_prefix(field_value: str, prefix: str) -> bool:
    """
    Имя интерфейса по prefix: br0, bond0, vlan10, lo.
    Не matчит произвольные слова с тем же началом (broadcast для br, loopback для lo).
    """
    if not field_value.startswith(prefix):
        return False
    suffix = field_value[len(prefix):]
    if not suffix:
        return True
    if suffix.isdigit():
        return True
    if "." in suffix:
        head, tail = suffix.split(".", 1)
        return head.isdigit() and tail.isdigit()
    return False


def _normalize_interface_rules(field_rules: dict) -> list:
    """
    Приводит interface_rules к упорядоченному списку правил.
    Поддерживает новый формат "rules" и "prefixes".
    """
    if "rules" in field_rules:
        return field_rules["rules"] # Возвращаем список правил

    rules = [] # Список правил
    for prefix, endpoints in field_rules.get("prefixes", {}).items(): # Для каждого префикса
        rules.append({"prefix": prefix, **endpoints}) # Добавляем правило в список
    return rules


def _resolve_interface_lifecycle_by_value(field_value: str, field_rules: dict):
    """
    Подбирает lifecycle-правило по значению (pattern/prefix).
    Не зависит от имени JSON-поля.
    """
    if not isinstance(field_value, str):
        return None

    for rule in _normalize_interface_rules(field_rules):
        matched = False
        if "pattern" in rule:
            matched = bool(re.fullmatch(rule["pattern"], field_value))
        elif "prefix" in rule:
            matched = _matches_interface_prefix(field_value, rule["prefix"])

        if not matched:
            continue

        if rule.get("physical") and not rule.get("teardown"):
            logger.debug(f"Физический интерфейс (без lifecycle): {field_value}")
            return {"physical": True}

        result = {}
        if rule.get("setup"):
            result["setup"] = rule["setup"]
        if rule.get("create"):
            result["create"] = rule["create"]
        if rule.get("teardown"):
            result["teardown"] = rule["teardown"]
        elif rule.get("delete"):
            result["delete"] = rule["delete"]
        if reqs := rule.get("requirements"):
            result["requirements"] = _normalize_lifecycle_requirements(reqs)
        if (priority := rule.get("teardown_priority")) is not None:
            result["teardown_priority"] = int(priority)
        if result:
            return result

    return None


def _resolve_auto_interface(field_name, field_value, iface_rules):
    """
    Определяет lifecycle интерфейса по имени поля и значению.
    Возвращает:
      - {"create": "...", "delete": "..."} — setup + delete в teardown
      - {"setup": {...}, "teardown": {...}} — кастомные шаги
      - {"teardown": {...}} — только кастомный teardown (reset и т.п.)
      - {"physical": True} — ничего не делать (legacy)
      - None — правило не найдено

    teardown/setup в правиле имеют приоритет над delete/create.
    physical без teardown — полный пропуск lifecycle.
    """
    if field_name not in iface_rules or not isinstance(field_value, str):
        return None
    return _resolve_interface_lifecycle_by_value(field_value, iface_rules[field_name])


def parse_interface_lifecycle_config(dependencies: dict) -> dict | None:
    """
    Конфиг schema-driven lifecycle из dependencies.json.
    По умолчанию: IFNAME → interface_rules.ifname (если секция не задана).
    enabled: false — только key-based обнаружение (ifname и др. ключи в interface_rules).
    """
    raw = dependencies.get("interface_lifecycle")
    if isinstance(raw, dict) and raw.get("enabled") is False:
        return None

    if isinstance(raw, dict):
        schema_components = [
            name for name in raw.get("schema_components", ["IFNAME"])
            if isinstance(name, str) and name.strip()
        ]
        rules_key = raw.get("rules_key", "ifname")
    else:
        schema_components = ["IFNAME"]
        rules_key = "ifname"

    if not schema_components:
        return None

    exclude_fields = frozenset(
        name for name in raw.get("exclude_fields", [])
        if isinstance(name, str) and name.strip()
    ) if isinstance(raw, dict) else frozenset()

    return {
        "schema_components": schema_components,
        "schema_components_set": frozenset(schema_components),
        "rules_key": rules_key,
        "exclude_fields": exclude_fields,
    }


def _schema_ref_component_name(schema: dict) -> str | None:
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        return name or None
    return None


def _iter_resolved_schema_branches(schema: dict, components: dict):
    if not isinstance(schema, dict):
        return
    if any(keyword in schema for keyword in ("oneOf", "anyOf", "allOf")):
        for branch in _iter_schema_branches(schema):
            if isinstance(branch, dict):
                yield branch
        return
    yield schema


def _interface_component_patterns(
    components: dict,
    lifecycle_config: dict,
) -> dict[str, str]:
    """Имена OpenAPI-компонентов → pattern (для inline-схем после mock/inventory)."""
    patterns: dict[str, str] = {}
    schemas = components.get("schemas", {})
    for name in lifecycle_config.get("schema_components", ()):
        comp = schemas.get(name)
        if isinstance(comp, dict) and (pattern := comp.get("pattern")):
            patterns[name] = pattern
    return patterns


def _branch_interface_component_name(
    branch: dict,
    components: dict,
    lifecycle_config: dict,
) -> str | None:
    allowed = lifecycle_config.get("schema_components_set", frozenset())
    if ref_name := _schema_ref_component_name(branch):
        return ref_name if ref_name in allowed else None

    resolved = ResolveScheme._resolve_ref(branch, components)
    if not isinstance(resolved, dict):
        return None
    branch_pattern = resolved.get("pattern")
    if not branch_pattern:
        return None

    for comp_name, comp_pattern in _interface_component_patterns(
        components, lifecycle_config,
    ).items():
        if branch_pattern == comp_pattern:
            return comp_name
    return None


def _string_matches_resolved_schema(value: str, schema: dict, components: dict) -> bool:
    if not isinstance(value, str) or not isinstance(schema, dict):
        return False

    resolved = ResolveScheme._resolve_ref(schema, components)
    if not isinstance(resolved, dict):
        return False

    if "enum" in resolved:
        return value in resolved["enum"]
    if "const" in resolved:
        return value == resolved["const"]
    if pattern := resolved.get("pattern"):
        try:
            return bool(re.fullmatch(pattern, value))
        except re.error:
            logger.warning("Некорректный pattern в схеме: %s", pattern)
            return False
    return False


def _string_matches_interface_lifecycle_branch(
    value: str,
    branch: dict,
    components: dict,
) -> bool:
    """
    Совпадение значения с веткой схемы интерфейса.
    Enum из инвентаря не блокирует pattern: eth1.1 валиден по pattern,
    даже если в enum только eth1.2 из .env.
    """
    resolved = ResolveScheme._resolve_ref(branch, components)
    if not isinstance(resolved, dict):
        return False
    if "enum" in resolved and value in resolved["enum"]:
        return True
    if "const" in resolved and value == resolved["const"]:
        return True
    if pattern := resolved.get("pattern"):
        try:
            return bool(re.fullmatch(pattern, value))
        except re.error:
            logger.warning("Некорректный pattern в схеме: %s", pattern)
            return False
    return False


def _value_matches_interface_lifecycle_schema(
    value: str,
    field_schema: dict,
    components: dict,
    lifecycle_config: dict,
) -> bool:
    """True, если значение соответствует одной из schema_components (anyOf/oneOf по значению)."""
    allowed = lifecycle_config.get("schema_components_set", frozenset())
    if not allowed:
        return False

    for branch in _iter_resolved_schema_branches(field_schema, components):
        comp_name = _branch_interface_component_name(branch, components, lifecycle_config)
        if comp_name not in allowed:
            continue
        if _string_matches_interface_lifecycle_branch(value, branch, components):
            return True
    return False


def _property_schema_for_key(parent_schema: dict | None, key: str) -> dict | None:
    if not isinstance(parent_schema, dict):
        return None
    return _property_schema_in_node(parent_schema, key)


def _collect_interface_candidates(
    obj,
    iface_rules,
    candidates: list,
    *,
    discovery_index: int = 0,
    request_schema: dict | None = None,
    current_schema: dict | None = None,
    openapi_components: dict | None = None,
    lifecycle_config: dict | None = None,
) -> int:
    """Собирает строки из payload для interface_rules (по ключу и по OpenAPI-схеме)."""
    if current_schema is None and request_schema is not None:
        current_schema = request_schema

    rules_key = lifecycle_config.get("rules_key", "ifname") if lifecycle_config else "ifname"
    rules_for_schema = iface_rules.get(rules_key, {}) if lifecycle_config else None
    skip_schema_keys = lifecycle_config.get("skip_field_keys", frozenset()) if lifecycle_config else frozenset()

    if isinstance(obj, dict):
        for key, value in obj.items():
            child_schema = _property_schema_for_key(current_schema, key)
            if isinstance(value, str):
                if key in _SKIP_INTERFACE_VALUE_KEYS:
                    continue
                if key in iface_rules and _resolve_auto_interface(key, value, iface_rules):
                    candidates.append((discovery_index, key, value))
                    discovery_index += 1
                elif (
                    lifecycle_config
                    and rules_for_schema is not None
                    and key not in iface_rules
                    and key not in skip_schema_keys
                    and child_schema is not None
                    and openapi_components
                    and _value_matches_interface_lifecycle_schema(
                        value, child_schema, openapi_components, lifecycle_config,
                    )
                    and _resolve_interface_lifecycle_by_value(value, rules_for_schema)
                ):
                    candidates.append((discovery_index, rules_key, value))
                    discovery_index += 1
            else:
                discovery_index = _collect_interface_candidates(
                    value, iface_rules, candidates,
                    discovery_index=discovery_index,
                    request_schema=request_schema,
                    current_schema=child_schema,
                    openapi_components=openapi_components,
                    lifecycle_config=lifecycle_config,
                )
    elif isinstance(obj, list):
        item_schema = None
        if isinstance(current_schema, dict) and _resolve_schema_type(current_schema) == "array":
            items = current_schema.get("items")
            if isinstance(items, dict):
                item_schema = items
        for item in obj:
            if isinstance(item, str):
                if "ifname" in iface_rules and _resolve_auto_interface(
                    "ifname", item, iface_rules,
                ):
                    candidates.append((discovery_index, "ifname", item))
                    discovery_index += 1
                elif (
                    lifecycle_config
                    and rules_for_schema is not None
                    and item_schema is not None
                    and openapi_components
                    and _value_matches_interface_lifecycle_schema(
                        item, item_schema, openapi_components, lifecycle_config,
                    )
                    and _resolve_interface_lifecycle_by_value(item, rules_for_schema)
                ):
                    candidates.append((discovery_index, rules_key, item))
                    discovery_index += 1
            else:
                discovery_index = _collect_interface_candidates(
                    item, iface_rules, candidates,
                    discovery_index=discovery_index,
                    request_schema=request_schema,
                    current_schema=item_schema,
                    openapi_components=openapi_components,
                    lifecycle_config=lifecycle_config,
                )
    return discovery_index


# =============================================================================
# ИНВЕНТАРЬ ИНТЕРФЕЙСОВ УСТРОЙСТВА (.env / allowed в dependencies.json)
# =============================================================================
def load_env_file(path: str = ".env") -> dict:
    """Простой парсер .env без внешних зависимостей."""
    env = {}
    env_path = Path(path)
    if not env_path.is_file():
        return env

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _parse_name_list(value: str) -> list:
    return [name.strip() for name in value.split(",") if name.strip()]


def _resolve_allowed_names(rule: dict, env_file: dict) -> list | None:
    """
    Определяет список допустимых имён для правила интерфейса.
    Возвращает:
      - list | None — список имён или None, если нет допустимых имён
    """
    allowed = rule.get("allowed") # Список имён из правила
    if env_key := rule.get("env"): # Если есть ключ env
        raw = os.environ.get(env_key) or env_file.get(env_key) # Получаем значение из переменных окружения или из файла
        if raw: # Если значение не пустое
            allowed = _parse_name_list(raw) # Парсим список имён
    if not allowed:
        return None # Если список имён пустой, возвращаем None
    return list(dict.fromkeys(allowed)) # Возвращаем список имён


def _collect_interface_schema_patterns(iface_rules: dict) -> frozenset[str]:
    """OpenAPI pattern'ы из interface_rules — только к ним применяется prefix-инвентарь."""
    patterns: set[str] = set()
    for field_rules in iface_rules.values():
        for rule in _normalize_interface_rules(field_rules):
            if pattern := rule.get("pattern"):
                patterns.add(pattern)
    return frozenset(patterns)


def build_interface_inventory(dependencies: dict, env_file: dict | None = None) -> list:
    """
    Собирает инвентарь из interface_rules (pattern или prefix + env/allowed).
    Источники имён (по приоритету): os.environ > .env > поле "allowed".
    """
    env_file = env_file or {} 
    entries = [] # Список интерфейсов
    iface_rules = dependencies.get("interface_rules", {}) # Правила интерфейсов
    interface_schema_patterns = _collect_interface_schema_patterns(iface_rules)

    for field_rules in iface_rules.values(): # Для каждого правила интерфейса
        for rule in _normalize_interface_rules(field_rules): # Нормализуем правила интерфейса
            names = _resolve_allowed_names(rule, env_file) # Получаем список имён из правила
            if not names: # Если список имён пустой, пропускаем
                continue

            entry = {"names": names} # Создаем запись для интерфейса
            if pattern := rule.get("pattern"): # Если есть pattern
                entry["pattern"] = pattern # Добавляем pattern в запись
            if prefix := rule.get("prefix"): # Если есть prefix
                entry["prefix"] = prefix # Добавляем prefix в запись
                if interface_schema_patterns:
                    entry["schema_patterns"] = sorted(interface_schema_patterns)
            if not entry.get("pattern") and not entry.get("prefix"):
                continue # Если нет pattern и prefix, пропускаем

            entries.append(entry) # Добавляем запись в список

    return entries # Возвращаем список интерфейсов


def _inventory_matches_schema_pattern(entry: dict, schema_pattern: str) -> bool:
    """Проверяет, относится ли инвентарь к pattern-узлу схемы OpenAPI."""
    names = entry["names"]
    if entry.get("pattern"):
        return entry["pattern"] == schema_pattern

    prefix = entry.get("prefix")
    if not prefix:
        return False
    if not all(_matches_interface_prefix(name, prefix) for name in names):
        return False
    allowed = entry.get("schema_patterns")
    if allowed is not None:
        return schema_pattern in allowed
    return False


def build_eth_parents_with_vlan_children(inventory: list) -> set[str]:
    """Родительские eth, у которых в инвентаре есть eth-vlan дочерние интерфейсы."""
    parents: set[str] = set()
    for entry in inventory:
        for name in entry.get("names", []):
            if match := _ETH_VLAN_IFNAME_RE.fullmatch(name):
                parents.add(f"eth{match.group(1)}")
    return parents


def apply_interface_inventory(
    schema: dict,
    inventory: list,
    *,
    blocked_eth_parents: set[str] | None = None,
) -> dict:
    """Подменяет pattern → enum для узлов схемы, подходящих под инвентарь устройства."""
    if not inventory:
        return schema

    blocked_eth_parents = blocked_eth_parents or set()
    schema = copy.deepcopy(schema)

    def _walk(obj):
        if not isinstance(obj, dict):
            return
        schema_pattern = obj.get("pattern")
        if schema_pattern:
            for entry in inventory:
                if _inventory_matches_schema_pattern(entry, schema_pattern):
                    names = list(entry["names"])
                    if (
                        blocked_eth_parents
                        and schema_pattern.startswith("^eth")
                        and r"\." not in schema_pattern
                    ):
                        filtered = [n for n in names if n not in blocked_eth_parents]
                        if filtered:
                            names = filtered
                    obj["enum"] = names
                    logger.debug(
                        f"Инвентарь для pattern {schema_pattern!r}: {names}"
                    )
                    break
        for value in obj.values():
            if isinstance(value, dict):
                _walk(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _walk(item)

    _walk(schema)
    return schema


# =============================================================================
# MOCK DATA (dependencies.json → mock_data, опционально)
# =============================================================================
_OPENAPI_DEFAULT_PATH = Path(__file__).resolve().parent / "openapi.json"
_openapi_components_cache: dict | None = None


def load_openapi_components(path: str = "openapi.json") -> dict:
    """Кэширует components из openapi.json для сопоставления by_schema."""
    global _openapi_components_cache
    if _openapi_components_cache is None:
        openapi_path = Path(path)
        if not openapi_path.is_file():
            openapi_path = _OPENAPI_DEFAULT_PATH
        if openapi_path.is_file():
            with open(openapi_path, "r", encoding="utf-8") as f:
                _openapi_components_cache = json.load(f).get("components", {})
        else:
            _openapi_components_cache = {}
    return _openapi_components_cache


def _parse_mock_value_list(value) -> list:
    if isinstance(value, list):
        return [item for item in value if item is not None and item != ""]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if value is None:
        return []
    return [value]


def parse_mock_data_config(dependencies: dict) -> dict | None:
    """
    Извлекает mock_data из dependencies.json.
    Отсутствие секции или пустые by_schema/by_field → None (генерация как раньше).
    """
    raw = dependencies.get("mock_data")
    if not isinstance(raw, dict):
        return None
    by_schema = {
        name: _parse_mock_value_list(values)
        for name, values in raw.get("by_schema", {}).items()
        if _parse_mock_value_list(values)
    }
    by_field = {
        name: _parse_mock_value_list(values)
        for name, values in raw.get("by_field", {}).items()
        if _parse_mock_value_list(values)
    }
    if not by_schema and not by_field:
        return None
    return {"by_schema": by_schema, "by_field": by_field}


def build_mock_pattern_index(
    openapi_components: dict,
    mock_config: dict,
) -> dict[str, list]:
    """pattern OpenAPI-компонента → список mock-значений (только by_schema)."""
    index: dict[str, list] = {}
    schemas = openapi_components.get("schemas", {})
    for schema_name, values in mock_config.get("by_schema", {}).items():
        component = schemas.get(schema_name)
        if not isinstance(component, dict):
            logger.warning(
                f"mock_data.by_schema: компонент {schema_name!r} не найден в openapi.json"
            )
            continue
        pattern = component.get("pattern")
        if not pattern:
            logger.warning(
                f"mock_data.by_schema: у {schema_name!r} нет pattern — пропуск "
                "(используйте by_field для этого поля)"
            )
            continue
        if pattern in index:
            logger.warning(
                f"mock_data.by_schema: pattern {schema_name!r} дублирует другой компонент"
            )
        index[pattern] = list(values)
        logger.debug(f"mock_data index: {schema_name} → {len(values)} знач.")
    return index


def apply_mock_data(
    schema: dict,
    mock_config: dict | None,
    pattern_index: dict[str, list] | None = None,
) -> dict:
    """
    Подменяет pattern → enum для mock-значений из dependencies.json.
    by_field и by_schema имеют приоритет над enum из инвентаря интерфейсов.
    """
    if not mock_config:
        return schema

    pattern_index = pattern_index or {}
    by_field = mock_config.get("by_field", {})
    schema = copy.deepcopy(schema)

    def _walk(obj: dict) -> None:
        if not isinstance(obj, dict):
            return

        pattern = obj.get("pattern")
        if pattern and pattern in pattern_index:
            obj["enum"] = list(pattern_index[pattern])
            logger.debug(
                f"mock_data by_schema (pattern): {pattern_index[pattern]}"
            )

        for prop_name, prop_schema in obj.get("properties", {}).items():
            if (
                prop_name in by_field
                and isinstance(prop_schema, dict)
                and not _schema_has_composition(prop_schema)
            ):
                prop_schema["enum"] = list(by_field[prop_name])
                logger.debug(f"mock_data by_field: {prop_name}={by_field[prop_name]}")

        for keyword in ("properties", "patternProperties"):
            for item in obj.get(keyword, {}).values():
                if isinstance(item, dict):
                    _walk(item)
        if isinstance(obj.get("items"), dict):
            _walk(obj["items"])
        for branch in _iter_schema_branches(obj):
            _walk(branch)

    _walk(schema)
    return schema


def _interface_var_name(ifname: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", ifname)
    return f"created_ifname_{safe}"


def _apply_interface_lifecycle(scenario, target_endpoint, field_name, field_value,
                               iface_rules, variables, handled_ifnames: set,
                               main_action=None,
                               *, env_file: dict | None = None,
                               vid_pool: _VidPool | None = None,
                               endpoint_rules: dict | None = None):
    """Setup/teardown для одного ifname (на любом уровне вложенности пейлоада)."""
    if field_value in handled_ifnames:
        return

    lifecycle = _resolve_auto_interface(field_name, field_value, iface_rules)
    if not lifecycle:
        return
    if lifecycle.get("physical"):
        logger.debug(f"Пропуск lifecycle для физического интерфейса: {field_value}")
        handled_ifnames.add(field_value)
        return

    var_name = _interface_var_name(field_value)
    requirements = lifecycle.get("requirements", frozenset())
    if lifecycle.get("setup") or lifecycle.get("create"):
        _append_lifecycle_setup(
            scenario, target_endpoint, field_name, field_value,
            lifecycle, variables, var_name, main_action=main_action,
            lifecycle_requirements=requirements,
            template_ifname=field_value,
            setup_phase="interface",
            field_config=lifecycle,
            env_file=env_file,
            vid_pool=vid_pool,
            endpoint_rules=endpoint_rules,
        )
    elif "setup" in requirements:
        logger.warning(
            f"requirements содержит setup, но шаги не заданы для ifname={field_value}"
        )

    if lifecycle.get("teardown") or lifecycle.get("delete"):
        teardown_priority = int(
            lifecycle.get("teardown_priority", _TEARDOWN_PRIORITY_INTERFACE),
        )
        _append_lifecycle_teardown(
            scenario, field_name, field_value, lifecycle, variables,
            lifecycle_requirements=requirements,
            teardown_priority=teardown_priority,
            template_ifname=field_value,
            env_file=env_file,
            vid_pool=vid_pool,
        )
    elif "teardown" in requirements:
        logger.warning(
            f"requirements содержит teardown, но шаги не заданы для ifname={field_value}"
        )

    handled_ifnames.add(field_value)


def _interface_deferred_prefixes(iface_rules: dict) -> frozenset[str]:
    """
    prefix'ы с setup_defer: true в interface_rules — их lifecycle откладывается
  (например bond создаётся после eth-vlan slave).
    """
    deferred: set[str] = set()
    for rule in _normalize_interface_rules(iface_rules.get("ifname", {})):
        if rule.get("setup_defer") and (prefix := rule.get("prefix")):
            deferred.add(prefix)
    return frozenset(deferred)


def _interface_setup_sort_key(ifname: str, iface_rules: dict) -> tuple[int, str]:
    deferred = _interface_deferred_prefixes(iface_rules)
    for prefix in deferred:
        if _matches_interface_prefix(ifname, prefix):
            return (1, ifname)
    return (0, ifname)


def _scan_payload_for_interfaces(
    obj,
    scenario,
    target_endpoint,
    iface_rules,
    variables,
    handled_ifnames: set,
    main_action=None,
    *,
    env_file: dict | None = None,
    vid_pool: _VidPool | None = None,
    endpoint_rules: dict | None = None,
    request_schema: dict | None = None,
    lifecycle_config: dict | None = None,
    openapi_components: dict | None = None,
):
    """
    Ищет имена интерфейсов в payload и применяет lifecycle.
    Порядок: сначала не-bond (prefix из dependencies.json), затем bond.
    """
    candidates: list[tuple[int, str, str]] = []
    _collect_interface_candidates(
        obj, iface_rules, candidates,
        request_schema=request_schema,
        openapi_components=openapi_components,
        lifecycle_config=lifecycle_config,
    )
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for _idx, field_name, value in sorted(
        candidates,
        key=lambda item: (
            _interface_setup_sort_key(item[2], iface_rules),
            item[0],
        ),
    ):
        key = (field_name, value)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)

    for field_name, value in ordered:
        if value in handled_ifnames:
            continue
        _apply_interface_lifecycle(
            scenario,
            target_endpoint,
            field_name,
            value,
            iface_rules,
            variables,
            handled_ifnames,
            main_action=main_action,
            env_file=env_file,
            vid_pool=vid_pool,
            endpoint_rules=endpoint_rules,
        )


def _apply_field_mapping_dependencies(
    scenario,
    target_endpoint,
    deps,
    main_action,
    variables,
    *,
    prerequisites_only: bool,
    env_file: dict | None = None,
    vid_pool: _VidPool | None = None,
    endpoint_rules: dict | None = None,
):
    for dep_path, dep_info in deps.items():
        field = dep_info["field"]
        value = dep_info["value"]
        config = dep_info["config"]
        if _should_skip_field_mapping(config, target_endpoint):
            logger.debug(
                f"Skip field_mapping {field} for {target_endpoint} (skip_targets)",
            )
            continue
        is_prerequisite = _is_prerequisite_field(config, target_endpoint)
        if prerequisites_only != is_prerequisite:
            continue
        phases = _field_lifecycle_phases(main_action, config, target_endpoint)
        logger.debug(
            f"Lifecycle {field} @ {dep_path}: phases={sorted(phases)} "
            f"({'prerequisite' if is_prerequisite else 'field'})"
        )
        _apply_field_lifecycle(
            scenario, target_endpoint, field, value, config,
            main_action, variables, phases,
            setup_phase="prerequisite" if is_prerequisite else "field",
            env_file=env_file,
            vid_pool=vid_pool,
            endpoint_rules=endpoint_rules,
        )


# =============================================================================
# ФОРМИРОВАНИЕ ТЕСТ-СЦЕНАРИЕВ (С INTERFACE_RULES + SELF-SKIP ДЛЯ SETUP)
# =============================================================================
def build_test_scenarios(
    target_endpoint, method, payload_records, dependencies_config,
    request_schema: dict | None = None,
    ollama: OllamaOrchestrator | None = None,
    expected_coverage: set[str] | None = None,
    env_file: dict | None = None,
    openapi_components: dict | None = None,
):
    logger.info(f"Формирую тест-сценарии для {len(payload_records)} пейлоадов...")
    filepath = endpoint_to_test_file(target_endpoint, Path("tests"), method)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    dep_map = dependencies_config.get("field_mappings", dependencies_config)
    iface_rules = dependencies_config.get("interface_rules", {})
    endpoint_rules = dependencies_config.get("endpoint_rules", {})
    mock_by_field = (parse_mock_data_config(dependencies_config) or {}).get(
        "by_field", {},
    )
    synthetic_bind_fields = dependencies_config.get("synthetic_bind_fields", {})
    auto_scalar_delete = (
        detect_scalar_delete_action_pattern(request_schema)
        if request_schema else None
    )
    if auto_scalar_delete:
        logger.info(
            "Авто lifecycle (scalar delete): id_field="
            f"{auto_scalar_delete['id_field']}"
        )
    scenarios = []
    vid_pool = _VidPool(env_file)
    env_file = env_file or {}
    lifecycle_config = parse_interface_lifecycle_config(dependencies_config)
    if lifecycle_config and openapi_components is None:
        openapi_components = load_openapi_components()
    if lifecycle_config:
        lifecycle_config = {
            **lifecycle_config,
            "skip_field_keys": lifecycle_config.get("exclude_fields", frozenset()),
        }

    for record in payload_records:
        payload = synchronize_vid_ifname(record.payload, request_schema)
        main_payload = copy.deepcopy(payload)
        variables = {_PLACEHOLDER_CONTEXT_KEY: copy.deepcopy(payload)}

        scenario = {
            "test_id": 0,
            "coverage_keys": sorted(set(record.coverage_keys)),
            "setup": [],
            "main_test": {
                "endpoint": target_endpoint,
                "method": method.upper(),
                "payload": main_payload,
                "expected_status": 200
            },
            "teardown": []
        }

        main_action, action_data = _extract_main_action(payload)
        if main_action:
            logger.debug(f"main_test action: {main_action}")
            _collect_bind_vars(action_data, variables)
        else:
            _collect_bind_vars(payload, variables)

        deps = scan_payload_for_dependencies(payload, dep_map)
        _inject_synthetic_field_dependencies(
            deps,
            variables,
            target_endpoint=target_endpoint,
            main_action=main_action,
            dep_map=dep_map,
            mock_by_field=mock_by_field,
            synthetic_fields=synthetic_bind_fields,
        )

        # =====================================================================
        # 1. PREREQUISITE field_mappings (vrf, dhcp pool, …)
        # =====================================================================
        _apply_field_mapping_dependencies(
            scenario, target_endpoint, deps, main_action, variables,
            prerequisites_only=True,
            env_file=env_file,
            vid_pool=vid_pool,
            endpoint_rules=endpoint_rules,
        )

        # =====================================================================
        # 2. INTERFACE_RULES (создание ifname; bond — после остальных)
        # =====================================================================
        handled_ifnames = set()
        _scan_payload_for_interfaces(
            payload, scenario, target_endpoint, iface_rules,
            variables, handled_ifnames, main_action,
            env_file=env_file,
            vid_pool=vid_pool,
            endpoint_rules=endpoint_rules,
            request_schema=request_schema,
            lifecycle_config=lifecycle_config,
            openapi_components=openapi_components,
        )

        # =====================================================================
        # 3. FIELD_MAPPINGS (enslave, acl lifecycle, …)
        # =====================================================================
        _apply_field_mapping_dependencies(
            scenario, target_endpoint, deps, main_action, variables,
            prerequisites_only=False,
            env_file=env_file,
            vid_pool=vid_pool,
            endpoint_rules=endpoint_rules,
        )

        # =====================================================================
        # ENDPOINT_RULES: setup/teardown самого тестируемого API
        # =====================================================================
        _apply_endpoint_rules_lifecycle(
            scenario, target_endpoint, payload, main_action, action_data,
            endpoint_rules, variables, mock_by_field=mock_by_field,
        )

        # =====================================================================
        # АВТО LIFECYCLE: action.add (object) + action.delete (scalar)
        # =====================================================================
        _apply_auto_scalar_delete_lifecycle(
            scenario, target_endpoint, main_action, payload,
            auto_scalar_delete, endpoint_rules, variables, deps,
        )

        # =====================================================================
        # ПОДСТАНОВКА РЕАЛЬНЫХ ЗНАЧЕНИЙ
        # =====================================================================
        scenario["main_test"]["payload"] = _replace_placeholders(main_payload, variables)
        for setup_step in scenario["setup"]:
            setup_step["payload"] = _replace_placeholders(setup_step["payload"], variables)
        for teardown_step in scenario["teardown"]:
            teardown_step["payload"] = _replace_placeholders(teardown_step["payload"], variables)

        # =====================================================================
        # DESCRIPTION (OPTIONAL OLLAMA)
        # =====================================================================
        default_description = f"Auto-test: {method.upper()} {target_endpoint}"
        if ollama and ollama.has_feature("describe"):
            try:
                scenario["description"] = ollama.generate_test_description(scenario)
            except Exception as e:
                logger.warning(f"Failed to generate description: {e}")
                scenario["description"] = default_description
        else:
            scenario["description"] = default_description

        for teardown_step in scenario["teardown"]:
            teardown_step.pop("expected_status", None)

        _sort_scenario_setup(scenario)
        _sort_scenario_teardown(scenario)

        scenarios.append(scenario)

    scenarios.sort(key=lambda item: _coverage_sort_key(item["coverage_keys"]))
    for idx, scenario in enumerate(scenarios, 1):
        scenario["test_id"] = idx

    if expected_coverage:
        present = {
            key
            for scenario in scenarios
            for key in scenario["coverage_keys"]
        }
        missing = expected_coverage - present
        matched = expected_coverage & present
        logger.info(
            "Coverage keys in tests: %d/%d",
            len(matched),
            len(expected_coverage),
        )
        if missing:
            logger.warning("Missing coverage keys (%d):", len(missing))
            for key in sorted(missing)[:20]:
                logger.warning("  • %s", key)
            if len(missing) > 20:
                logger.warning("  … and %d more", len(missing) - 20)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(scenarios, f, indent=2, ensure_ascii=False)
    logger.info(f"Сценарий сохранён: {filepath} ({len(scenarios)} тестов)")
    return filepath


# =============================================================================
# CLI
# =============================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Генератор REST API тестов из OpenAPI-схемы",
    )
    parser.add_argument(
        "-e",
        "--endpoint",
        nargs="+",
        metavar="PATH",
        help="Эндпоинт или список эндпоинтов (POST). Без -e/-d — все POST из openapi.json",
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
        help="Debug-режим логирования (logs/gen_*.log)",
    )
    parser.add_argument(
        "-c",
        "--compact-coverage",
        action="store_true",
        help="Компактное покрытие: большие enum (>10) → 3 значения вместо всех",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Параллельная генерация в N процессах (только при 2+ эндпоинтах; по умолчанию 1)",
    )
    parser.add_argument(
        "--ollama",
        action="store_true",
        help="Ollama: описания тестов и читаемые имена ресурсов (английский)",
    )
    parser.add_argument(
        "--ollama-features",
        metavar="LIST",
        help="Фичи Ollama через запятую: describe, enrich (по умолчанию: describe,enrich)",
    )
    return parser.parse_args(argv)


def _normalize_endpoint(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def discover_post_endpoints(openapi_data: dict) -> list[str]:
    return sorted(
        path
        for path, methods in openapi_data.get("paths", {}).items()
        if isinstance(methods, dict) and "post" in methods
    )


def resolve_target_endpoints(requested: list[str] | None, all_endpoints: list[str]) -> list[str]:
    if not requested:
        return all_endpoints

    normalized = [_normalize_endpoint(ep) for ep in requested]
    known = set(all_endpoints)
    invalid = [ep for ep in normalized if ep not in known]
    if invalid:
        raise SystemExit(
            f"Эндпоинты не найдены в openapi.json (POST): {', '.join(invalid)}"
        )
    return normalized


def _normalize_prefix(prefix: str) -> str:
    normalized = _normalize_endpoint(prefix).rstrip("/")
    return normalized or "/"


def resolve_endpoints_by_prefix(
    prefixes: list[str],
    all_endpoints: list[str],
) -> list[str]:
    """Эндпоинты, совпадающие с префиксом: /interfaces → /interfaces, /interfaces/..."""
    normalized = [_normalize_prefix(prefix) for prefix in prefixes]
    matched: list[str] = []
    for endpoint in all_endpoints:
        for prefix in normalized:
            if endpoint == prefix or endpoint.startswith(f"{prefix}/"):
                matched.append(endpoint)
                break
    if not matched:
        raise SystemExit(
            "Нет POST-эндпоинтов с префиксом: "
            + ", ".join(normalized)
        )
    return matched


def resolve_run_endpoints(
    *,
    requested: list[str] | None,
    dir_prefixes: list[str] | None,
    all_endpoints: list[str],
) -> list[str]:
    if requested and dir_prefixes:
        raise SystemExit("Укажите либо -e, либо -d, но не оба одновременно")
    if dir_prefixes:
        return resolve_endpoints_by_prefix(dir_prefixes, all_endpoints)
    return resolve_target_endpoints(requested, all_endpoints)


def generate_single_endpoint(
    target_endpoint: str,
    dependencies: dict,
    interface_inventory: dict | None,
    *,
    compact_coverage: bool,
    ollama: OllamaOrchestrator,
    env_file: dict | None = None,
) -> str:
    """Генерирует тесты для одного POST-эндпоинта. Возвращает путь эндпоинта."""
    method = "post"
    start = time.time()
    logger.info(f"Целевой эндпоинт: {method.upper()} {target_endpoint}")

    resolved_endpoint = ResolveScheme.resolve_endpoint(
        openapi_file="openapi.json", endpoint_path=target_endpoint, method=method
    )
    request_schema = resolved_endpoint['requestBody']['content']['application/json']['schema']

    clean_schema = preprocess_schema_for_jsf(request_schema)
    blocked_parents = build_eth_parents_with_vlan_children(interface_inventory or [])
    clean_schema = apply_interface_inventory(
        clean_schema, interface_inventory, blocked_eth_parents=blocked_parents,
    )
    mock_config = parse_mock_data_config(dependencies)
    if mock_config:
        pattern_index = build_mock_pattern_index(
            load_openapi_components(), mock_config,
        )
        clean_schema = apply_mock_data(clean_schema, mock_config, pattern_index)
        logger.info(
            "mock_data: by_schema=%d, by_field=%d",
            len(mock_config.get("by_schema", {})),
            len(mock_config.get("by_field", {})),
        )
    logger.debug("Схема препроцессирована для JSF")

    arguments = ResolveScheme.find_all_patterns_min_max(schema=clean_schema)
    logger.info(f"Извлечены правила: {json.dumps(arguments, indent=2)}")

    all_expected_fields = extract_all_fields(clean_schema)
    logger.info(
        f"Ожидается покрытие {len(all_expected_fields)} полей: "
        f"{sorted(all_expected_fields)}"
    )

    faker = JSF(clean_schema)

    expected_coverage = build_coverage_expectations(
        clean_schema, compact=compact_coverage,
    )
    final_payloads = generate_value_coverage_payloads(
        clean_schema, compact=compact_coverage,
    )
    covered_fields = set()
    for record in final_payloads:
        covered_fields.update(get_payload_fields(record.payload))

    missing = all_expected_fields - covered_fields
    if missing:
        logger.info(
            f"После покрытия значений не хватает полей: {sorted(missing)}. "
            "Добираю случайной генерацией..."
        )

    max_attempts = 50
    for i in range(max_attempts):
        if not missing:
            break
        try:
            payload = _coerce_payload_to_schema(faker.generate(), clean_schema)
            valid, reason = _validate_payload(payload, clean_schema)
            if not valid:
                logger.debug(f"Пропуск JSF-добора: {reason}")
                continue
            fill_key = f"__field_fill__:{','.join(sorted(missing))}"
            final_payloads.append(PayloadCoverage(payload, [fill_key]))
            covered_fields.update(get_payload_fields(payload))
            missing = all_expected_fields - covered_fields
            if not missing:
                logger.info(
                    f"100% покрытие полей достигнуто за {len(final_payloads)} пейлоадов"
                )
                break
            if (i + 1) % 5 == 0:
                logger.info(
                    f"Попытка {i+1}/{max_attempts} | "
                    f"Поля: {len(covered_fields)}/{len(all_expected_fields)} | "
                    f"Осталось: {sorted(missing)}"
                )
        except Exception as e:
            logger.warning(f"Ошибка генерации на попытке {i+1}: {e}. Пропускаю.")
            continue
    else:
        if missing:
            logger.warning(
                f"Достигнут лимит ({max_attempts}). Не покрыты поля: {sorted(missing)}"
            )

    final_payloads = dedupe_payloads(final_payloads)
    logger.info(f"Итого уникальных пейлоадов: {len(final_payloads)}")

    if ollama.has_feature("enrich"):
        field_schemas = ResolveScheme.extract_field_schemas(clean_schema)
        enriched_payloads = ollama.enrich_payloads(
            [record.payload for record in final_payloads],
            clean_schema,
            field_schemas,
            target_endpoint,
            method=method.upper(),
        )
        final_payloads = [
            PayloadCoverage(payload, record.coverage_keys)
            for payload, record in zip(enriched_payloads, final_payloads)
        ]

    build_test_scenarios(
        target_endpoint, method, final_payloads, dependencies,
        request_schema=clean_schema,
        ollama=ollama,
        expected_coverage=expected_coverage,
        env_file=env_file,
    )

    logger.info(
        f"Эндпоинт {target_endpoint}: готово за {time.time() - start:.2f} сек."
    )
    return target_endpoint


def _generate_endpoint_task(job: dict) -> _EndpointTaskResult:
    log_buffer = _configure_worker_capture_logging(job["verbose"])
    try:
        ollama = OllamaOrchestrator.from_cli(job["use_ollama"], job["ollama_features"])
        endpoint = generate_single_endpoint(
            job["target_endpoint"],
            job["dependencies"],
            job["interface_inventory"],
            compact_coverage=job["compact_coverage"],
            ollama=ollama,
            env_file=job.get("env_file"),
        )
        return _EndpointTaskResult(endpoint, log_buffer)
    except Exception as exc:
        logger.exception("Ошибка генерации %s", job["target_endpoint"])
        return _EndpointTaskResult(job["target_endpoint"], log_buffer, error=exc)


# =============================================================================
# MAIN
# =============================================================================
def main(argv: list[str] | None = None):
    args = parse_args(argv)
    if args.workers < 1:
        raise SystemExit("--workers должен быть >= 1")
    # Имя лога зависит от -e/-d: logs/gen_<datetime>_<scope>.log
    # (all | interfaces | dns_client | ...)
    log_path = build_generation_log_path(
        endpoints=args.endpoint,
        dir_prefixes=args.dir,
    )
    configure_logging(debug=args.verbose, log_file=log_path)

    logger.info("Запуск генератора тестов...")
    logger.info(f"Лог: {log_path.as_posix()}")
    start_main = time.time()
    ollama = OllamaOrchestrator.from_cli(args.ollama, args.ollama_features)
    
    # Открываем файл с зависимостями
    with open("dependencies.json", "r", encoding="utf-8") as f:
        dependencies = json.load(f)
    logger.debug("dependencies.json загружен")

    # Загружаем переменные окружения
    env_vars = load_env_file()
    interface_inventory = build_interface_inventory(dependencies, env_vars) # Строим инвентарь интерфейсов
    if interface_inventory:
        logger.info(
            "Инвентарь интерфейсов: "
            f"{json.dumps(interface_inventory, ensure_ascii=False)}"
        )
    else:
        logger.info("Инвентарь интерфейсов не задан (.env / allowed в dependencies.json)")

    with open("openapi.json", "r", encoding="utf-8") as file:
        data = json.load(file)

    post_endpoints = discover_post_endpoints(data)
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

    effective_workers = args.workers if len(endpoints) > 1 else 1
    if len(endpoints) <= 1 and args.workers > 1:
        logger.info(
            f"--workers={args.workers} игнорируется: выбран 1 эндпоинт, "
            "параллельная генерация не используется"
        )
    elif effective_workers > 1:
        effective_workers = min(effective_workers, len(endpoints))
        logger.info(f"Параллельная генерация: {effective_workers} процесс(ов)")

    try:
        if effective_workers > 1:
            jobs = [
                {
                    "target_endpoint": target_endpoint,
                    "dependencies": dependencies,
                    "interface_inventory": interface_inventory,
                    "compact_coverage": args.compact_coverage,
                    "use_ollama": args.ollama,
                    "ollama_features": args.ollama_features,
                    "verbose": args.verbose,
                    "env_file": env_vars,
                }
                for target_endpoint in endpoints
            ]
            with ProcessPoolExecutor(max_workers=effective_workers) as pool:
                futures = {
                    pool.submit(_generate_endpoint_task, job): job["target_endpoint"]
                    for job in jobs
                }
                for future in as_completed(futures):
                    result = future.result()
                    _append_log_block(result.log_lines)
                    if result.error is not None:
                        raise result.error
        else:
            for target_endpoint in endpoints:
                try:
                    generate_single_endpoint(
                        target_endpoint,
                        dependencies,
                        interface_inventory,
                        compact_coverage=args.compact_coverage,
                        ollama=ollama,
                        env_file=env_vars,
                    )
                except Exception as e:
                    logger.critical(f"Критическая ошибка в main: {e}", exc_info=True)
                    raise
    finally:
        _write_generation_summary(start_main, len(endpoints))


if __name__ == "__main__":
    try:
        main() # Запускаем главную функцию
    except Exception as e:
        logger.critical(f"Критическая ошибка в main: {e}", exc_info=True)
        raise SystemExit(1)
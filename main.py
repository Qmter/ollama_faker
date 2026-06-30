import json
import os
import re
import copy
import time
import logging
import argparse
from dataclasses import dataclass
from pathlib import Path
from jsf import JSF
from ollama_orchestrator import OllamaOrchestrator
from resolve_scheme import ResolveScheme

# =============================================================================
# ГЛОБАЛЬНАЯ НАСТРОЙКА ЛОГИРОВАНИЯ
# =============================================================================
logger = logging.getLogger("MAIN")


def configure_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        filename="test.log",
        filemode="w",
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


# =============================================================================
# ПРЕПРОЦЕССИНГ СХЕМЫ ДЛЯ JSF
# =============================================================================
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
                new_schema.setdefault('type', 'string')
                logger.debug(f"{keyword} с const → enum: {consts}")
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
            for branch in schema['oneOf']:
                if isinstance(branch, dict) and branch.get('type') == 'null':
                    continue
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
    """Находит ветку oneOf/anyOf, которой принадлежит поле (с учётом const/enum)."""
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
        for branch in candidates:
            prop_schema = branch.get('properties', {}).get(field_name, {})
            if 'const' in prop_schema and prop_schema['const'] != field_value:
                continue
            if 'enum' in prop_schema and field_value not in prop_schema['enum']:
                continue
            return branch

    return candidates[0]


def _schema_has_composition(schema: dict) -> bool:
    return any(k in schema for k in ('oneOf', 'anyOf', 'allOf'))


_NUMERIC_BOUNDS = ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum')
_STRING_BOUNDS = ('minLength', 'maxLength')
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
    try:
        return _jsf_generate(prop_schema)
    except Exception:
        return None


def _minimal_object_composed(schema: dict, exclude: str | None = None) -> dict:
    """
    Минимальный валидный object: required из properties + одна ветка oneOf/anyOf/allOf.
    Поддерживает схемы вида {required: [acl_name], properties: {acl_name}, oneOf: [...]}.
    """
    if not isinstance(schema, dict) or _resolve_schema_type(schema) != 'object':
        return {}

    props = schema.get('properties', {})
    result = {}

    for req in schema.get('required', []):
        if req == exclude:
            continue
        if req in props:
            result[req] = _minimal_prop_value(props[req])

    if not _schema_has_composition(schema):
        return _coerce_payload_to_schema(result, schema)

    branch = None
    if exclude and exclude not in props:
        branch = _find_oneof_branch_for_field(schema, exclude, None)
    if branch is None:
        branch = next(_iter_schema_branches(schema), None)
    if branch is None:
        return _coerce_payload_to_schema(result, schema)

    branch_props = branch.get('properties', {})
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
    result.update(branch_obj)
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
    """Объект на уровне schema с field=value и обязательными соседями."""
    props = schema.get('properties', {})
    result = {}

    for req in schema.get('required', []):
        if req == field:
            continue
        if req in props:
            result[req] = _minimal_prop_value(props[req])

    if field in props:
        result[field] = _assign_object_value(props[field], value)
        if _schema_has_composition(schema):
            branch = next(_iter_schema_branches(schema), None)
            if branch:
                branch_obj = _minimal_object_composed(branch)
                for key, val in branch_obj.items():
                    result.setdefault(key, val)
        return _coerce_payload_to_schema(result, schema)

    if _schema_has_composition(schema):
        branch = _find_oneof_branch_for_field(schema, field, value)
        if branch:
            branch_obj = _minimal_object_composed(branch, exclude=field)
            branch_obj[field] = value
            result.update(branch_obj)
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

    if _schema_has_composition(schema):
        values = []
        for keyword in ('oneOf', 'anyOf'):
            for branch in schema.get(keyword, []):
                if isinstance(branch, dict) and branch.get('type') == 'null':
                    if None not in values:
                        values.append(None)
        for branch in _iter_schema_branches(schema):
            for candidate in collect_test_values(branch):
                if candidate not in values:
                    values.append(candidate)
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
        return True, ""
    except ImportError:
        return True, ""
    except Exception as e:
        message = str(e).split("\n", 1)[0]
        return False, message


def build_minimal_payload(schema: dict) -> dict:
    """Пейлоад только с required-полями с учётом composition."""
    if _resolve_schema_type(schema) != 'object':
        return _coerce_payload_to_schema(_jsf_generate(schema), schema)
    return _minimal_object_composed(schema)


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


def _apply_field_lifecycle(scenario, target_endpoint, field, value, config,
                           main_action, variables, phases: set):
    """Setup/teardown для одного field_mapping с учётом phases."""
    if config.get("optional") and value in (None, "", []):
        return

    lifecycle = _resolve_field_lifecycle(config, field)
    var_name = f"created_{field}"

    if "setup" in phases:
        _append_lifecycle_setup(
            scenario, target_endpoint, field, value,
            lifecycle, variables, var_name, main_action=main_action,
        )
    else:
        variables.setdefault(var_name, value)

    if "teardown" in phases:
        _append_lifecycle_teardown(
            scenario, field, value, lifecycle, variables,
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

    rules = (
        endpoint_rules.get(target_endpoint)
        or endpoint_rules.get(target_endpoint.rstrip("/"))
        or {}
    )
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
            "expected_status": 200,
            "note": f"Auto-teardown: delete {id_field}={id_value}",
        })
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
        scenario["setup"].append({
            "endpoint": target_endpoint,
            "method": "POST",
            "payload": setup_payload,
            "expected_status": 200,
            "note": f"Auto-setup: add {id_field}={delete_value}",
        })
        variables["delete"] = delete_value
        variables[id_field] = delete_value
        logger.debug(f"Auto-setup before scalar delete: {id_field}={delete_value}")


def _apply_endpoint_action_lifecycle(scenario, target_endpoint, main_action,
                                     action_data, endpoint_rules, variables):
    """Setup/teardown самого эндпоинта из endpoint_rules по action main_test."""
    if not main_action:
        return

    rules = endpoint_rules.get(target_endpoint) or endpoint_rules.get(target_endpoint.rstrip("/"))
    if not rules:
        return

    action_rules = rules.get(main_action, {})
    bind_fields = rules.get("bind_fields", [])
    bind_vars = _collect_bind_vars(action_data)
    if bind_fields:
        bind_vars = {k: bind_vars[k] for k in bind_fields if k in bind_vars}
    variables.update(bind_vars)

    for phase in ("setup", "teardown"):
        for step_def in _as_lifecycle_list(action_rules.get(phase)):
            if not isinstance(step_def, dict) or "endpoint" not in step_def:
                continue
            step = copy.deepcopy(step_def)
            if "note" not in step:
                step["note"] = f"{phase}: action {main_action} on {target_endpoint}"
            _append_custom_lifecycle_step(scenario, phase, step, variables)
            logger.debug(f"Endpoint {phase} ({main_action}): {step['endpoint']}")


# =============================================================================
# ПОДСТАНОВКА ПЕРЕМЕННЫХ В ОБЪЕКТЕ (рекурсивно)
# =============================================================================
_PLACEHOLDER_CONTEXT_KEY = "__placeholder_context__"
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


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


def _resolve_placeholder(name: str, variables: dict, context: dict | None) -> str | None:
    name = name.strip()
    if not name or name.startswith("__"):
        return None
    if name in variables:
        value = variables[name]
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    if context is None:
        return None
    if "." in name:
        value = _get_nested_placeholder_value(context, name)
    elif name in context:
        value = context[name]
    else:
        return None
    if value is not None and not isinstance(value, (dict, list)):
        return str(value)
    return None


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
        def _substitute(match: re.Match) -> str:
            resolved = _resolve_placeholder(match.group(1), variables, context)
            return resolved if resolved is not None else match.group(0)

        return _PLACEHOLDER_RE.sub(_substitute, obj)
    return obj


def _lifecycle_vars(field_name, field_value, variables: dict | None = None) -> dict:
    """Переменные для {{placeholder}} в setup/teardown."""
    result = {field_name: field_value, "ifname": field_value} # Создаем словарь с переменными
    if variables:
        result.update(variables) # Обновляем словарь с переменными
    return result # Возвращаем словарь с переменными


def _append_custom_lifecycle_step(scenario, phase, step_def, variables):
    """Добавляет кастомный setup/teardown из dependencies.json."""
    payload_template = step_def.get("payload", {}) # Получаем шаблон пейлоада
    payload = _replace_placeholders(copy.deepcopy(payload_template), variables) # Заменяем значения в шаблоне пейлоада
    step = {
        "endpoint": step_def["endpoint"], # Добавляем endpoint в шаг
        "method": step_def.get("method", "POST").upper(),
        "payload": payload,
        "expected_status": step_def.get("expected_status", 200),
    }
    if note := step_def.get("note"): # Если есть note
        step["note"] = note # Добавляем note в шаг
    if phase == "setup": # Если фаза setup
        if extract_var := step_def.get("extract_to_variable"): # Если есть extract_to_variable
            step["extract_to_variable"] = extract_var # Добавляем extract_to_variable в шаг
        if extract_path := step_def.get("response_extract"): # Если есть response_extract
            step["response_extract"] = extract_path # Добавляем response_extract в шаг
    scenario[phase].append(step) # Добавляем шаг в фазу


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
        "expected_status": 200, # Добавляем expected_status в шаг
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


def _append_lifecycle_setup(scenario, target_endpoint, field_name, field_value,
                            lifecycle, variables, var_name, main_action=None):
    """Setup: один или несколько шагов (setup / create)."""
    steps = list(_iter_setup_steps(lifecycle, field_name, field_value))
    if not steps:
        return

    extract_assigned = False
    vars_ = _lifecycle_vars(field_name, field_value, variables)

    for step_def in steps:
        endpoint = step_def["endpoint"]
        is_self = endpoint.rstrip("/") == target_endpoint.rstrip("/")
        if is_self and main_action != "delete":
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
            scenario["setup"].append({
                "endpoint": step["endpoint"], # Добавляем endpoint в шаг
                "method": step.get("method", "POST").upper(), # Добавляем method в шаг
                "payload": step["payload"], # Добавляем payload в шаг
                "expected_status": step.get("expected_status", 200), # Добавляем expected_status в шаг
                "extract_to_variable": step["extract_to_variable"], # Добавляем extract_to_variable в шаг
                "response_extract": step["response_extract"], # Добавляем response_extract в шаг
            })
            logger.debug(f"Setup (create) для {field_name}={field_value} → {endpoint}") # Логируем setup (create)
        else: # Если нет _default_create
            _append_custom_lifecycle_step(scenario, "setup", step, vars_) # Добавляем кастомный шаг setup
            logger.debug(f"Кастомный setup для {field_name}={field_value} → {endpoint}") # Логируем кастомный setup

    variables[var_name] = field_value


def _append_lifecycle_teardown(scenario, field_name, field_value, lifecycle, variables):
    """Teardown: один или несколько шагов (teardown / delete)."""
    steps = list(_iter_teardown_steps(lifecycle, field_name, field_value))
    if not steps: # Если нет шагов
        return

    vars_ = _lifecycle_vars(field_name, field_value, variables) # Создаем переменную для проверки, есть ли extract_to_variable

    for step_def in steps: # Для каждого шага
        endpoint = step_def["endpoint"] # Получаем endpoint
        step = copy.deepcopy(step_def) # Копируем шаг

        if step.pop("_default_delete", False):
            scenario["teardown"].append({
                "endpoint": step["endpoint"], # Добавляем endpoint в шаг
                "method": step.get("method", "POST").upper(), # Добавляем method в шаг
                "payload": step["payload"], # Добавляем payload в шаг
                "note": step.get("note", f"Cleanup {field_name}"), # Добавляем note в шаг
            })
            logger.debug(f"Teardown (delete) для {field_name}={field_value} → {endpoint}") # Логируем teardown (delete)
        else: # Если нет _default_delete
            _append_custom_lifecycle_step(scenario, "teardown", step, vars_) # Добавляем кастомный шаг teardown
            logger.debug(f"Кастомный teardown для {field_name}={field_value} → {endpoint}") # Логируем кастомный teardown

# =============================================================================
# АВТО-ОПРЕДЕЛЕНИЕ ИНТЕРФЕЙСОВ (pattern / prefix, порядок правил важен)
# =============================================================================
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


def _resolve_auto_interface(field_name, field_value, iface_rules):
    """
    Определяет lifecycle интерфейса по имени.
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

    for rule in _normalize_interface_rules(iface_rules[field_name]):
        matched = False
        if "pattern" in rule:
            matched = bool(re.fullmatch(rule["pattern"], field_value))
        elif "prefix" in rule:
            matched = field_value.startswith(rule["prefix"])

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
        if result:
            return result

    return None


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


def build_interface_inventory(dependencies: dict, env_file: dict | None = None) -> list:
    """
    Собирает инвентарь из interface_rules (pattern или prefix + env/allowed).
    Источники имён (по приоритету): os.environ > .env > поле "allowed".
    """
    env_file = env_file or {} 
    entries = [] # Список интерфейсов
    iface_rules = dependencies.get("interface_rules", {}) # Правила интерфейсов

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
    if not all(name.startswith(prefix) for name in names):
        return False
    try:
        return all(re.fullmatch(schema_pattern, name) for name in names)
    except re.error:
        return False


def apply_interface_inventory(schema: dict, inventory: list) -> dict:
    """Подменяет pattern → enum для узлов схемы, подходящих под инвентарь устройства."""
    if not inventory:
        return schema

    schema = copy.deepcopy(schema)

    def _walk(obj):
        if not isinstance(obj, dict):
            return
        schema_pattern = obj.get("pattern")
        if schema_pattern:
            for entry in inventory:
                if _inventory_matches_schema_pattern(entry, schema_pattern):
                    obj["enum"] = entry["names"]
                    logger.debug(
                        f"Инвентарь для pattern {schema_pattern!r}: {entry['names']}"
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


def _interface_var_name(ifname: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", ifname)
    return f"created_ifname_{safe}"


def _apply_interface_lifecycle(scenario, target_endpoint, field_name, field_value,
                               iface_rules, variables, handled_ifnames: set,
                               main_action=None):
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
    if lifecycle.get("setup") or lifecycle.get("create"):
        _append_lifecycle_setup(
            scenario, target_endpoint, field_name, field_value,
            lifecycle, variables, var_name, main_action=main_action,
        )

    if lifecycle.get("teardown") or lifecycle.get("delete"):
        _append_lifecycle_teardown(
            scenario, field_name, field_value, lifecycle, variables,
        )

    handled_ifnames.add(field_value)


# =============================================================================
# ФОРМИРОВАНИЕ ТЕСТ-СЦЕНАРИЕВ (С INTERFACE_RULES + SELF-SKIP ДЛЯ SETUP)
# =============================================================================
def build_test_scenarios(
    target_endpoint, method, payload_records, dependencies_config,
    request_schema: dict | None = None,
    ollama: OllamaOrchestrator | None = None,
    expected_coverage: set[str] | None = None,
):
    logger.info(f"Формирую тест-сценарии для {len(payload_records)} пейлоадов...")
    os.makedirs("tests", exist_ok=True)
    safe_name = target_endpoint.strip("/").replace("/", "_") + f"_{method}.json"
    filepath = Path("tests") / safe_name

    dep_map = dependencies_config.get("field_mappings", dependencies_config)
    iface_rules = dependencies_config.get("interface_rules", {})
    endpoint_rules = dependencies_config.get("endpoint_rules", {})
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

    for record in payload_records:
        payload = record.payload
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

        deps = scan_payload_for_dependencies(payload, dep_map)

        # =====================================================================
        # FIELD_MAPPINGS: setup/teardown по action в main_test
        # =====================================================================
        for dep_path, dep_info in deps.items():
            field = dep_info["field"]
            value = dep_info["value"]
            config = dep_info["config"]
            phases = _field_lifecycle_phases(main_action, config, target_endpoint)
            logger.debug(f"Lifecycle {field} @ {dep_path}: phases={sorted(phases)}")
            _apply_field_lifecycle(
                scenario, target_endpoint, field, value, config,
                main_action, variables, phases,
            )

        # =====================================================================
        # ENDPOINT_RULES: setup/teardown самого тестируемого API
        # =====================================================================
        _apply_endpoint_action_lifecycle(
            scenario, target_endpoint, main_action, action_data,
            endpoint_rules, variables,
        )

        # =====================================================================
        # АВТО LIFECYCLE: action.add (object) + action.delete (scalar)
        # =====================================================================
        _apply_auto_scalar_delete_lifecycle(
            scenario, target_endpoint, main_action, payload,
            auto_scalar_delete, endpoint_rules, variables, deps,
        )

        # =====================================================================
        # АВТО-ОБРАБОТКА ИНТЕРФЕЙСОВ (ifname на любом уровне: ifname, port[].ifname, …)
        # =====================================================================
        handled_ifnames = set()

        def _scan_for_interfaces(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    new_path = f"{path}.{k}" if path else k
                    if k in iface_rules and isinstance(v, str):
                        _apply_interface_lifecycle(
                            scenario, target_endpoint, k, v, iface_rules,
                            variables, handled_ifnames, main_action=main_action,
                        )
                    _scan_for_interfaces(v, new_path)
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _scan_for_interfaces(item, f"{path}[{i}]")

        _scan_for_interfaces(payload)

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
        help="Эндпоинт или список эндпоинтов (POST). Без -e — все POST из openapi.json",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug-режим логирования (test.log)",
    )
    parser.add_argument(
        "-c",
        "--compact-coverage",
        action="store_true",
        help="Компактное покрытие: большие enum (>10) → 3 значения вместо всех",
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


# =============================================================================
# MAIN
# =============================================================================
def main(argv: list[str] | None = None):
    args = parse_args(argv)
    configure_logging(debug=args.verbose)

    logger.info("Запуск генератора тестов...")
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
    endpoints = resolve_target_endpoints(args.endpoint, post_endpoints)
    if args.endpoint:
        logger.info(f"Выбрано эндпоинтов: {len(endpoints)}")
    else:
        logger.info(f"Все POST-эндпоинты из openapi.json: {len(endpoints)}")
    
    for target_endpoint in endpoints:
        # Метод ендпоинта
        method = "post"
        logger.info(f"Целевой эндпоинт: {method.upper()} {target_endpoint}")

        try:
            # Разрешаем схему для этого ендпоинта
            resolved_endpoint = ResolveScheme.resolve_endpoint(
                openapi_file="openapi.json", endpoint_path=target_endpoint, method=method
            )

            # Берем из разрешенной схемы тольео поле schema
            request_schema = resolved_endpoint['requestBody']['content']['application/json']['schema']
            
            # Для лучшей работы JSF отчищвем схему от OneOf, AnyOf и тд., приводя её к enum
            clean_schema = preprocess_schema_for_jsf(request_schema)
            clean_schema = apply_interface_inventory(clean_schema, interface_inventory)
            logger.debug("Схема препроцессирована для JSF")
            # print(json.dumps(clean_schema, indent=2))

            # Получаем все аргументы из схемы
            arguments = ResolveScheme.find_all_patterns_min_max(schema=clean_schema)
            logger.info(f"Извлечены правила: {json.dumps(arguments, indent=2)}")

            # Получаем аргументы необходямые для покрытия ендпоинта тестами
            all_expected_fields = extract_all_fields(clean_schema)
            logger.info(f"Ожидается покрытие {len(all_expected_fields)} полей: {sorted(all_expected_fields)}")

            # Создаем объект JSF и передаем отчищенную схему
            faker = JSF(clean_schema)

            # 1) Целенаправленное покрытие значений (enum, boolean, границы чисел)
            expected_coverage = build_coverage_expectations(
                clean_schema, compact=args.compact_coverage,
            )
            final_payloads = generate_value_coverage_payloads(
                clean_schema, compact=args.compact_coverage,
            )
            covered_fields = set() # Создаем множество для полей, которые покрыты
            for record in final_payloads:
                covered_fields.update(get_payload_fields(record.payload))

            missing = all_expected_fields - covered_fields # Получаем поля, которые не покрыты
            if missing:
                logger.info(f"После покрытия значений не хватает полей: {sorted(missing)}. Добираю случайной генерацией...")

            # 2) Добираем оставшиеся поля случайной генерацией JSF (сложные oneOf и т.п.)
            max_attempts = 50 # Максимальное количество попыток
            for i in range(max_attempts):
                if not missing:
                    break # Если поля покрыты, выходим из цикла
                try:
                    payload = _coerce_payload_to_schema(faker.generate(), clean_schema)
                    fill_key = f"__field_fill__:{','.join(sorted(missing))}"
                    final_payloads.append(PayloadCoverage(payload, [fill_key]))
                    covered_fields.update(get_payload_fields(payload))
                    missing = all_expected_fields - covered_fields
                    if not missing: # Если поля покрыты, выходим из цикла
                        logger.info(f"100% покрытие полей достигнуто за {len(final_payloads)} пейлоадов") # Логируем 100% покрытие полей
                        break
                    if (i + 1) % 5 == 0:
                        logger.info(f"Попытка {i+1}/{max_attempts} | Поля: {len(covered_fields)}/{len(all_expected_fields)} | Осталось: {sorted(missing)}")
                except Exception as e:
                    logger.warning(f"Ошибка генерации на попытке {i+1}: {e}. Пропускаю.")
                    continue
            else:
                if missing: # Если поля не покрыты, логируем ошибку
                    logger.warning(f"Достигнут лимит ({max_attempts}). Не покрыты поля: {sorted(missing)}")

            final_payloads = dedupe_payloads(final_payloads) # Убираем дубликаты пейлоадов
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

            # Строим json тест
            build_test_scenarios(
                target_endpoint, method, final_payloads, dependencies,
                request_schema=clean_schema,
                ollama=ollama,
                expected_coverage=expected_coverage,
            )
            
            logger.info(f"Генерация завершена. Общее время: {time.time() - start_main:.2f} сек.") # Логируем общее время генерации
            
        except Exception as e:
            logger.critical(f"Критическая ошибка в main: {e}", exc_info=True) # Логируем критическую ошибку
            raise


if __name__ == "__main__":
    main() # Запускаем главную функцию
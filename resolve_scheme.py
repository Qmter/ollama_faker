import copy
import json
import logging
import time
import sys

logger = logging.getLogger(__name__)

class ResolveScheme:
    @staticmethod
    def _merge_resolved_schema(base: dict, overlay: dict) -> dict:
        """
        Объединяет развёрнутую схему по $ref с sibling-ключами (OpenAPI 3 / JSON Schema).
        overlay дополняет и уточняет base: required, properties, unevaluatedProperties и т.д.
        """
        if not isinstance(base, dict):
            return copy.deepcopy(overlay) if isinstance(overlay, dict) else base
        if not isinstance(overlay, dict):
            return copy.deepcopy(base)

        result = copy.deepcopy(base)
        for key, value in overlay.items():
            if key == "properties" and isinstance(value, dict):
                merged_props = copy.deepcopy(result.get("properties", {}))
                for prop_name, prop_schema in value.items():
                    if (
                        prop_name in merged_props
                        and isinstance(prop_schema, dict)
                        and isinstance(merged_props[prop_name], dict)
                    ):
                        merged_props[prop_name] = ResolveScheme._merge_resolved_schema(
                            merged_props[prop_name], prop_schema,
                        )
                    else:
                        merged_props[prop_name] = copy.deepcopy(prop_schema)
                result["properties"] = merged_props
            elif key == "required" and isinstance(value, list):
                merged_required = list(result.get("required", []))
                for req in value:
                    if req not in merged_required:
                        merged_required.append(req)
                result["required"] = merged_required
            else:
                result[key] = copy.deepcopy(value)
        return result

    @staticmethod
    def _resolve_ref(obj, components, seen=None):
        try:
            if seen is None:
                seen = set()

            if isinstance(obj, dict):
                if "$ref" in obj:
                    ref = obj["$ref"]
                    if not isinstance(ref, str):
                        logger.warning(f"$ref не является строкой: {ref}, тип: {type(ref)}")
                        return obj

                    if ref.startswith("#/components/schemas/"):
                        name = ref.split("/")[-1]
                        if not name:
                            logger.debug(f"Имя схемы пустое в ссылке: {ref}")
                            return obj

                        key = f"schemas/{name}"
                        if key in seen:
                            logger.debug(f"Обнаружена циклическая ссылка: {ref}")
                            return {"type": "object", "x-circular": True}

                        if "schemas" not in components:
                            logger.debug(f"Компоненты не содержат ключа 'schemas': {list(components.keys())}")
                            return obj

                        schema = components["schemas"].get(name)
                        if not schema:
                            logger.warning(f"Схема '{name}' не найдена в компонентах")
                            return obj

                        seen.add(key)
                        logger.debug(f"Разрешаю $ref: {ref}")
                        resolved = ResolveScheme._resolve_ref(schema, components, seen)
                        seen.discard(key)
                        siblings = {k: v for k, v in obj.items() if k != "$ref"}
                        if not siblings:
                            return resolved
                        resolved_siblings = ResolveScheme._resolve_ref(siblings, components, seen)
                        if isinstance(resolved, dict) and isinstance(resolved_siblings, dict):
                            return ResolveScheme._merge_resolved_schema(resolved, resolved_siblings)
                        return resolved
                    else:
                        logger.debug(f"⏭Игнорирую нестандартную ссылку: {ref}")
                        return obj

                result = {}
                for k, v in obj.items():
                    try:
                        result[k] = ResolveScheme._resolve_ref(v, components, seen)
                    except Exception as e:
                        logger.error(f"Ошибка при разрешении ключа '{k}': {e}")
                        result[k] = v
                return result

            elif isinstance(obj, list):
                result = []
                for i, item in enumerate(obj):
                    try:
                        resolved_item = ResolveScheme._resolve_ref(item, components, seen)
                        result.append(resolved_item)
                    except Exception as e:
                        logger.error(f"Ошибка при разрешении элемента списка[{i}]: {e}")
                        result.append(item)
                return result

            return obj

        except Exception as e:
            logger.critical(f"Критическая ошибка в _resolve_ref: {e}", exc_info=True)
            raise

    @staticmethod
    def _extract_rules_from_schema(obj: dict) -> dict:
        """Собирает ограничения из узла JSON Schema."""
        if not isinstance(obj, dict):
            return {}

        current_rules = {}
        if 'type' in obj and isinstance(obj['type'], (str, list)):
            current_rules['type'] = obj['type']
        if 'pattern' in obj and isinstance(obj['pattern'], str):
            current_rules['pattern'] = obj['pattern']
        if 'minimum' in obj and isinstance(obj['minimum'], (int, float)):
            current_rules['minimum'] = obj['minimum']
        if 'maximum' in obj and isinstance(obj['maximum'], (int, float)):
            current_rules['maximum'] = obj['maximum']
        if 'enum' in obj and isinstance(obj['enum'], list):
            current_rules['enum'] = obj['enum']

        if 'anyOf' in obj and isinstance(obj['anyOf'], list):
            patterns = [
                item['pattern'] for item in obj['anyOf']
                if isinstance(item, dict) and 'pattern' in item and isinstance(item['pattern'], str)
            ]
            if patterns:
                current_rules['pattern'] = patterns if len(patterns) > 1 else patterns[0]

        return current_rules

    @staticmethod
    def _iter_schema_branches(schema: dict):
        """Итерирует ветки oneOf/anyOf/allOf, пропуская null."""
        if not isinstance(schema, dict):
            return
        for keyword in ('oneOf', 'anyOf', 'allOf'):
            for branch in schema.get(keyword, []):
                if isinstance(branch, dict) and branch.get('type') != 'null':
                    yield branch

    @staticmethod
    def _merge_field_schema(existing: dict, new: dict) -> dict:
        """Объединяет под-схемы одного пути из разных веток oneOf."""
        if existing == new:
            return existing
        if isinstance(existing, dict) and 'oneOf' in existing:
            branches = list(existing['oneOf'])
            if new not in branches:
                branches.append(new)
            return {'oneOf': branches}
        return {'oneOf': [existing, new]}

    @staticmethod
    def _store_field_schema(results: dict, path: str, schema: dict):
        if path in results:
            results[path] = ResolveScheme._merge_field_schema(results[path], schema)
        else:
            results[path] = schema

    @staticmethod
    def extract_field_schemas(schema, path=""):
        """
        Обходит схему и возвращает {dotted_path: sub_schema}.
        Рекурсивно раскрывает oneOf/anyOf/allOf — поля из всех веток.
        """
        results = {}
        if not isinstance(schema, dict):
            return results

        if 'properties' in schema and isinstance(schema['properties'], dict):
            for prop_name, prop_schema in schema['properties'].items():
                child_path = f"{path}.{prop_name}" if path else prop_name
                ResolveScheme._store_field_schema(results, child_path, prop_schema)
                nested = ResolveScheme.extract_field_schemas(prop_schema, child_path)
                for npath, nschema in nested.items():
                    ResolveScheme._store_field_schema(results, npath, nschema)

        if 'items' in schema and isinstance(schema['items'], dict):
            items_schema = schema['items']
            if 'properties' in items_schema:
                item_path = f"{path}[]" if path else "[]"
                nested = ResolveScheme.extract_field_schemas(items_schema, item_path)
                for npath, nschema in nested.items():
                    ResolveScheme._store_field_schema(results, npath, nschema)

        for branch in ResolveScheme._iter_schema_branches(schema):
            nested = ResolveScheme.extract_field_schemas(branch, path)
            for npath, nschema in nested.items():
                ResolveScheme._store_field_schema(results, npath, nschema)

        return results

    @staticmethod
    def find_all_patterns_min_max(schema):
        results = {}
        logger.debug("Начинаю поиск pattern/minimum/maximum/enum/type в схеме")

        for field_path, field_schema in ResolveScheme.extract_field_schemas(schema).items():
            rules = ResolveScheme._extract_rules_from_schema(field_schema)
            if rules:
                results[field_path] = rules
                logger.debug(f"Извлечены правила для поля '{field_path}': {rules}")

        logger.info(f"Извлечение правил завершено. Найдено ограничений для {len(results)} полей.")
        return results

    @staticmethod
    def resolve_endpoint(openapi_file: str, endpoint_path: str, method: str = 'post'):
        start_time = time.time()
        logger.info(f"Начинаю разрешение эндпоинта: {method.upper()} {endpoint_path} из {openapi_file}")

        try:
            method = method.lower()
            if method not in ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']:
                logger.error(f"Неподдерживаемый метод HTTP: {method}")
                sys.exit()

            import os
            if not os.path.exists(openapi_file):
                logger.error(f"Файл OpenAPI не найден: {openapi_file}")
                sys.exit()

            with open(openapi_file, 'r', encoding='utf-8') as f:
                schema = json.load(f)
            logger.debug(f"Файл OpenAPI загружен ({os.path.getsize(openapi_file)} байт)")

            if 'paths' not in schema:
                logger.error("Файл не содержит ключа 'paths'")
                sys.exit()

            paths = schema.get('paths', {})
            if endpoint_path not in paths:
                logger.error(f"Эндпоинт '{endpoint_path}' не найден.")
                sys.exit()

            if method not in paths[endpoint_path]:
                logger.error(f"Метод '{method}' не найден для '{endpoint_path}'")
                sys.exit()

            components = schema.get('components', {})
            logger.debug("Запускаю рекурсивное разрешение $ref...")
            resolved_endpoint = ResolveScheme._resolve_ref(paths[endpoint_path][method], components)
            
            elapsed = time.time() - start_time
            logger.info(f"Разрешение завершено за {elapsed:.2f} сек.")
            return resolved_endpoint

        except Exception as e:
            logger.error(f"Ошибка в resolve_endpoint: {e}", exc_info=True)
            sys.exit()
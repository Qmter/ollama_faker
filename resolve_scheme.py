import json
import logging
import time

logger = logging.getLogger(__name__)

class ResolveScheme:
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
    def find_all_patterns_min_max(schema):
        results = {}
        logger.debug("Начинаю поиск pattern/minimum/maximum/enum/type в схеме")

        def _deep_search(obj, field_name=""):
            if not isinstance(obj, dict):
                return

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

            if current_rules and field_name:
                results[field_name] = current_rules
                logger.debug(f"Извлечены правила для поля '{field_name}': {current_rules}")

            if 'properties' in obj and isinstance(obj['properties'], dict):
                for prop_name, prop_schema in obj['properties'].items():
                    _deep_search(prop_schema, prop_name)

            for key, value in obj.items():
                if key not in ('properties', 'anyOf', 'pattern', 'minimum', 'maximum', 'enum', 'type'):
                    if isinstance(value, dict):
                        _deep_search(value, field_name)
                    elif isinstance(value, list):
                        for item in value:
                            _deep_search(item, field_name)

        _deep_search(schema)
        logger.info(f"Извлечение правил завершено. Найдено ограничений для {len(results)} полей.")
        return results

    @staticmethod
    def resolve_endpoint(openapi_file: str, endpoint_path: str, method: str = 'post'):
        start_time = time.time()
        logger.info(f"Начинаю разрешение эндпоинта: {method.upper()} {endpoint_path} из {openapi_file}")

        try:
            method = method.lower()
            if method not in ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']:
                raise ValueError(f"Неподдерживаемый метод HTTP: {method}")

            import os
            if not os.path.exists(openapi_file):
                raise FileNotFoundError(f"Файл OpenAPI не найден: {openapi_file}")

            with open(openapi_file, 'r', encoding='utf-8') as f:
                schema = json.load(f)
            logger.debug(f"Файл OpenAPI загружен ({os.path.getsize(openapi_file)} байт)")

            if 'paths' not in schema:
                raise ValueError("Файл не содержит ключа 'paths'")

            paths = schema.get('paths', {})
            if endpoint_path not in paths:
                raise KeyError(f"Эндпоинт '{endpoint_path}' не найден. Доступные: {list(paths.keys())[:5]}...")

            if method not in paths[endpoint_path]:
                raise KeyError(f"Метод '{method}' не найден для '{endpoint_path}'")

            components = schema.get('components', {})
            logger.debug("Запускаю рекурсивное разрешение $ref...")
            resolved_endpoint = ResolveScheme._resolve_ref(paths[endpoint_path][method], components)
            
            elapsed = time.time() - start_time
            logger.info(f"Разрешение завершено за {elapsed:.2f} сек.")
            return resolved_endpoint

        except Exception as e:
            logger.error(f"Ошибка в resolve_endpoint: {e}", exc_info=True)
            raise
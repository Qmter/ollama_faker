import json
import os
import copy
import time
import logging
from pathlib import Path
from jsf import JSF
from resolve_scheme import ResolveScheme

# =============================================================================
# ГЛОБАЛЬНАЯ НАСТРОЙКА ЛОГИРОВАНИЯ
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"  # Дата + время
)
logger = logging.getLogger("MAIN")

# Для полной отладки раскомментируйте:
logging.getLogger().setLevel(logging.DEBUG)


# =============================================================================
# МИНИМАЛЬНЫЙ OLLAMA: ТОЛЬКО DESCRIPTION
# =============================================================================
USE_OLLAMA = False
ollama_available = False

if USE_OLLAMA:
    try:
        import requests
        
        def generate_test_description(scenario: dict) -> str:
            """Генерирует краткое описание теста через Ollama"""
            payload = {
                "model": "qwen2.5-coder:7b",
                "prompt": f"Write a single sentence in English describing this API test. Only text, no quotes or markdown:\n{json.dumps(scenario['main_test'], indent=2)}",
                "stream": False,
                "options": {"temperature": 0.3}
            }
            resp = requests.post("http://localhost:11434/api/generate", json=payload, timeout=120)
            text = resp.json().get("response", "").strip()
            # Очистка от markdown
            text = text.replace("```", "").strip()
            return text if text else "Automatically generated test description."
        
        ollama_available = True
        logger.info("Ollama: доступен (только description)")
    except Exception as e:
        logger.warning(f"Ollama недоступен: {e}. Описание будет дефолтным.")
        ollama_available = False


# =============================================================================
# ПРЕПРОЦЕССИНГ СХЕМЫ ДЛЯ JSF
# =============================================================================
def preprocess_schema_for_jsf(schema: dict) -> dict:
    if not isinstance(schema, dict):
        return schema
    new_schema = copy.deepcopy(schema)
    
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
                    new_schema.update(non_null_opts[0])
                    del new_schema[keyword]
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
# ИЗВЛЕЧЕНИЕ ВСЕХ ПОЛЕЙ ИЗ СХЕМЫ
# =============================================================================
def extract_all_fields(schema):
    fields = set()
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
# СБОР КЛЮЧЕЙ ИЗ ПЕЙЛОАДА
# =============================================================================
def get_payload_fields(payload):
    keys = set()
    if isinstance(payload, dict):
        for k, v in payload.items():
            keys.add(k)
            keys.update(get_payload_fields(v))
    elif isinstance(payload, list):
        for item in payload:
            keys.update(get_payload_fields(item))
    return keys


# =============================================================================
# ПОИСК ЗАВИСИМОСТЕЙ В ПЕЙЛОАДЕ
# =============================================================================
def scan_payload_for_dependencies(payload, dep_map, path=""):
    found = {}
    if isinstance(payload, dict):
        for k, v in payload.items():
            new_path = f"{path}.{k}" if path else k
            if k in dep_map:
                found[new_path] = {"field": k, "value": v, "config": dep_map[k]}
                logger.debug(f"Найдена зависимость: {new_path} → {k}")
            found.update(scan_payload_for_dependencies(v, dep_map, new_path))
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            found.update(scan_payload_for_dependencies(item, dep_map, f"{path}[{i}]"))
    return found


# =============================================================================
# ПОДСТАНОВКА ПЕРЕМЕННЫХ В ОБЪЕКТЕ (рекурсивно)
# =============================================================================
def _replace_placeholders(obj, variables):
    """Заменяет {{ var_name }} на реальные значения в словаре/списке/строке"""
    if isinstance(obj, dict):
        return {k: _replace_placeholders(v, variables) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_replace_placeholders(item, variables) for item in obj]
    elif isinstance(obj, str):
        for var_name, var_value in variables.items():
            obj = obj.replace(f"{{{{ {var_name} }}}}", str(var_value))
        return obj
    return obj


# =============================================================================
# ФОРМИРОВАНИЕ ТЕСТ-СЦЕНАРИЕВ
# =============================================================================
def build_test_scenarios(target_endpoint, method, raw_payloads, dependencies_config):
    logger.info(f"Формирую тест-сценарии для {len(raw_payloads)} пейлоадов...")

    # Создаем папку tests, если не существует
    os.makedirs("tests", exist_ok=True)

    # Собираем имя файла теста
    safe_name = target_endpoint.strip("/").replace("/", "_") + f"_{method}.json"

    # Формируем путь сохранения
    filepath = Path("tests") / safe_name

    # Получаем схему зависимостей
    dep_map = dependencies_config.get("field_mappings", dependencies_config)
    scenarios = []

    # Проходимся по всем сгенерированным схемам
    for idx, payload in enumerate(raw_payloads, 1):
        main_payload = copy.deepcopy(payload)
        variables = {}

        # Создаем шаблон теста и подставляем в него значения
        scenario = {
            "test_id": idx,
            "setup": [],
            "main_test": {
                "endpoint": target_endpoint,
                "method": method.upper(),
                "payload": main_payload,
                "expected_status": 200
            },
            "teardown": []
        }

        # Ищем зависимости в сгенерированной схеме
        deps = scan_payload_for_dependencies(payload, dep_map)
        
        # Формируем setup и собираем переменные
        for dep_path, dep_info in deps.items():
            field = dep_info["field"]
            value = dep_info["value"]
            config = dep_info["config"]
            
            if config.get("optional") and value in [None, "", []]:
                logger.debug(f"Пропускаю optional поле: {field}")
                continue

            provider = config["provider"]
            create_act = config.get("action_create", "add")
            extract_path = config.get("extract", f"data.{field}")
            var_name = f"created_{field}"

            setup_payload = {"action": create_act, field: value}
            scenario["setup"].append({
                "endpoint": provider, "method": "POST", "payload": setup_payload,
                "expected_status": 200, "extract_to_variable": var_name,
                "response_extract": extract_path
            })
            
            variables[var_name] = value
            logger.debug(f"Запомнил: {var_name} = {value}")

        # Подставляем реальные значения в main_test.payload
        scenario["main_test"]["payload"] = _replace_placeholders(main_payload, variables)
        
        # Формируем teardown с подставленными значениями
        for dep_path, dep_info in deps.items():
            field = dep_info["field"]
            config = dep_info["config"]
            value = dep_info["value"]
            
            if config.get("optional") and value in [None, "", []]:
                continue

            provider = config["provider"]
            delete_act = config.get("action_delete", "delete")
            var_name = f"created_{field}"

            teardown_payload = {"action": delete_act, field: variables.get(var_name, value)}
            scenario["teardown"].append({
                "endpoint": provider, "method": "POST", "payload": teardown_payload,
                "expected_status": 200, "note": f"Cleanup created {field}"
            })
            logger.debug(f"🧹 Teardown для {field}: значение = {variables.get(var_name, value)}")

        # ДОБАВЛЯЕМ DESCRIPTION ЧЕРЕZ OLLAMA (минимальная интеграция)
        if ollama_available:
            try:
                desc = generate_test_description(scenario)
                scenario["description"] = desc
                logger.info(f"Test #{idx}: description добавлен")
            except Exception as e:
                logger.warning(f"Не удалось сгенерировать description: {e}")
                scenario["description"] = f"Тест для {method.upper()} {target_endpoint}"
        else:
            scenario["description"] = f"Автотест: {method.upper()} {target_endpoint} (payload: {list(payload.keys())})"

        scenarios.append(scenario)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(scenarios, f, indent=2, ensure_ascii=False)
    logger.info(f"Сценарий сохранён: {filepath} ({len(scenarios)} тестов)")
    return filepath


# =============================================================================
# MAIN
# =============================================================================
def main():
    logger.info("Запуск генератора тестов...")
    start_main = time.time()
    
    # Открываем файл с зависимостями
    with open("dependencies.json", "r", encoding="utf-8") as f:
        dependencies = json.load(f)
    logger.debug("dependencies.json загружен")

    # Ендпоинт для теста
    target_endpoint = "/interfaces/bonding/capability" 
    
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
        logger.debug("Схема препроцессирована для JSF")

        # Получаем все аргументы из схемы
        arguments = ResolveScheme.find_all_patterns_min_max(schema=clean_schema)
        logger.info(f"Извлечены правила: {json.dumps(arguments, indent=2)}")

        # Получаем аргументы необходямые для покрытия ендпоинта тестами
        all_expected_fields = extract_all_fields(clean_schema)
        logger.info(f"Ожидается покрытие {len(all_expected_fields)} полей: {sorted(all_expected_fields)}")

        # Создаем объект JSF и передаем отчищенную схему
        faker = JSF(clean_schema)
        final_payloads = [] # Все сгенерированные схемы
        covered_fields = set() # Покрытые аргументы
        max_attempts = 50 # Максимальное количество попыток покрыть ендпоинт тестами

        # Запускаем цикл для генерации схем
        logger.info("Запуск цикла генерации до 100% покрытия...")
        for i in range(max_attempts):
            payload = faker.generate()
            final_payloads.append(payload)

            # Получаем аргументы, котоыре сгенерировались в схему
            newly_covered = get_payload_fields(payload)

            # Обновляем множество
            covered_fields.update(newly_covered)

            # Получаем какие поля необходимо еще покрыть
            missing = all_expected_fields - covered_fields
            
            if not missing:
                logger.info(f"100% покрытие достигнуто за {len(final_payloads)} генераций!")
                break
            if (i + 1) % 5 == 0:
                logger.info(f"Попытка {i+1}/{max_attempts} | Покрыто: {len(covered_fields)}/{len(all_expected_fields)} | Осталось: {sorted(missing)}")
        else:
            logger.warning(f"Достигнут лимит ({max_attempts}). Покрытие: {len(covered_fields)}/{len(all_expected_fields)}")
            if missing := all_expected_fields - covered_fields:
                logger.warning(f"   Не покрыты: {sorted(missing)}")

        # Строим json тест
        build_test_scenarios(target_endpoint, method, final_payloads, dependencies)
        
        logger.info(f"Генерация завершена. Общее время: {time.time() - start_main:.2f} сек.")
        
    except Exception as e:
        logger.critical(f"Критическая ошибка в main: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
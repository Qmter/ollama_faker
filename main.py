import json
import copy
import requests
from jsf import JSF
from resolve_scheme import ResolveScheme

# --- Конфигурация ---
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen2.5-coder:7b"

def call_ollama(prompt: str, system_prompt: str = "") -> dict:
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "system": system_prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 1024}
    }
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=120)
        response.raise_for_status()
        return json.loads(response.json().get('response', '{}'))
    except Exception as e:
        print(f"Ollama Error: {e}")
        return {}

def preprocess_schema_for_jsf(schema: dict) -> dict:
    if not isinstance(schema, dict):
        return schema
    new_schema = copy.deepcopy(schema)
    for keyword in ['oneOf', 'anyOf']:
        if keyword in new_schema:
            options = new_schema[keyword]
            consts = []
            is_all_consts = True
            for opt in options:
                if isinstance(opt, dict) and 'const' in opt:
                    consts.append(opt['const'])
                else:
                    is_all_consts = False
                    break
            
            if is_all_consts and consts:
                new_schema['enum'] = consts
                del new_schema[keyword]
                if 'type' not in new_schema:
                    new_schema['type'] = 'string'
            else:
                # Обработка null union
                has_null = any(isinstance(o, dict) and o.get('type') == 'null' for o in options)
                non_null_opts = [o for o in options if not (isinstance(o, dict) and o.get('type') == 'null')]
                if has_null and len(non_null_opts) == 1:
                    base_opt = non_null_opts[0]
                    new_schema.update(base_opt)
                    del new_schema[keyword]
                    new_schema['x-nullable'] = True
                else:
                    new_schema[keyword] = [preprocess_schema_for_jsf(opt) for opt in options]

    if 'properties' in new_schema:
        for key, val in new_schema['properties'].items():
            new_schema['properties'][key] = preprocess_schema_for_jsf(val)
    if 'items' in new_schema:
        new_schema['items'] = preprocess_schema_for_jsf(new_schema['items'])
    return new_schema

def filter_reasonable_schemas(payloads: list) -> list:
    """
    Этап 1: LLM фильтрует список payload'ов, оставляя только разумные.
    """
    print("Starting LLM filtering (Stage 1)...")
    reasonable_payloads = []
    
    # Группируем по 5 штук для экономии запросов, или по одному для точности.
    # Для простоты и точности будем проверять по одному, но быстро.
    
    system_prompt = """
    Ты - QA Expert. Оцени JSON payload для API роутера.
    Верни JSON: {"is_reasonable": true/false, "reason": "short explanation"}
    Критерии отказа:
    - Пустые обязательные массивы, если они должны содержать данные (например enslave).
    - Некорректные имена интерфейсов (не соответствуют паттернам eth/vlan/bond).
    - Бессмысленные комбинации (например null там, где нужно число).
    """

    for i, payload in enumerate(payloads):
        user_prompt = f"Payload: {json.dumps(payload)}\nIs this reasonable for a test?"
        result = call_ollama(user_prompt, system_prompt)
        
        if result.get("is_reasonable", False):
            reasonable_payloads.append(payload)
            print(f"  [{i+1}/{len(payloads)}] Kept: {payload.get('ifname')}")
        else:
            print(f"  [{i+1}/{len(payloads)}] Rejected: {result.get('reason')}")
            
    return reasonable_payloads

def generate_descriptions(payloads: list) -> list:
    """
    Этап 2: LLM придумывает описание для каждого теста.
    Возвращает список словарей: {"payload": ..., "description": ...}
    """
    print("Starting description generation (Stage 2)...")
    tests_with_desc = []
    
    system_prompt = """
    Ты - QA Expert. Придумай краткое (до 10 слов) описание теста на английском.
    Верни JSON: {"description": "string"}
    """
    
    for payload in payloads:
        user_prompt = f"Payload: {json.dumps(payload)}\nDescribe this test case."
        result = call_ollama(user_prompt, system_prompt)
        desc = result.get("description", "Auto-generated test")
        tests_with_desc.append({
            "payload": payload,
            "description": desc
        })
        print(f"  Generated desc for {payload.get('ifname')}: {desc}")
        
    return tests_with_desc

def build_final_test_structure(test_data, dependencies):
    """
    Собирает финальный JSON теста строго заданного формата.
    """
    payload = test_data["payload"]
    desc = test_data["description"]
    ifname = payload.get("ifname", "bond_unknown")
    
    deps = dependencies
    
    # 1. PRESET
    preset_block = {}
    if "preset" in deps:
        for idx, p in enumerate(deps["preset"], 1):
            ep = p["endpoint"]
            action = p.get("action", "add") # По умолчанию add для пресета
            
            # Логика подстановки ifname
            schema = {}
            if "bonding" in ep:
                schema = {"ifname": ifname}
            elif "vrf" in ep:
                # Для VRF генерируем фиктивное имя, т.к. в main payload его нет
                schema = {"vrf_name": f"vrf_{ifname}", "action": action}
                
            preset_block[str(idx)] = {
                "type": "POST",
                "endpoint": ep,
                "schema": schema,
                "errCode": 0,
                "httpCode": 200
            }

    # 2. MAIN TEST (1)
    main_step = {
        "description": desc,
        "1.1": {
            "type": "POST",
            "endpoint": "/interfaces/bonding/capability", # Хардкод текущего эндпоинта
            "schema": payload,
            "errCode": 0,
            "httpCode": 200
        }
    }
    
    # Добавляем шаг валидации (просто запрос GET без проверки тела, как просили)
    if "verify" in deps:
        main_step["1.2"] = {
            "type": "GET",
            "endpoint": deps["verify"],
            "arguments": {}, # Можно добавить аргументы если нужно
            "errCode": 0,
            "httpCode": 200
        }

    # 3. AFTER-TEST
    after_block = {}
    if "after" in deps:
        for idx, a in enumerate(deps["after"], 1):
            ep = a["endpoint"]
            action = a.get("action", "delete") # По умолчанию delete
            
            schema = {}
            if "bonding" in ep:
                schema = {"ifname": ifname}
            elif "vrf" in ep:
                schema = {"vrf_name": f"vrf_{ifname}", "action": action}
                
            after_block[str(idx)] = {
                "type": "POST",
                "endpoint": ep,
                "schema": schema,
                "errCode": 0,
                "httpCode": 200
            }

    return {
        "PRESET": preset_block,
        "1": main_step,
        "AFTER-TEST": after_block
    }

def main():
    with open("dependencies.json", "r") as f:
        dependencies = json.load(f)

    target_endpoint = "/interfaces/bonding/add"
    dep_config = dependencies.get(target_endpoint, {})

    print(f"Resolving schema for {target_endpoint}...")
    resolved_endpoint = ResolveScheme.resolve_endpoint(
        openapi_file="openapi.json",
        endpoint_path=target_endpoint,
        method="post"
    )
    
    request_schema = resolved_endpoint['requestBody']['content']['application/json']['schema']
    clean_schema = preprocess_schema_for_jsf(request_schema)
    
    # 1. Генерация 100 схем
    print("Generating 10 raw schemas with JSF...")
    faker = JSF(clean_schema)
    raw_payloads = [faker.generate() for _ in range(10)]
    
    # 2. Чистка от одинаковых (по ifname)
    seen_ifnames = set()
    unique_payloads = []
    for p in raw_payloads:
        ifname = p.get('ifname')
        if ifname and ifname not in seen_ifnames:
            seen_ifnames.add(ifname)
            unique_payloads.append(p)
            
    print(f"Unique payloads after deduplication: {len(unique_payloads)}")
    
    # 3. Этап 1: Фильтрация LLM
    reasonable_payloads = filter_reasonable_schemas(unique_payloads)
    print(f"Reasonable payloads after LLM filter: {len(reasonable_payloads)}")
    
    if not reasonable_payloads:
        print("No reasonable payloads found. Exiting.")
        return

    # 4. Этап 2: Генерация описаний
    tests_with_desc = generate_descriptions(reasonable_payloads)
    
    # 5. Сборка финальных файлов
    final_tests = []
    for test_data in tests_with_desc:
        test_json = build_final_test_structure(test_data, dep_config)
        final_tests.append(test_json)
        
    # Сохранение
    output_filename = f"{target_endpoint.replace('/', '_').strip('_')}_suite.json"
    with open(output_filename, "w") as f:
        json.dump(final_tests, f, indent=4, ensure_ascii=False)
        
    print(f"\nSuccessfully saved {len(final_tests)} tests to {output_filename}")

if __name__ == "__main__":
    main()
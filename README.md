# llm_faker

Генератор и раннер REST API тестов для маршрутизатора по OpenAPI-схеме.

1. **`main.py`** — читает `openapi.json`, строит пейлоады (JSF + покрытие enum/границ), добавляет `setup` / `main_test` / `teardown` из `dependencies.json`, пишет сценарии в `tests/`.
2. **`run_tests.py`** — последовательно выполняет сценарии на реальном устройстве.
3. **`run_unit_tests.py`** — офлайн-проверка логики генератора (без HTTP).

---

## Быстрый старт

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install jsf jsonschema requests

cp .env.example .env
# отредактируйте .env: интерфейсы, API_BASE_URL, авторизация

# 1. Сгенерировать тесты
python main.py -d /interfaces

# 2. Прогнать на устройстве
python run_tests.py -d /interfaces -v

# 3. Проверить логику генератора (без API)
python run_unit_tests.py -v
```

---

## Структура проекта

| Файл / каталог | Назначение |
|----------------|------------|
| `main.py` | Генерация пейлоадов и сценариев |
| `run_tests.py` | Запуск тестов на устройстве |
| `run_unit_tests.py` | Запуск unit-тестов |
| `unit_tests/` | Unit-тесты по модулям |
| `resolve_scheme.py` | Разрешение `$ref`, обход схемы |
| `ollama_orchestrator.py` | Опционально: описания и enrich через Ollama |
| `openapi.json` | OpenAPI-спецификация API |
| `dependencies.json` | Зависимости, lifecycle, mock-данные |
| `.env` | Инвентарь интерфейсов и доступ к API |
| `tests/` | Сгенерированные JSON-сценарии (не путать с `unit_tests/`) |
| `test.log` | Лог генерации (`main.py`) |
| `run.log` | Лог прогона (`run_tests.py`) |

---

## Как работает генерация

```
openapi.json
    → ResolveScheme.resolve_endpoint()      # $ref
    → preprocess_schema_for_jsf()           # oneOf → enum, nullable
    → apply_interface_inventory()           # pattern → enum из .env
    → apply_mock_data()                     # mock_data → enum (IP, VRF, …)
    → generate_value_coverage_payloads()    # покрытие значений + JSF
    → build_test_scenarios()                # setup / teardown из dependencies.json
    → tests/*.json
```

### Порядок lifecycle в сценарии

Для каждого пейлоада `build_test_scenarios()` выполняет:

1. **Prerequisite** `field_mappings` — VRF, DHCP pool и т.п. (другой эндпоинт).
2. **Interface rules** — создание `ifname` (bond откладывается, если `setup_defer: true`).
3. **Field mappings** — enslave, ACL и прочие поля.
4. **Endpoint rules** — setup/teardown тестируемого API по `action`.
5. **Auto scalar delete** — `action.add` (object) + `action.delete` (скаляр).
6. Сортировка **setup** по фазам, внутри фазы `…/add` раньше остальных.
7. Сортировка **teardown** по `teardown_priority` (меньше число → раньше).

---

## Запуск `main.py` (генерация)

```bash
python main.py                              # все POST из openapi.json
python main.py -d /interfaces               # префикс пути
python main.py -e /vrf /interfaces/bonding/add
python main.py -d /interfaces -v            # debug → test.log
python main.py -d /interfaces -c            # компактное покрытие (enum >10 → 3 значения)
python main.py --workers 4                  # параллельно (2+ эндпоинта)
python main.py --ollama                     # описания тестов через Ollama
```

| Аргумент | Описание |
|----------|----------|
| `-e`, `--endpoint PATH [PATH ...]` | Один или несколько POST-эндпоинтов |
| `-d`, `--dir PREFIX [PREFIX ...]` | Все POST, путь которых начинается с PREFIX |
| `-v`, `--verbose` | DEBUG в `test.log` |
| `-c`, `--compact-coverage` | Меньше комбинаций для больших enum |
| `--workers N` | Параллельная генерация (при 2+ эндпоинтах) |
| `--ollama` | Включить Ollama |
| `--ollama-features LIST` | `describe`, `enrich` (по умолчанию оба) |

`-e` и `-d` **нельзя** указывать одновременно.

**Результат:** `tests/<endpoint>_post.json`, лог в `test.log`.

---

## Запуск `run_tests.py` (прогон на устройстве)

```bash
python run_tests.py -d /interfaces -v
python run_tests.py -e /interfaces/bonding/mode
python run_tests.py --base-url https://10.0.0.1:8082
python run_tests.py --stop-on-failure
python run_tests.py --no-recover-already-exists
```

| Аргумент | Описание |
|----------|----------|
| `-e`, `--endpoint` | Эндпоинт(ы) для прогона |
| `-d`, `--dir` | Префикс пути |
| `-v`, `--verbose` | Полные тела запросов/ответов в `run.log` |
| `--base-url URL` | Базовый URL (иначе `API_BASE_URL` из `.env`) |
| `--tests-dir DIR` | Каталог со сценариями (по умолчанию `tests`) |
| `--log-file FILE` | Файл лога (по умолчанию `run.log`) |
| `--timeout SEC` | Таймаут HTTP (по умолчанию 60) |
| `--stop-on-failure` | Остановиться после первого FAIL |
| `--skip-teardown-on-failure` | Не делать teardown при падении setup/main |
| `--max-teardown-retry N` | Повторы teardown (по умолчанию 3) |
| `--recover-already-exists` / `--no-recover-already-exists` | При «already exists»: teardown → повтор (по умолчанию включено) |

В конце `run.log` — итоговая таблица по эндпоинтам (PASS/FAIL, `test_id`).

---

## Запуск unit-тестов

```bash
python run_unit_tests.py          # все тесты
python run_unit_tests.py -v       # подробно
python -m unittest discover -s unit_tests -v
```

| Модуль | Что проверяет |
|--------|----------------|
| `test_schema_preprocess.py` | препроцессинг схемы, minimal payload |
| `test_payload_generation.py` | покрытие значений, dedupe |
| `test_placeholders.py` | `{{var}}`, dotted-пути |
| `test_lifecycle.py` | порядок setup/teardown, VID, defer bond |
| `test_mock_data.py` | секция `mock_data` |
| `test_inventory_env.py` | `.env`, инвентарь интерфейсов |
| `test_dependencies.py` | поиск зависимостей в payload |
| `test_scalar_delete.py` | авто lifecycle scalar delete |
| `test_resolve_scheme.py` | `$ref`, поля схемы |
| `test_endpoint_discovery.py` | фильтр `-e` / `-d` |
| `test_runner_helpers.py` | вспомогательная логика раннера |
| `test_scenario_build.py` | сборка JSON-сценариев |
| `test_ollama.py` | Ollama без сети |
| `test_vid_range.py` | VID_RANGE_LIST |

`OK` в конце = все assert прошли. Это **не** заменяет прогон на устройстве.

---

## `.env` — инвентарь и доступ к API

Скопируйте `.env.example` → `.env`. Приоритет значений: `os.environ` → `.env` → `allowed` в `dependencies.json`.

### Интерфейсы

Переменные привязываются к правилу в `interface_rules` через поле `"env": "ИМЯ_ПЕРЕМЕННОЙ"`.

```env
# Физические ethernet (pattern ^eth(...)$ без точки)
DEVICE_ETH_IFNAMES=eth0,eth1

# Ethernet VLAN: eth1.200 → vid 200 при lifecycle
DEVICE_ETH_VLAN_IFNAMES=eth0.100,eth1.200

# VLAN-интерфейсы (prefix vlan): vlan100 → vid 100
DEVICE_VLAN_IFNAMES=vlan100,vlan200

# Switchport
DEVICE_SWITCHPORT_IFNAMES=switchport1,switchport2

# Bond-интерфейсы (если заданы в enum схемы)
DEVICE_BOND_IFNAMES=bond0

# Пул VID, если имя не содержит vid (bond, br, …)
VID_RANGE=100-120

# Списки для QoS / DSCP (если используются в схеме)
DSCP_LIST=0,63
FPRI_LIST=1,2,3
```

**Добавить новый тип интерфейса:** правило в `dependencies.json` + строка в `.env`. Код менять не нужно.

### Доступ к API (для `run_tests.py`)

```env
API_BASE_URL=https://10.65.5.125:8082

# Один из вариантов авторизации:
API_USER=admin
API_PASSWORD=secret
# API_KEY=your-key
# API_BEARER_TOKEN=eyJ...
```

---

## `mock_data` — предсказуемые значения

Секция в `dependencies.json`. Ограничивает генерацию **безопасными** значениями вместо случайных IP/имён из JSF.

```json
"mock_data": {
  "by_schema": {
    "IP_ADDR": ["10.0.0.1", "10.0.0.2"],
    "IP_ADDR_WITH_BIT_MASK": ["10.0.0.1/24", "10.0.0.2/24"],
    "IPV6_ADDR": ["2001:db8::1"],
    "IPV6_ADDR_WITH_BIT_MASK": ["2001:db8:1::1/64"]
  },
  "by_field": {
    "vrf_name": ["autotest-vrf-1"],
    "vrf": ["autotest-vrf-1"],
    "acl_name": ["autotest-acl-1"]
  }
}
```

| Ключ | Когда использовать |
|------|-------------------|
| `by_schema` | Имя компонента OpenAPI (`IP_ADDR`, `VRF`, …) — автоматически для всех полей с таким `pattern` |
| `by_field` | Конкретное имя поля в payload (`vrf_name`, `acl_name`) |

**Правила:**

- Секция **опциональна** — без неё генерация как раньше (JSF).
- `by_field` и `by_schema` перезаписывают `enum` из инвентаря интерфейсов (приоритет у `mock_data`).
- Для IP лучше `by_schema`, не `by_field` на `ip_addr` (IPv4/IPv6 в разных ветках `oneOf`).
- Значения можно задать списком или строкой через запятую: `"vrf_name": "a,b"`.

Рекомендуемые диапазоны: IPv4 `10.0.0.0/8` или RFC5737 `192.0.2.0/24`, IPv6 `2001:db8::/32`.

---

## `dependencies.json` — зависимости и lifecycle

Файл описывает, какие HTTP-запросы добавить в `setup` / `teardown` каждого сценария.

### Структура

```json
{
  "field_mappings": { },
  "interface_rules": { },
  "endpoint_rules": { },
  "mock_data": { }
}
```

| Раздел | Когда срабатывает |
|--------|-------------------|
| `field_mappings` | В payload есть ключ (`vrf_name`, `primary_interface`, `acl_name`, …) |
| `interface_rules` | Строковое поле подходит под `pattern` / `prefix` (`ifname`, вложенные) |
| `endpoint_rules` | Тестируемый эндпоинт + в payload есть `action` |
| `mock_data` | При генерации (не lifecycle) |

### Единая модель lifecycle

| Фаза | Ключи |
|------|-------|
| Setup (до main) | `setup` или `create` |
| Teardown (после main) | `teardown` или `delete` |

**Простой формат:**

```json
"create": "/interfaces/bonding/add",
"delete": "/interfaces/bonding/delete"
```

**Кастомный шаг:**

```json
{
  "endpoint": "/vrf",
  "method": "POST",
  "payload": { "action": "add", "vrf_name": "{{vrf_name}}" },
  "expected_status": 200,
  "note": "Создать VRF",
  "extract_to_variable": "created_vrf_name",
  "response_extract": "data.vrf_name"
}
```

Любой ключ может быть **объектом или списком** шагов.

### Плейсхолдеры

| Переменная | Источник |
|------------|----------|
| `{{ifname}}`, `{{vrf_name}}`, … | Значение из `main_test.payload` |
| `{{vid}}`, `{{vlan}}` | Из имени интерфейса (`vlan100` → 100) или `VID_RANGE` в `.env` |
| `{{settings.source}}` | Dotted-путь в payload main_test |
| `{{created_pool}}` | Из ответа setup (`extract_to_variable`) |

Неразрешённый плейсхолдер остаётся в JSON как есть.

### Приоритеты setup и teardown

**Setup** сортируется автоматически:

| Фаза | Примеры |
|------|---------|
| `prerequisite` (10) | VRF, DHCP pool |
| `interface` (20) | eth_vlan/add, bond/add |
| `field` (30) | enslave, ACL |
| `endpoint` (40) | endpoint_rules |
| `auto` (50) | scalar delete setup |

Внутри фазы шаги с `…/add` идут раньше (`shutdown`, `capability`, …).

Явный приоритет в JSON:

```json
"teardown_priority": 10,
"setup_priority": 15
```

**Teardown:** меньше число → раньше. По умолчанию: интерфейсы `10`, обычные поля `50`, prerequisite (VRF) `100`.

### `setup_defer` для bond

```json
{
  "prefix": "bond",
  "setup_defer": true,
  "create": "/interfaces/bonding/add",
  "delete": "/interfaces/bonding/delete"
}
```

Bond создаётся **после** eth_vlan / vlan slave-интерфейсов.

### VID и vlandb

В setup для vlan/eth_vlan используйте `"vid": "{{vid}}"`, не фиксированное число:

```json
"setup": {
  "endpoint": "/interfaces/eth_vlan/add",
  "payload": {
    "ifname": "{{ifname}}",
    "vid": "{{vid}}"
  }
}
```

Если в `endpoint_rules` описан `/interfaces/switchport/vlandb`, генератор **автоматически** добавляет `vlandb add` перед созданием VLAN и `vlandb delete` в teardown для того же `{{vid}}`.

### Self-skip setup

Если endpoint шага setup совпадает с тестируемым эндпоинтом — шаг пропускается (ресурс создаётся в `main_test`). Исключение: `main_test.action == "delete"`.

### field_mappings

```json
"primary_interface": {
  "optional": true,
  "setup": [{
    "endpoint": "/interfaces/bonding/capability",
    "payload": {
      "ifname": "{{ifname}}",
      "capability": { "enslave": ["{{primary_interface}}"] }
    }
  }],
  "teardown": {
    "endpoint": "/interfaces/bonding/capability",
    "payload": {
      "ifname": "{{ifname}}",
      "capability": { "enslave": [] }
    }
  }
}
```

| Поле | Описание |
|------|----------|
| `optional` | Не добавлять lifecycle, если значение пустое |
| `teardown_priority` | Порядок в teardown |
| `extract` | Путь в ответе setup (legacy: `data.<field>`) |

**Action-aware lifecycle** (когда в main_test есть `action`):

| `main_test.action` | На том же эндпоинте | Prerequisite (другой эндпоинт) |
|--------------------|---------------------|--------------------------------|
| `add` | только teardown | setup + teardown |
| `delete` | только setup | setup + teardown |
| `modify` | setup + teardown | setup + teardown |

### interface_rules

Поиск **рекурсивный**: `ifname`, `mode.primary_interface`, `port[].ifname`. Первое совпавшее правило сверху вниз.

```json
"interface_rules": {
  "ifname": {
    "rules": [
      {
        "pattern": "^eth(0|[1-9][0-9]{0,3})\\.(0|[1-9][0-9]{0,3})$",
        "env": "DEVICE_ETH_VLAN_IFNAMES",
        "setup": {
          "endpoint": "/interfaces/eth_vlan/add",
          "payload": { "ifname": "{{ifname}}", "vid": "{{vid}}" }
        },
        "teardown": [{
          "endpoint": "/interfaces/eth_vlan/delete",
          "payload": { "ifname": "{{ifname}}" }
        }]
      },
      {
        "prefix": "bond",
        "setup_defer": true,
        "teardown_priority": 10,
        "create": "/interfaces/bonding/add",
        "delete": "/interfaces/bonding/delete"
      }
    ]
  }
}
```

| Поле | Описание |
|------|----------|
| `pattern` | Regex полного совпадения имени |
| `prefix` | Имя начинается с `bond`, `vlan`, `tunnel`, … |
| `env` / `allowed` | Список имён для enum в схеме |
| `physical` | Без lifecycle, если нет teardown |
| `requirements` | `["setup", "teardown"]` — принудительно, даже при self-skip |

Если в `.env` есть `eth1.1`, генератор **исключает** родительский `eth1` из enum для bonding `primary_interface` (нельзя enslave родителя с VLAN-дочерними).

### endpoint_rules

Для эндпоинтов с `action` в payload:

```json
"/interfaces/switchport/vlandb": {
  "bind_fields": ["vlan"],
  "add": {
    "teardown": {
      "endpoint": "/interfaces/switchport/vlandb",
      "payload": { "action": "delete", "vlan": "{{vlan}}" }
    }
  },
  "delete": {
    "setup": {
      "endpoint": "/interfaces/switchport/vlandb",
      "payload": { "action": "add", "vlan": "{{vlan}}" }
    }
  }
}
```

### Авто lifecycle: scalar delete

Если в схеме `action.add` — object, а `action.delete` — скаляр (например `pool_number`), генератор сам добавляет setup/teardown на том же эндпоинте. Настраивать в JSON не нужно.

---

## Формат выходного теста

```json
{
  "test_id": 1,
  "coverage_keys": ["ifname=\"bond0\""],
  "setup": [ { "endpoint": "...", "method": "POST", "payload": {}, "expected_status": 200 } ],
  "main_test": {
    "endpoint": "/interfaces/bonding/mode",
    "method": "POST",
    "payload": { "ifname": "bond0", "mode": { "mode_type": "balance-rr" } },
    "expected_status": 200
  },
  "teardown": [ { "endpoint": "...", "method": "POST", "payload": {} } ],
  "description": "Auto-test: POST /interfaces/bonding/mode"
}
```

Имя файла: `tests/interfaces_bonding_mode_post.json`.

---

## Логирование

| Файл | Программа | Содержимое |
|------|-----------|------------|
| `test.log` | `main.py` | Покрытие полей, mock_data, self-skip, инвентарь |
| `run.log` | `run_tests.py` | HTTP-запросы, PASS/FAIL, итоговая таблица |

`-v` включает подробный вывод (DEBUG / полные тела).

Полезные строки в `test.log`:

- `mock_data: by_schema=N, by_field=M`
- `Покрытие значений: N пейлоадов, целей X/Y`
- `Self-skip setup: …`
- `Инвентарь для pattern …`

---

## Ollama (опционально)

```bash
python main.py -d /interfaces --ollama
python main.py --ollama --ollama-features describe
```

| Фича | Назначение |
|------|------------|
| `describe` | Человекочитаемые `description` в сценариях |
| `enrich` | Подстановка осмысленных строк вместо JSF-заглушек |

Требуется локальный Ollama. Без `--ollama` используются шаблонные описания.

---

## Типичные задачи

| Задача | Действие |
|--------|----------|
| Новый эндпоинт в OpenAPI | `python main.py -e /новый/путь` |
| Тест ломает VRF/ACL | Добавить `field_mappings` |
| Нужны безопасные IP | Секция `mock_data` |
| Создаваемый интерфейс (bond, tunnel) | `interface_rules`: `prefix` + `create`/`delete` |
| Bond после slave VLAN | `"setup_defer": true` на bond |
| Порядок teardown | `teardown_priority` (меньше → раньше) |
| Эндпоинт с `action` | `endpoint_rules` |
| Проверить генератор без API | `python run_unit_tests.py -v` |
| Прогон на устройстве | `python run_tests.py -d /interfaces -v` |

### Чеклист: новая зависимость

**Поле в payload (VRF, ACL):**

1. Ключ в `field_mappings` с `setup`/`teardown`.
2. `optional: true`, если поле может отсутствовать.
3. `python main.py -e /ваш/эндпоинт`.

**Интерфейс:**

1. Правило в `interface_rules.ifname.rules` (специфичные **выше** общих).
2. Строка в `.env` с `env`.
3. Для VLAN: `"vid": "{{vid}}"`.

**Эндпоинт с action:**

1. Ключ в `endpoint_rules`.
2. `bind_fields` для плейсхолдеров.
3. Блоки `add` / `delete` / `modify`.

---

## Зависимости Python

| Пакет | Зачем |
|-------|-------|
| `jsf` | Генерация по JSON Schema |
| `jsonschema` | Валидация пейлоадов |
| `requests` | HTTP в `run_tests.py` и Ollama |

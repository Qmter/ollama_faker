# ollama_faker

Генератор и раннер REST API тестов для маршрутизатора по OpenAPI-схеме.

Идея простая: **весь поведение сценариев настраивается в JSON и `.env`**, а не в Python. Код только:

1. читает `openapi.json`;
2. строит пейлоады (покрытие enum / границ / boolean);
3. по `dependencies.json` добавляет `setup` / `teardown`;
4. пишет JSON в `tests/`;
5. `run_tests.py` гоняет эти JSON на живом устройстве.

```
openapi.json + dependencies.json + .env
        │
        ▼
     main.py  ──►  tests/<group>/<endpoint>_post.json
        │
        ▼
  clear_for_tests.py  ← cleanup.json  (опционально, list→delete)
        │
        ▼
   run_tests.py  ──►  logs/run_*.log + таблица PASS/FAIL
```

Опционально: GUI `deps_editor.py` для правки `dependencies.json` кнопками.

---

## Содержание

1. [Быстрый старт](#быстрый-старт)
2. [Структура проекта](#структура-проекта)
3. [Рабочий процесс](#рабочий-процесс)
4. [CLI: генерация (`main.py`)](#cli-генерация-mainpy)
5. [CLI: предочистка (`clear_for_tests.py`)](#cli-предочистка-clear_for_testspy)
6. [CLI: прогон (`run_tests.py`)](#cli-прогон-run_testspy)
7. [GUI: редактор dependencies (`deps_editor.py`)](#gui-редактор-dependencies-deps_editorpy)
8. [Unit-тесты](#unit-тесты)
9. [Логи](#логи)
10. [Файл `.env`](#файл-env)
11. [`dependencies.json` — обзор](#dependenciesjson--обзор)
12. [Формат lifecycle-шага](#формат-lifecycle-шага)
13. [Плейсхолдеры `{{…}}`](#плейсхолдеры-)
14. [Порядок setup / teardown](#порядок-setup--teardown)
15. [`field_mappings`](#field_mappings)
16. [`interface_rules`](#interface_rules)
17. [`endpoint_rules`](#endpoint_rules)
18. [`mock_data`](#mock_data)
19. [`interface_lifecycle` и `synthetic_bind_fields`](#interface_lifecycle-и-synthetic_bind_fields)
20. [`field_couplings`](#field_couplings)
21. [`reserved_values`](#reserved_values)
22. [`cleanup.json`](#cleanupjson)
23. [Формат сгенерированного теста](#формат-сгенерированного-теста)
24. [Как работает генерация (pipeline)](#как-работает-генерация-pipeline)
25. [Ollama (опционально)](#ollama-опционально)
26. [Типичные задачи и чеклисты](#типичные-задачи-и-чеклисты)
27. [Зависимости Python](#зависимости-python)

---

## Быстрый старт

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# отредактируйте .env: интерфейсы устройства, API_BASE_URL, логин/пароль

# 1. Сгенерировать сценарии для группы
python main.py -d /interfaces

# 2. (опционально) снести тестовые ресурсы с устройства (list→delete)
python clear_for_tests.py

# 3. Прогнать на устройстве
python run_tests.py -d /interfaces -v

# 4. Проверить логику генератора без сети
python run_unit_tests.py -v
```

`-e` и `-d` действуют одинаково в `main.py` и `run_tests.py`: один эндпоинт / список или префикс пути.
`clear_for_tests.py` работает по `cleanup.json` (не по `-e`/`-d` из тестов).

---

## Структура проекта

| Путь | Назначение |
|------|------------|
| `main.py` | Генерация пейлоадов и JSON-сценариев |
| `run_tests.py` | Прогон сценариев на устройстве |
| `clear_for_tests.py` | Предочистка: list→delete по `cleanup.json` |
| `deps_editor.py` | GUI (PyQt6) для правки `dependencies.json` |
| `run_unit_tests.py` | Офлайн unit-тесты генератора |
| `resolve_scheme.py` | Разрешение `$ref`, обход OpenAPI |
| `ollama_orchestrator.py` | Опциональные описания через Ollama |
| `log_paths.py` | Общие имена логов `logs/<prefix>_…` |
| `test_paths.py` | Путь API → файл в `tests/` |
| `openapi.json` | Спецификация API устройства |
| `dependencies.json` | Lifecycle, mock, couplings, reserved… |
| `cleanup.json` | Правила предочистки (list + delete) |
| `.env` / `.env.example` | Инвентарь интерфейсов + доступ к API |
| `requirements.txt` | Python-зависимости |
| `tests/` | Сгенерированные сценарии (не путать с `unit_tests/`) |
| `unit_tests/` | Unit-тесты |
| `logs/` | Логи генерации / прогона / очистки |

---

## Рабочий процесс

### Рекомендуемый цикл для группы API

```bash
# Настроить .env и dependencies.json под устройство
python main.py -d /interfaces -c          # генерация (compact — меньше сценариев)
python clear_for_tests.py                 # list→delete по cleanup.json
python run_tests.py -d /interfaces -v     # прогон
# смотреть logs/run_*_interfaces.log → править dependencies / mock_data / .env
python main.py -d /interfaces -c          # перегенерация после правок
```

### Один эндпоинт

```bash
python main.py -e /interfaces/bonding/mode
python run_tests.py -e /interfaces/bonding/mode -v
```

### Полный прогон всего OpenAPI

```bash
python main.py --workers 4
python run_tests.py --stop-on-failure
```

---

## CLI: генерация (`main.py`)

```bash
python main.py                              # все POST из openapi.json
python main.py -d /interfaces               # все POST с префиксом
python main.py -e /vrf /dns/client
python main.py -d /interfaces -v            # DEBUG → logs/gen_*.log
python main.py -d /interfaces -c            # компактное покрытие enum
python main.py --workers 4                  # параллельно (нужно ≥2 эндпоинта)
python main.py --ollama                     # описания/имена через Ollama
```

| Аргумент | Описание |
|----------|----------|
| `-e`, `--endpoint PATH …` | Один или несколько POST-путей |
| `-d`, `--dir PREFIX …` | Все POST, путь которых начинается с PREFIX |
| `-v`, `--verbose` | DEBUG в лог генерации |
| `-c`, `--compact-coverage` | Большие enum (>10) → 3 значения вместо всех |
| `--workers N` | N процессов (только при 2+ эндпоинтах) |
| `--ollama` | Включить Ollama |
| `--ollama-features LIST` | `describe`, `enrich` (по умолчанию оба) |

`-e` и `-d` **нельзя** указывать одновременно. Без обоих — все POST из `openapi.json`.

**Результат:** `tests/<первый_сегмент>/<все_сегменты>_post.json`, например:

```
/interfaces/bonding/mode  →  tests/interfaces/interfaces_bonding_mode_post.json
/acl/filter/filter_ipv4   →  tests/acl/acl_filter_filter_ipv4_post.json
/vrf                      →  tests/vrf_post.json
```

---

## CLI: предочистка (`clear_for_tests.py`)

Читает **`cleanup.json`** (не teardown из `tests/`). Для каждого правила: GET/list → фильтр объектов → POST delete с `{{item}}`.

Нужно, когда устройство «грязное» после прошлых FAIL. **Не** добавляйте в cleanup удаление system profiles / ssh / telnet / usergroups — это может заблокировать доступ к роутеру. Защита: `defaults.skip` / `skip_prefix`.

```bash
python clear_for_tests.py                      # все rules из cleanup.json
python clear_for_tests.py -r interfaces_vlan   # только выбранные rules
python clear_for_tests.py --dry-run            # list есть, delete не шлётся
python clear_for_tests.py --dry-run-config     # только показать правила, без HTTP
python clear_for_tests.py --config cleanup.json -v
```

| Аргумент | Описание |
|----------|----------|
| `--config FILE` | Файл правил (по умолчанию `cleanup.json`) |
| `-r`, `--rule NAME …` | Выполнить только правила с этими `name` |
| `-v` | Подробный лог |
| `--base-url` | URL API (иначе `API_BASE_URL` из `.env`) |
| `--log-file` | Явный файл лога |
| `--timeout` | Таймаут HTTP |
| `--max-teardown-retry N` | Повторы одного delete (по умолчанию 3) |
| `--dry-run` | List вызывается, delete не выполняется |
| `--dry-run-config` | Только разбор конфига, без HTTP |

Порядок rules — по полю `priority` (меньше → раньше). Подробнее: [cleanup.json](#cleanupjson).

---

## CLI: прогон (`run_tests.py`)

```bash
python run_tests.py -d /interfaces -v
python run_tests.py -e /interfaces/bonding/mode
python run_tests.py --base-url https://10.0.0.1:8082
python run_tests.py --stop-on-failure
python run_tests.py --no-recover-already-exists
```

| Аргумент | Описание |
|----------|----------|
| `-e` / `-d` | Какие сценарии гонять |
| `-v` | Полные тела запросов/ответов в лог |
| `--base-url URL` | Базовый URL (иначе `API_BASE_URL`) |
| `--tests-dir DIR` | Каталог JSON (по умолчанию `tests`) |
| `--log-file FILE` | Явный лог (по умолчанию `logs/run_<datetime>_<scope>.log`) |
| `--timeout SEC` | Таймаут HTTP (по умолчанию 60) |
| `--stop-on-failure` | Остановиться после первого FAIL |
| `--skip-teardown-on-failure` | Не делать teardown, если упал setup/main |
| `--max-teardown-retry N` | Повторы teardown (по умолчанию 3) |
| `--recover-already-exists` / `--no-…` | При «already exists»: teardown → повтор (вкл. по умолчанию) |

В конце лога — итоговая таблица по эндпоинтам: статус, список `test_id` с FAIL.

Порядок внутри сценария:

1. все шаги `setup` (ожидается HTTP 200, если не указано иное);
2. `main_test`;
3. все шаги `teardown` (статус не валидируется строго; «not found» считается OK).

---

## GUI: редактор dependencies (`deps_editor.py`)

PyQt6-приложение: формы по всем секциям `dependencies.json`, live JSON-preview, Apply → Ctrl+S.

```bash
.venv\Scripts\python.exe deps_editor.py
.venv\Scripts\python.exe deps_editor.py path\to\dependencies.json
```

| Возможность | Описание |
|-------------|----------|
| Вкладки 1–8 | field_mappings … reserved_values |
| Применить | Записать текущее правило в память редактора |
| Ctrl+S | Сохранить файл |
| Вид → тема | Светлая / тёмная (Ctrl+T), сохраняется между запусками |
| Подсказки | Tooltip на кнопках и вкладках |

После правок в GUI нужна **перегенерация** сценариев: `python main.py -d …`.

---

## Unit-тесты

Офлайн, без устройства и без HTTP:

```bash
python run_unit_tests.py
python run_unit_tests.py -v
python run_unit_tests.py -k test_lifecycle.py
```

| Модуль | Что проверяет |
|--------|----------------|
| `test_schema_preprocess.py` | Препроцессинг схемы, minimal payload |
| `test_payload_generation.py` | Покрытие значений, dedupe |
| `test_placeholders.py` | `{{var}}`, dotted-пути |
| `test_lifecycle.py` | Порядок setup/teardown, VID, defer bond |
| `test_mock_data.py` | Секция `mock_data` (by_field / by_schema) |
| `test_field_couplings.py` | `field_couplings`: when → ensure/remove |
| `test_reserved_values.py` | `reserved_values.by_field` |
| `test_schema_field_relations.py` | swap min/max, drop burst и т.п. |
| `test_mirror_delete_rule.py` | delete.rule → setup add того же эндпоинта |
| `test_inventory_env.py` | `.env`, инвентарь интерфейсов |
| `test_dependencies.py` | field_mappings, endpoint_rules, skip_targets |
| `test_scalar_delete.py` | Авто lifecycle scalar delete |
| `test_resolve_scheme.py` | `$ref`, поля схемы |
| `test_endpoint_discovery.py` | Фильтр `-e` / `-d` |
| `test_runner_helpers.py` | Вспомогательная логика раннера |
| `test_scenario_build.py` | Сборка JSON-сценариев |
| `test_generation_log.py` | Имена файлов логов |
| `test_clear_for_tests.py` | Парсинг `cleanup.json`, skip, priority |
| `test_vid_range.py` | VID_RANGE_LIST |
| `test_ollama.py` | Ollama без сети |
| `test_interface_schema_lifecycle.py` | Schema-driven lifecycle |

`OK` в unit-тестах **не заменяет** прогон на устройстве.

---

## Логи

Все логи пишутся в каталог `logs/` (создаётся автоматически). Формат имени:

```
logs/<prefix>_YYYYMMDD_HHMMSS_<scope>.log
```

| prefix | Скрипт |
|--------|--------|
| `gen` | `main.py` |
| `run` | `run_tests.py` |
| `clear` | `clear_for_tests.py` |

| scope | Когда |
|-------|--------|
| `all` | Запуск без `-e` и `-d` |
| `interfaces` | `-d /interfaces` |
| `dns_client` | `-e /dns/client` |

Примеры:

```
logs/gen_20260714_091500_interfaces.log
logs/run_20260714_100952_interfaces.log
logs/clear_20260714_101000_all.log
```

Явный путь: `--log-file my.log` (для run/clear). `-v` включает подробный уровень.

`*.log` в `.gitignore` — в git не коммитятся.

---

## Файл `.env`

Скопируйте `.env.example` → `.env` и подставьте значения **вашего** устройства.

### Приоритет источников имён

Для списков интерфейсов:

1. переменная окружения процесса (`os.environ`);
2. файл `.env`;
3. поле `"allowed"` в правиле `interface_rules` (если есть).

### Переменные интерфейсов

Каждая переменная привязана к правилу через `"env": "ИМЯ"` в `dependencies.json` → `interface_rules`.

```env
# Физические ethernet (без точки): eth1
DEVICE_ETH_IFNAMES=eth1

# Ethernet VLAN: имя eth1.200 → vid выводится как 200
DEVICE_ETH_VLAN_IFNAMES=eth1.2,eth1.3

# VLAN-интерфейсы: vlan100 → vid 100
# Не используйте vlan0 / vlan1 / vlan4095, если API это запрещает
DEVICE_VLAN_IFNAMES=vlan100,vlan200

# Switchport
DEVICE_SWITCHPORT_IFNAMES=switchport2,switchport3

# Bond (если нужны фиксированные имена в enum)
DEVICE_BOND_IFNAMES=bond1

# Пул VID, когда имя само по себе не несёт номер (bond, br, …)
# Также отсекаются значения вне 1..4094
VID_RANGE=100-4000

# QoS / DSCP (если поля в схеме матчятся на эти env через interface_rules)
DSCP_LIST=0,63
FPRI_LIST=1,2,3
```

**Зачем инвентарь.** Генератор подменяет широкий OpenAPI-`pattern` на `enum` из ваших имён. Тогда тесты используют реальные порты устройства, а lifecycle знает, как создать `vlan100` / `eth1.2`.

**Добавить новый тип интерфейса:** правило в `interface_rules` + строка в `.env`. Код трогать не нужно.

### Доступ к API (для run / clear)

```env
API_BASE_URL=https://10.65.5.125:8082

# Достаточно одного варианта авторизации:
API_USER=admin
API_PASSWORD=admin
# API_KEY=your-api-key
# API_BEARER_TOKEN=eyJ...
```

`--base-url` в CLI перекрывает `API_BASE_URL`.

### Практические советы по `.env`

| Ситуация | Что сделать |
|----------|-------------|
| `vid 0` / `4095` / vlandb «must be > 1» | Убрать `vlan0`, `vlan1`, `vlan4095` из `DEVICE_VLAN_IFNAMES`; задать `VID_RANGE=2-4094` |
| Bond enslave «Network is down» | Поднять parent в lifecycle (`/interfaces/shutdown` с `adm_state: true`) и перечислить реальные `eth1.N` |
| Blink / eth-only API | Оставить в `DEVICE_ETH_IFNAMES` только NIC, который поддерживает операцию |
| Разные стенды | Держать разные `.env` (не коммитить боевой `.env`) |

---

## `dependencies.json` — обзор

Единственный конфиг «что создать до теста и что удалить после». Код **не** знает про конкретные `/dns/...` или bond-режимы — всё описывается здесь.

### Корневые секции

```json
{
  "field_mappings": { },
  "endpoint_rules": { },
  "interface_rules": { },
  "interface_lifecycle": { },
  "synthetic_bind_fields": { },
  "mock_data": { },
  "field_couplings": [ ],
  "reserved_values": { }
}
```

| Секция | Когда срабатывает |
|--------|-------------------|
| `field_mappings` | В payload main_test есть одноимённое поле (`vrf_name`, `zone_name`, `primary_interface`, …) |
| `interface_rules` | Строковое значение подходит под `pattern` / `prefix` правила (`ifname`, вложенные поля) |
| `endpoint_rules` | Ключ = тестируемый эндпоинт; setup/teardown по `action` или `lifecycle_key_field` |
| `interface_lifecycle` | Какие schema-компоненты (`IFNAME`, …) искать как «интерфейсные» поля |
| `synthetic_bind_fields` | Для `delete`/`modify` без поля в payload — подтянуть lifecycle из `field_mappings` + `mock_data` |
| `mock_data` | При генерации пейлоадов: фиксированные enum вместо случайных JSF-значений |
| `field_couplings` | После генерации payload: при условии `when` — `ensure` поля и/или `remove` пути |
| `reserved_values` | Значения, которые генератор **никогда** не подставит (`vlan1`, `vid` 603, …) |

---

## Формат lifecycle-шага

Во всех секциях используется одна модель.

### Короткий вид (только пути)

```json
{
  "create": "/interfaces/bonding/add",
  "delete": "/interfaces/bonding/delete"
}
```

Генератор сам соберёт payload с `{{ifname}}` (и при необходимости `{{vid}}`).

### Полный шаг

```json
{
  "endpoint": "/vrf",
  "method": "POST",
  "payload": {
    "action": "add",
    "vrf_name": "{{vrf_name}}"
  },
  "expected_status": 200,
  "note": "Создать VRF до теста",
  "extract_to_variable": "created_vrf_name",
  "response_extract": "data.vrf_name",
  "setup_priority": 15
}
```

| Поле | Описание |
|------|----------|
| `endpoint` | POST-путь |
| `method` | Обычно `POST` |
| `payload` | Тело; плейсхолдеры `{{…}}` подставляются при генерации |
| `expected_status` | Только для setup (по умолчанию 200) |
| `note` | Комментарий в JSON-сценарии |
| `extract_to_variable` | Имя переменной из ответа setup |
| `response_extract` | Путь в ответе (`data.vrf_name`) |
| `setup_priority` / `teardown_priority` | Явный порядок (см. ниже) |

`setup` / `teardown` могут быть **одним объектом или списком** шагов.

Синонимы: `setup` ≈ `create`, `teardown` ≈ `delete`.

---

## Плейсхолдеры `{{…}}`

| Пример | Откуда значение |
|--------|-----------------|
| `{{ifname}}`, `{{vrf_name}}`, `{{zone_name}}` | Поле из `main_test.payload` (рекурсивный обход) |
| `{{vid}}`, `{{vlan}}` | Из имени (`vlan100` → 100, `eth1.200` → 200) или из пула `VID_RANGE` |
| `{{settings.source}}` | Dotted-путь в payload |
| `{{created_vrf_name}}` | Значение, сохранённое setup через `extract_to_variable` |
| `{{primary_interface}}` | То же поле из payload / bind |

Если плейсхолдер не удалось разрешить, он **остаётся** в JSON как строка `{{…}}` — смотрите лог генерации.

Для `endpoint_rules` список нужных ключей задаётся в `bind_fields`. Недостающие можно добить из `mock_data.by_field`.

---

## Порядок setup / teardown

### Setup (меньше число → раньше)

Фазы по умолчанию:

| Фаза | Приоритет | Примеры |
|------|-----------|---------|
| `interface` | 5 | `vlan/add`, `eth_vlan/add`, `tunnel/add` |
| `prerequisite` | 10 | VRF, DHCP pool, create на **другом** эндпоинте |
| `field` | 30 | enslave, ACL из `field_mappings` |
| `endpoint` | 40 | шаги из `endpoint_rules` |
| `auto` | 50 | авто scalar-delete setup |

Внутри одной фазы шаги с `…/add` идут раньше (`capability`, `shutdown`, …).

Явно: `"setup_priority": 15` в шаге или в конфиге поля.

Bond с `"setup_defer": true` создаётся **после** slave-интерфейсов (eth_vlan / vlan), чтобы enslave был возможен.

### Teardown (меньше число → раньше)

| Тип | По умолчанию |
|-----|--------------|
| Интерфейсы (bond, vlan, tunnel, …) | `10`–`11` |
| Обычные поля | `50` |
| Prerequisite (VRF и т.п.) | `100` |

Явно: `"teardown_priority": 10`. Сначала сносятся зависимости интерфейсов, потом сам VRF.

### Self-skip

Если endpoint шага setup **совпадает** с тестируемым эндпоинтом — шаг пропускается (ресурс создаёт `main_test`). Исключение: `main_test` с `action: delete` (нужен предварительный create).

Принудительно оставить setup даже при совпадении пути: `"requirements": ["setup", "teardown"]`.

### Auto vlandb

Если в `endpoint_rules` описан `/interfaces/switchport/vlandb` (шаблоны add/delete), при создании vlan/eth_vlan генератор **сам** добавляет:

- setup: `vlandb` action add для того же `{{vid}}`;
- teardown: `vlandb` action delete.

В шаблоне vlan setup пишите `"vid": "{{vid}}"`, не литерал.

### Auto scalar delete

Если в схеме `action.add` — object, а `action.delete` — скаляр (например только `pool_number`), генератор сам добавит create-before-delete на том же эндпоинте. В JSON настраивать не нужно.

---

## `field_mappings`

Срабатывает, когда в payload есть ключ с таким именем.

```json
"field_mappings": {
  "vrf_name": {
    "teardown_priority": 100,
    "optional": true,
    "setup": {
      "endpoint": "/vrf",
      "method": "POST",
      "payload": {
        "action": "add",
        "vrf_name": "{{vrf_name}}"
      },
      "extract_to_variable": "created_vrf_name",
      "response_extract": "data.vrf_name"
    },
    "teardown": {
      "endpoint": "/vrf",
      "method": "POST",
      "payload": {
        "action": "delete",
        "vrf_name": "{{vrf_name}}"
      }
    }
  }
}
```

### Полезные флаги

| Поле | Смысл |
|------|--------|
| `optional: true` | Не добавлять lifecycle, если значение пустое / отсутствует |
| `teardown_priority` | Порядок удаления |
| `requirements` | `["setup","teardown"]` — всегда, даже при self-skip |
| `skip_targets` | Список эндпоинтов, для которых mapping **не** применять |
| `extract` | Legacy-путь извлечения (`data.<field>`) |

### `skip_targets`

Нужен, когда глобальный mapping мешает «родным» операциям поля.

```json
"zone_name": {
  "skip_targets": [
    "/dns/server/zone/master/add",
    "/dns/server/zone/slave/*"
  ],
  "setup": { "...": "создать master-зону" },
  "teardown": { "...": "удалить зону" }
}
```

- точное совпадение: `/dns/server/zone/master/add`;
- префикс: `/dns/server/zone/slave/*` (звёздочка только в конце).

Иначе перед `slave/add` генератор создал бы master-зону с тем же именем → конфликт.

### Action-aware поведение

Если lifecycle уходит на **тот же** эндпоинт, что и тест:

| `action` в main | Что добавляется |
|-----------------|-----------------|
| `add` | только teardown |
| `delete` | только setup |
| `modify` | setup + teardown |

Если setup/teardown на **другом** эндпоинте (prerequisite, VRF) — добавляются обе фазы.

### Пример: bonding enslave

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

---

## `interface_rules`

Описывает, **как создавать/удалять интерфейс по его имени**. Поиск рекурсивный: `ifname`, `mode.primary_interface`, `port[].ifname`, …

Правила проверяются **сверху вниз** — более специфичные (eth_vlan) ставьте **выше** общих (`^eth…$` без точки).

```json
"interface_rules": {
  "ifname": {
    "rules": [
      {
        "pattern": "^eth[0-9]+\\.[0-9]+$",
        "env": "DEVICE_ETH_VLAN_IFNAMES",
        "teardown_priority": 11,
        "setup": {
          "endpoint": "/interfaces/eth_vlan/add",
          "payload": {
            "ifname": "{{ifname}}",
            "vid": "{{vid}}"
          }
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
      },
      {
        "prefix": "vlan",
        "env": "DEVICE_VLAN_IFNAMES",
        "teardown_priority": 11,
        "setup": {
          "endpoint": "/interfaces/vlan/add",
          "payload": {
            "ifname": "{{ifname}}",
            "vid": "{{vid}}"
          }
        },
        "teardown": [{
          "endpoint": "/interfaces/vlan/delete",
          "payload": { "ifname": "{{ifname}}" }
        }]
      }
    ]
  }
}
```

| Поле правила | Описание |
|--------------|----------|
| `pattern` | Regex полного совпадения имени |
| `prefix` | Имя начинается с `bond` / `vlan` / `tunnel` / … |
| `env` | Имя переменной в `.env` → enum в схеме |
| `allowed` | Список имён прямо в JSON (fallback без `.env`) |
| `setup` / `teardown` / `create` / `delete` | Lifecycle |
| `setup_defer` | Отложить create (bond после slave) |
| `teardown_priority` | Порядок удаления |
| `requirements` | Принудительный lifecycle |
| `physical` | Физический порт без create (иногда только reset в teardown) |

Если в инвентаре есть `eth1.1`, родительский `eth1` обычно **исключается** из enum для bonding `primary_interface` (нельзя enslave родителя при существующих VLAN-дочерних) — это поведение генератора по inventory.

Тот же механизм `pattern` + `env` используется и для не-интерфейсных списков (`VID_RANGE`, `DSCP_LIST`, …), если правило так настроено.

---

## `endpoint_rules`

Правила привязаны к **тестируемому пути**, а не к имени поля.

### Структура

```json
"/dns/server/zone/master/entry/delete": {
  "lifecycle_key_field": "entry_type",
  "bind_fields": ["zone_name", "entry_name", "entry_type", "ip_address", "priority"],
  "a": {
    "setup": {
      "endpoint": "/dns/server/zone/master/entry/add",
      "payload": {
        "zone_name": "{{zone_name}}",
        "entry": {
          "entry_name": "{{entry_name}}",
          "entry_params": {
            "entry_type": "a",
            "ip_address": "{{ip_address}}"
          }
        }
      }
    }
  },
  "aaaa": { "setup": { "...": "…" } },
  "mx": { "setup": { "...": "…" } }
}
```

### Как выбирается блок lifecycle

1. **Top-level** `setup` / `teardown` у правила — применяются **всегда**.
2. Дополнительно — блок по ключу:
   - если есть `action` в payload → блоки `add` / `delete` / `modify` / …;
   - иначе, если задан `lifecycle_key_field` → значение этого поля (`entry_type=a` → блок `"a"`).

Плоский action вроде `"action": "add first"` тоже может быть ключом блока (как строка в `endpoint_rules`).

### `bind_fields`

Список имён, которые нужно вытащить из payload (включая вложенные) для плейсхолдеров. Если поля нет в payload — берётся первое значение из `mock_data.by_field`.

### Пример: vlandb

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

### Пример: always-on top-level + action

```json
"/dns/client": {
  "setup": { "...": "общие шаги до любого теста" },
  "teardown": { "...": "общие шаги после" },
  "add first": {
    "teardown": { "...": "только для action=add first" }
  }
}
```

---

## `mock_data`

Ограничивает генерацию **предсказуемыми** значениями вместо случайных IP/строк JSF.

```json
"mock_data": {
  "by_schema": {
    "IP_ADDR": ["10.0.0.1", "10.0.0.2"],
    "IP_ADDR_WITH_BIT_MASK": ["10.0.0.1/32", "10.0.0.2/32"],
    "IPV6_ADDR": ["2001:db8::1"],
    "IPV6_ADDR_WITH_BIT_MASK": ["2001:db8:1::1/64"]
  },
  "by_field": {
    "vrf_name": ["vrf1", "vrf2"],
    "acl_name": ["acl1"],
    "description": ["", "autotest-desc"],
    "zone_name": ["zone1", "zone2"]
  }
}
```

| Ключ | Когда |
|------|--------|
| `by_schema` | Имя компонента OpenAPI (`IP_ADDR`, …) — по `pattern` поля |
| `by_field` | Имя свойства в схеме (`vrf_name`, `description`) |

**Правила:**

- секция опциональна;
- `by_field` имеет **приоритет** над `by_schema` (одинаковые имена свойств / схем не конфликтуют «в пользу схемы»);
- значения: список или строка через запятую (`"vrf_name": "a,b"`);
- для IP лучше `by_schema`, а не `by_field` на `ip_addr` (IPv4/IPv6 в разных ветках `oneOf`);
- если API требует `/32` для tunnel peer — задайте это в `IP_ADDR_WITH_BIT_MASK`, а не правьте код;
- безопасные диапазоны: `10.0.0.0/8`, RFC5737 `192.0.2.0/24`, IPv6 `2001:db8::/32`.

После правок `mock_data` нужна **перегенерация**: `python main.py -d …`.

---

## `interface_lifecycle` и `synthetic_bind_fields`

### `interface_lifecycle`

Указывает, какие `$ref`-компоненты схемы считать «именем интерфейса» и гонять через `interface_rules`:

```json
"interface_lifecycle": {
  "schema_components": [
    "IFNAME",
    "interfaces_ethernet_ifname",
    "interfaces_eth_vlan_ifname",
    "interfaces_vlan_ifname"
  ],
  "rules_key": "ifname",
  "exclude_fields": ["jail_name"]
}
```

- `"enabled": false` — отключить schema-driven поиск (останется только поиск по ключам правил);
- `exclude_fields` — не считать эти имена interface-lifecycle (например `jail_name`).

### `synthetic_bind_fields`

Для эндпоинтов, где `delete`/`modify` **не** кладут идентификатор в payload (он уже на устройстве), но lifecycle нужен:

```json
"synthetic_bind_fields": {
  "/acl/filter/filter_ipv4": ["acl_name"]
}
```

Генератор возьмёт `acl_name` из `mock_data.by_field` и применит `field_mappings.acl_name` (setup перед delete).

---

## `field_couplings`

Правки payload **после** генерации: при условии `when` выставить поля (`ensure`) и/или убрать конфликтующие пути (`remove`). Порядок правил в массиве = приоритет.

```json
{
  "endpoints": ["/acl/acl_ipv4"],
  "when": { "path": "action.add.rule.icmp", "present": true },
  "ensure": {
    "action.add.rule.protocol": { "value": { "protocol_name": "icmp" } }
  },
  "remove": [
    "action.add.rule.tcp_flags",
    "action.add.rule.sourceports"
  ],
  "only_if_missing": false
}
```

| Поле | Описание |
|------|----------|
| `endpoints` | Список путей (пусто / нет = все эндпоинты) |
| `when.path` | Dotted-путь в payload |
| `when.present: true` | Поле есть |
| `when.in: […]` | Значение ∈ списка (например mode `gretap`) |
| `ensure.<path>` | `{ "value": … }`, `{ "values": […] }` или `{ "from_mock": "field" }` |
| `remove` | Список dotted-путей удалить |
| `only_if_missing` | Ensure только если целевого поля ещё нет |

Типичный кейс: ACL icmp/igmp/ports → выставить `protocol` и убрать несовместимые поля. Зеркалите правила для `action.add.*` и `action.delete.*`.

---

## `reserved_values`

Значения, которые генератор отфильтрует из enum / покрытия (даже если они есть в OpenAPI или `.env`).

```json
"reserved_values": {
  "by_field": {
    "ifname": ["vlan0", "vlan1", "vlan603", "vlan4095", "switchport1"],
    "vid": [0, 1, 603, 4095],
    "vlan": ["0", "1", "603", "4095"]
  }
}
```

Дублируйте защиту и в `cleanup.json` → `defaults.skip`, чтобы предочистка не трогала служебные объекты.

---

## `cleanup.json`

Конфиг для `clear_for_tests.py`. Каждое правило: **list** (получить список) → **delete** (удалить каждый item).

```json
{
  "defaults": {
    "skip": ["vlan1", "admin", "startup", "base_config"],
    "skip_prefix": []
  },
  "rules": [
    {
      "name": "interfaces_vlan",
      "priority": 11,
      "list": {
        "endpoint": "/interfaces/list",
        "method": "GET",
        "items_path": "result.interfaces",
        "item_filter": { "category": "vlan" },
        "item_values": "ifname"
      },
      "delete": {
        "endpoint": "/interfaces/vlan/delete",
        "method": "POST",
        "payload": { "ifname": "{{item}}" }
      },
      "skip": ["vlan1", "vlan603"]
    }
  ]
}
```

| Поле | Описание |
|------|----------|
| `defaults.skip` / `skip_prefix` | Мержится во все rules |
| `priority` | Меньше → раньше |
| `list.items_path` | Dotted-путь к массиву в ответе |
| `list.item_filter` | Фильтр объектов (все ключи должны совпасть) |
| `list.item_values` | Поле элемента → значение `{{item}}` |
| `delete.payload` | Тело с `{{item}}` |
| `skip` | Точные имена, которые не удалять |

---

## Формат сгенерированного теста

```json
{
  "test_id": 1,
  "coverage_keys": ["ifname=\"bond0\"", "mode.mode_type=\"balance-rr\""],
  "setup": [
    {
      "endpoint": "/interfaces/bonding/add",
      "method": "POST",
      "payload": { "ifname": "bond0" },
      "expected_status": 200
    }
  ],
  "main_test": {
    "endpoint": "/interfaces/bonding/mode",
    "method": "POST",
    "payload": {
      "ifname": "bond0",
      "mode": { "mode_type": "balance-rr" }
    },
    "expected_status": 200
  },
  "teardown": [
    {
      "endpoint": "/interfaces/bonding/delete",
      "method": "POST",
      "payload": { "ifname": "bond0" }
    }
  ],
  "description": "Auto-test: POST /interfaces/bonding/mode"
}
```

`coverage_keys` — что именно этот сценарий покрывает (для анализа FAIL). Руками править JSON в `tests/` обычно не нужно: правите dependencies / `.env` и перегенерируйте.

---

## Как работает генерация (pipeline)

```
openapi.json
  → discover POST endpoints (-e / -d)
  → ResolveScheme.resolve_endpoint()      # развернуть $ref
  → preprocess_schema_for_jsf()           # oneOf/const → enum, nullable
  → apply_interface_inventory()           # pattern → enum из .env (+ reserved)
  → apply_mock_data()                     # by_schema, затем by_field (приоритетнее)
  → apply_reserved_values_to_schema()     # выкинуть запрещённые из enum схемы
  → generate_value_coverage_payloads()    # enum, boolean, min/max, + JSF
  → build_test_scenarios()
       · synchronize_vid_ifname
       · normalize_schema_field_relations  # swap min/max, drop invalid siblings
       · apply_field_couplings             # when → ensure / remove
       · setup/teardown из dependencies
         (field_mappings, interface_rules, endpoint_rules,
          auto vlandb, mirror delete.rule → setup, scalar delete)
       · сортировка setup / teardown
  → tests/.../*.json
```

Параллельный режим (`--workers`): каждый эндпоинт — отдельный процесс; логи воркеров дописываются в общий `logs/gen_*.log`.

---

## Ollama (опционально)

```bash
python main.py -d /interfaces --ollama
python main.py --ollama --ollama-features describe
```

| Фича | Назначение |
|------|------------|
| `describe` | Человекочитаемые `description` в сценариях |
| `enrich` | Более осмысленные строки вместо JSF-мусора |

Нужен локальный Ollama. Без `--ollama` — шаблонные описания `Auto-test: POST …`.

---

## Типичные задачи и чеклисты

### Шпаргалка

| Задача | Куда смотреть |
|--------|----------------|
| Новый эндпоинт в OpenAPI | `python main.py -e /path` |
| Тест требует VRF / ACL / jail | `field_mappings` + `mock_data.by_field` |
| Безопасные IP / маски | `mock_data.by_schema` |
| Создаваемый ifname (bond, tunnel, vlan) | `interface_rules` + `.env` |
| Bond после VLAN-slave | `"setup_defer": true` на bond |
| Порядок teardown | `teardown_priority` (меньше → раньше) |
| Эндпоинт с `action` или `entry_type` | `endpoint_rules` + `bind_fields` / `lifecycle_key_field` |
| Mapping мешает своему add | `skip_targets` |
| Delete без поля в payload | `synthetic_bind_fields` + `mock_data` |
| icmp/ports ⇒ protocol | `field_couplings` |
| Никогда не трогать vlan1 / vid 603 | `reserved_values` + `cleanup.json` skip |
| Предочистка устройства | `clear_for_tests.py` + `cleanup.json` |
| Править dependencies кнопками | `deps_editor.py` |
| Проверить генератор без API | `run_unit_tests.py -v` |

### Чеклист: поле в payload (VRF, zone, ACL)

1. Ключ в `field_mappings` с `setup` / `teardown`.
2. Значения в `mock_data.by_field`.
3. При необходимости `optional`, `teardown_priority`, `skip_targets`.
4. `python main.py -e /ваш/эндпоинт` и прогон одного теста.

### Чеклист: интерфейс

1. Правило в `interface_rules.ifname.rules` (специфичные выше).
2. Строка в `.env` (`env`) или `allowed`.
3. Для VLAN/eth_vlan: `"vid": "{{vid}}"`.
4. Убедиться, что `interface_lifecycle.schema_components` покрывает `$ref` поля.

### Чеклист: эндпоинт с action

1. Ключ в `endpoint_rules` (полный путь).
2. `bind_fields` для плейсхолдеров.
3. Блоки `add` / `delete` / `modify` или top-level setup/teardown.
4. Если ветвление не по `action` — `lifecycle_key_field`.

### Чеклист: после FAIL на устройстве

1. В `logs/run_*.log` найти `Result: FAIL` и текст `errCode`.
2. Классифицировать: плохие значения → `mock_data` / `.env`; не хватает ресурса → `field_mappings` / `endpoint_rules` / `interface_rules`.
3. Поправить JSON / `.env`.
4. Перегенерировать только нужную группу (`-d` / `-e`).
5. `clear_for_tests` при необходимости → снова `run_tests`.

**Не** чинить конкретные эндпоинты в Python — расширяйте конфиг.

---

## Зависимости Python

Установка:

```bash
pip install -r requirements.txt
```

| Пакет | Зачем |
|-------|-------|
| `jsf` | Генерация значений по JSON Schema |
| `jsonschema` | Валидация пейлоадов (транзитивно через jsf и код) |
| `requests` | HTTP в `run_tests.py`, `clear_for_tests.py`, Ollama |
| `PyQt6` | GUI `deps_editor.py` |

Python **3.10+** (аннотации `list[str] | None`, `match` и т.п.).

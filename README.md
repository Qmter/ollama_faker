# llm_faker

Генератор REST API тестов из OpenAPI-схемы. Берёт `openapi.json`, строит пейлоады по JSON Schema (JSF + целенаправленное покрытие значений), оборачивает их в сценарии с `setup` / `main_test` / `teardown` и сохраняет в `tests/`.

## Быстрый старт

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install jsf jsonschema requests

cp .env.example .env
# отредактируйте списки интерфейсов под своё устройство

python main.py
```

Результат:
- `tests/<endpoint>_<method>.json` — готовые тест-сценарии
- `test.log` — подробный лог генерации (покрытие полей/значений, пропуски валидации)

## Структура проекта

| Файл | Назначение |
|------|------------|
| `main.py` | Точка входа: генерация пейлоадов, lifecycle, сборка сценариев |
| `resolve_scheme.py` | Разрешение `$ref`, обход схемы, извлечение полей и ограничений |
| `dependencies.json` | Зависимости между полями и lifecycle интерфейсов |
| `openapi.json` | OpenAPI-спецификация API |
| `.env` | Инвентарь реальных имён интерфейсов на устройстве |
| `tests/` | Сгенерированные JSON-сценарии |
| `test.log` | Лог последнего запуска |
| `ollama_orchestrator.py` | Опциональная интеграция с Ollama (не используется по умолчанию) |

## Как это работает

```
openapi.json
    │
    ▼
ResolveScheme.resolve_endpoint()     ← разворачивает $ref для эндпоинта
    │
    ▼
preprocess_schema_for_jsf()          ← oneOf/anyOf → enum, nullable, type: object
    │
    ▼
apply_interface_inventory()          ← pattern → enum из .env
    │
    ▼
generate_value_coverage_payloads()   ← enum/boolean/границы чисел, oneOf вложенно
    + JSF (добор сложных полей)
    │
    ▼
build_test_scenarios()               ← setup/teardown из dependencies.json
    │
    ▼
tests/*.json
```

### Покрытие тестами

1. **Покрытие значений** — для каждого поля схемы генерируются отдельные пейлоады:
   - `enum` → все варианты
   - `boolean` → `true` / `false`
   - числа → min/max через под-схемы + JSF
   - вложенные `oneOf` / `anyOf` / `allOf` раскрываются рекурсивно

2. **Покрытие полей** — если после шага 1 остались непокрытые поля, JSF добирает случайными пейлоадами (до 50 попыток).

3. **Валидация** — каждый пейлоад проверяется через `jsonschema` перед добавлением. Невалидные варианты пропускаются и попадают в `test.log`.

В логе смотрите строку вида:
```
Покрытие значений: 36 пейлоадов, целей 39/39
```

## Настройка эндпоинтов

Список генерируемых эндпоинтов задаётся в `main.py`:

```python
target_endpoints = [
    "/interfaces/ethernet/capability",
    "/vrf",
    # ...
]
```

Добавьте путь из `openapi.json` и перезапустите `python main.py`.

## dependencies.json

Два раздела с **единой моделью lifecycle**: `setup` / `teardown` (кастом) или `create` / `delete` (простой эндпоинт).

### field_mappings

Срабатывает, когда в пейлоаде встречается поле по имени (`vrf_name`, `acl_name` и т.д.).

```json
"vrf_name": {
  "setup": {
    "endpoint": "/vrf",
    "payload": { "action": "add", "vrf_name": "{{vrf_name}}" },
    "extract_to_variable": "created_vrf_name",
    "response_extract": "data.vrf_name"
  },
  "teardown": {
    "endpoint": "/vrf",
    "payload": { "action": "delete", "vrf_name": "{{vrf_name}}" },
    "note": "Cleanup created vrf_name"
  },
  "optional": true
}
```

| Поле | Описание |
|------|----------|
| `optional` | если `true`, пустое значение не трогаем |
| `setup` / `teardown` | кастомный шаг (объект или **список** объектов) |
| `create` / `delete` | простой эндпоинт (строка или **список** строк) |
| `action_create` / `action_delete` | для legacy-формата с `provider` |
| `extract` | путь извлечения из ответа setup (по умолчанию `data.<field>`) |

**Legacy-формат** (всё ещё работает):
```json
"provider": "/vrf",
"action_create": "add",
"action_delete": "delete",
"extract": "data.vrf_name"
```

### interface_rules

Срабатывает для полей из секции правил (сейчас — `ifname`) на **любом уровне** вложенности: `ifname`, `port[].ifname` и т.д.

Правила проверяются **сверху вниз** — первое совпадение побеждает.

| Ключ правила | Описание |
|--------------|----------|
| `pattern` | regex для полного совпадения (`^eth(0\|1)$`) |
| `prefix` | имя начинается с префикса (`bond`, `br`, `tunnel`) |
| `env` | переменная из `.env` / `os.environ` со списком имён |
| `allowed` | список имён прямо в JSON (альтернатива `env`) |
| `create` / `delete` | простой lifecycle |
| `setup` / `teardown` | кастомный lifecycle |

Пример физического eth — только reset после теста:
```json
{
  "pattern": "^eth(0|[1-9][0-9]{0,3})$",
  "env": "DEVICE_ETH_IFNAMES",
  "teardown": {
    "endpoint": "/interfaces/common/reset_interface",
    "payload": { "ifname": "{{ifname}}" },
    "note": "Reset physical ethernet to defaults"
  }
}
```

Пример tunnel — кастомный setup с дополнительными полями:
```json
{
  "prefix": "tunnel",
  "setup": {
    "endpoint": "/interfaces/tunnel/add",
    "payload": {
      "ifname": "{{ifname}}",
      "settings": { "source": "1.1.1.1" }
    },
    "extract_to_variable": "created_ifname_tunnel",
    "response_extract": "data.ifname"
  },
  "teardown": "/interfaces/tunnel/delete"
}
```

### Несколько шагов в одном lifecycle

Любой из ключей `create`, `delete`, `setup`, `teardown` может быть **списком**:

```json
"teardown": [
  {
    "endpoint": "/interfaces/tunnel/ip_address",
    "payload": { "ifname": "{{ifname}}", "action": "delete" },
    "note": "Remove tunnel IP"
  },
  {
    "endpoint": "/interfaces/tunnel/delete",
    "payload": { "ifname": "{{ifname}}" },
    "note": "Delete tunnel"
  }
]
```

```json
"create": [
  "/interfaces/bonding/add",
  "/interfaces/bonding/capability"
]
```

Шаги выполняются по порядку. Для setup действует **self-skip**: если эндпоинт шага совпадает с тестируемым — этот шаг пропускается. Teardown добавляется всегда.

### Плейсхолдеры

В `payload` шагов можно использовать `{{field_name}}`, `{{ifname}}` и другие переменные — они подставляются из значений пейлоада.

## .env — инвентарь интерфейсов

Список реальных имён на тестовом устройстве. Привязывается к правилу через `"env": "DEVICE_ETH_IFNAMES"`.

```env
DEVICE_ETH_IFNAMES=eth0,eth1
DEVICE_ETH_VLAN_IFNAMES=eth0.100,eth1.200
DEVICE_SWITCHPORT_IFNAMES=switchport1,switchport2
DEVICE_VLAN_IFNAMES=vlan100,vlan200
```

Приоритет: `os.environ` → `.env` → `allowed` в правиле.

Инвентарь подменяет `pattern` на `enum` в схеме — JSF и покрытие значений используют только реальные имена.

**Добавить новый тип интерфейса:**
1. Правило в `dependencies.json` (`pattern` или `prefix` + `env`)
2. Строка в `.env`
3. Код менять не нужно

## Формат выходного теста

```json
{
  "test_id": 1,
  "setup": [
    {
      "endpoint": "/vrf",
      "method": "POST",
      "payload": { "action": "add", "vrf_name": "test" },
      "expected_status": 200,
      "extract_to_variable": "created_vrf_name",
      "response_extract": "data.vrf_name"
    }
  ],
  "main_test": {
    "endpoint": "/interfaces/bridge/add",
    "method": "POST",
    "payload": { "ifname": "br0", "vrf_name": "test" },
    "expected_status": 200
  },
  "teardown": [
    {
      "endpoint": "/vrf",
      "method": "POST",
      "payload": { "action": "delete", "vrf_name": "test" },
      "expected_status": 200,
      "note": "Cleanup created vrf_name"
    },
    {
      "endpoint": "/interfaces/bridge/delete",
      "method": "POST",
      "payload": { "ifname": "br0" },
      "expected_status": 200,
      "note": "Cleanup ifname"
    }
  ],
  "description": "Автотест: POST /interfaces/bridge/add"
}
```

Имя файла: `tests/<path_без_слешей>_<method>.json`, например `interfaces_ethernet_capability_post.json`.

## Логирование

- Файл: `test.log` (перезаписывается при каждом запуске)
- Уровень INFO по умолчанию
- Для отладки в `main.py` раскомментируйте:
  ```python
  logging.getLogger().setLevel(logging.DEBUG)
  ```

Полезные сообщения:
- `Покрытие значений: N пейлоадов, целей X/Y` — покрытие enum/boolean/границ
- `Не покрыто целевых значений` — пейлоады не прошли jsonschema
- `Self-skip setup` — setup пропущен, т.к. тестируем тот же эндпоинт
- `Инвентарь для pattern` — какие имена подставлены из `.env`

## Ollama (опционально)

В `main.py` установите `USE_OLLAMA = True` — тогда `description` тестов будет генерироваться через локальную Ollama (`qwen2.5-coder:7b`). По умолчанию выключено.

## Зависимости

| Пакет | Зачем |
|-------|-------|
| `jsf` | генерация данных по JSON Schema |
| `jsonschema` | валидация пейлоадов |
| `requests` | только для Ollama |

## Типичные задачи

### Добавить новый эндпоинт
1. Проверить путь в `openapi.json`
2. Добавить в `target_endpoints` в `main.py`
3. `python main.py`

### Тест использует vrf/acl, которых нет на устройстве
Добавить поле в `field_mappings` — setup создаст зависимость перед тестом, teardown удалит после.

### Тест ломает физический порт
Для `eth0`, `switchport1` и т.п. — правило с `teardown` → `reset_interface` в `interface_rules`.

### Создаваемый интерфейс (bond, br, tunnel)
Правило с `create`/`delete` или кастомным `setup`/`teardown`. Для tunnel часто нужен кастомный `setup` с `settings`.

### Не все значения поля покрыты
Смотреть `test.log` → `Не покрыто целевых значений`. Обычно это вложенный `oneOf` или невалидная комбинация required-полей. Генератор пытается собрать минимальный валидный контейнер вокруг тестируемого поля.

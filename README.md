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

С аргументами:

```bash
python main.py                              # все POST-эндпоинты из openapi.json
python main.py -e /vrf                      # один эндпоинт
python main.py -e /vrf /ipsla /ipsla/config # несколько эндпоинтов
python main.py -v                           # debug-логирование в test.log
python main.py -e /vrf -v                   # эндпоинт + debug
```


| Аргумент             | Описание                                  |
| -------------------- | ----------------------------------------- |
| `-e PATH [PATH ...]` | Эндпоинт или список эндпоинтов (POST)     |
| `-v`                 | Debug-режим логирования                   |
| без аргументов       | Генерация для всех POST из `openapi.json` |


Результат:

- `tests/<endpoint>_<method>.json` — готовые тест-сценарии
- `test.log` — подробный лог генерации (покрытие полей/значений, пропуски валидации)

## Структура проекта


| Файл                     | Назначение                                                                                                                  |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| `main.py`                | Точка входа: генерация пейлоадов, lifecycle, сборка сценариев                                                               |
| `resolve_scheme.py`      | Разрешение `$ref`, обход схемы, извлечение полей и ограничений                                                              |
| `dependencies.json`      | Зависимости полей, lifecycle интерфейсов и правила по `action` — см. [документацию](#dependenciesjson--полная-документация) |
| `openapi.json`           | OpenAPI-спецификация API                                                                                                    |
| `.env`                   | Инвентарь реальных имён интерфейсов на устройстве                                                                           |
| `tests/`                 | Сгенерированные JSON-сценарии                                                                                               |
| `test.log`               | Лог последнего запуска                                                                                                      |
| `ollama_orchestrator.py` | Опциональная интеграция с Ollama (не используется по умолчанию)                                                             |


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

Эндпоинты задаются через аргумент `-e` при запуске. Без него обрабатываются **все POST** из `openapi.json`:

```bash
python main.py -e /interfaces/ethernet/capability /vrf
```

Список доступных путей можно посмотреть в `openapi.json` или через `l.py`.

## dependencies.json — полная документация

Файл `dependencies.json` описывает **зависимости между полями пейлоада и побочными HTTP-запросами** (setup/teardown), которые генератор добавляет в каждый тест-сценарий. Без этого файла тесты не будут создавать/удалять VRF, ACL, интерфейсы и другие сущности, от которых зависит API.

### Структура файла

```json
{
  "field_mappings": { ... },
  "endpoint_rules": { ... },
  "interface_rules": { ... }
}
```


| Раздел            | Когда срабатывает                                                                                                |
| ----------------- | ---------------------------------------------------------------------------------------------------------------- |
| `field_mappings`  | В пейлоаде (на любом уровне вложенности) встречается ключ с таким именем: `vrf_name`, `acl_name`, `vrf`…         |
| `interface_rules` | В пейлоаде встречается поле из секции правил (сейчас — `ifname`) с именем, подходящим под `pattern` или `prefix` |
| `endpoint_rules`  | Тестируемый эндпоинт совпадает с ключом, **и** в `main_test.payload` есть поле `action`                          |


Все три раздела опциональны. Можно использовать только нужные.

### Порядок применения при генерации

Для каждого сгенерированного пейлоада `build_test_scenarios()` выполняет:

1. Извлекает `action` из `main_test.payload` (если есть).
2. `**field_mappings`** — ищет зависимости рекурсивно по всему дереву пейлоада.
3. `**endpoint_rules**` — добавляет setup/teardown для самого тестируемого API по `action`.
4. `**interface_rules**` — ищет поле `ifname` (и другие ключи из секции) на любом уровне.
5. Подставляет плейсхолдеры `{{...}}` в финальные пейлоады setup/main/teardown.

Шаги из разных разделов **складываются**: setup всех источников идёт в начало сценария, teardown — в конец (в порядке добавления).

---

### Единая модель lifecycle

У каждой зависимости есть две фазы:


| Фаза                             | Ключи в JSON            | Назначение                            |
| -------------------------------- | ----------------------- | ------------------------------------- |
| **Setup** (до `main_test`)       | `setup` или `create`    | Создать ресурс, подготовить окружение |
| **Teardown** (после `main_test`) | `teardown` или `delete` | Удалить ресурс, сбросить состояние    |


**Два формата одного и того же:**


| Простой                                  | Кастомный                                             |
| ---------------------------------------- | ----------------------------------------------------- |
| `"create": "/interfaces/bonding/add"`    | `"setup": { "endpoint": "...", "payload": {...} }`    |
| `"delete": "/interfaces/bonding/delete"` | `"teardown": { "endpoint": "...", "payload": {...} }` |


Простой формат (`create` / `delete`) — это сокращение: генератор сам собирает POST-запрос с телом `{ "<имя_поля>": "<значение>" }` или legacy-форматом с `"action": "add"`.

Любой из ключей `setup`, `teardown`, `create`, `delete` может быть **одним объектом/строкой** или **списком** — шаги выполняются по порядку.

**Приоритет внутри правила интерфейса:** если заданы и `setup`, и `create` — в setup попадут оба (сначала кастомный `setup`, затем шаги из `create`). Для teardown: `teardown` имеет приоритет над `delete` (если есть `teardown`, `delete` игнорируется).

---

### Синтаксис шага (кастомный setup / teardown)

Объект шага в `setup` или `teardown`:

```json
{
  "endpoint": "/vrf",
  "method": "POST",
  "payload": {
    "action": "add",
    "vrf_name": "{{vrf_name}}"
  },
  "expected_status": 200,
  "note": "Создать VRF перед тестом",
  "extract_to_variable": "created_vrf_name",
  "response_extract": "data.vrf_name"
}
```


| Поле                  | Обязательное | По умолчанию   | Описание                                                    |
| --------------------- | ------------ | -------------- | ----------------------------------------------------------- |
| `endpoint`            | да           | —              | Путь API (как в OpenAPI)                                    |
| `payload`             | нет          | `{}`           | Тело запроса; поддерживает `{{плейсхолдеры}}`               |
| `method`              | нет          | `"POST"`       | HTTP-метод                                                  |
| `expected_status`     | нет          | `200`          | Ожидаемый код ответа                                        |
| `note`                | нет          | —              | Комментарий в выходном JSON (для человека/раннера)          |
| `extract_to_variable` | нет          | auto           | Имя переменной для значения из ответа setup (только setup)  |
| `response_extract`    | нет          | `data.<field>` | Путь к полю в JSON-ответе (точечная нотация: `data.ifname`) |


Для **первого** setup-шага без явного `extract_to_variable` генератор подставляет:

- `extract_to_variable` → `created_<имя_поля>` (для `ifname` → `created_ifname_tunnel0` с санитизацией)
- `response_extract` → из поля `extract` конфига или `data.<имя_поля>`

---

### Плейсхолдеры

В `payload` шагов можно писать `{{имя}}` или `{{ имя }}` — они заменяются на реальные значения перед записью в `tests/*.json`.

**Автоматически доступны:**


| Переменная                                    | Откуда                                                      |
| --------------------------------------------- | ----------------------------------------------------------- |
| `{{vrf_name}}`, `{{acl_name}}`, `{{ifname}}`… | Значение поля из пейлоада `main_test`                       |
| `{{ifname}}`                                  | Дублирует значение поля `ifname` (удобно в interface_rules) |
| `created_<field>`                             | После setup — значение из ответа или исходное значение поля |


Для `endpoint_rules` дополнительно в `bind_fields` перечисляются поля из блока `action`, которые попадут в плейсхолдеры (`{{vrf}}`, `{{chain}}`…).

#### w поля (dotted-пути)

Дополнительно поддерживаются плейсхолдеры с точкой — значение берётся из **вложенного** пейлоада `main_test`:

```json
"ifname": "{{settings.source}}"
```

Пример: тест `POST /interfaces/tunnel/add` с телом

```json
{
  "ifname": "tunnel0",
  "settings": { "source": "eth1" }
}
```

→ в teardown подставится `"ifname": "eth1"`.

**Приоритет подстановки:**

1. Явные переменные lifecycle (`variables`) — `{{vrf_name}}`, `{{ifname}}` после setup и т.п.
2. Dotted-путь в пейлоаде `main_test` — `{{settings.source}}`, `{{action.add.acl_name}}`…

Если плейсхолдер не удалось разрешить, он остаётся в JSON как есть (например `{{missing.field}}`).

Типичный кейс — teardown в `interface_rules`, когда нужно сослаться на вложенное поле, а не на ключ верхнего уровня:

```json
"teardown": {
  "endpoint": "/interfaces/common/reset_interface",
  "payload": {
    "ifname": "{{settings.source}}"
  },
  "note": "Reset physical port used as tunnel source"
}
```

Для полей верхнего уровня по-прежнему достаточно `{{ifname}}` или `{{source}}` (если в пейлоаде есть ключ `source`).

---

### field_mappings

Ключ верхнего уровня — **имя поля в пейлоаде** (должно совпасть с ключом в JSON тела запроса).

```json
"field_mappings": {
  "vrf_name": {
    "setup": {
      "endpoint": "/vrf",
      "payload": {
        "action": "add",
        "vrf_name": "{{vrf_name}}"
      },
      "extract_to_variable": "created_vrf_name",
      "response_extract": "data.vrf_name"
    },
    "teardown": {
      "endpoint": "/vrf",
      "payload": {
        "action": "delete",
        "vrf_name": "{{vrf_name}}"
      },
      "note": "Cleanup created vrf_name"
    },
    "optional": true
  }
}
```

#### Поля конфигурации field_mapping


| Поле       | Тип             | Описание                                                                           |
| ---------- | --------------- | ---------------------------------------------------------------------------------- |
| `setup`    | объект | список | Кастомный setup (см. синтаксис шага)                                               |
| `teardown` | объект | список | Кастомный teardown                                                                 |
| `create`   | строка | список | URL эндпоинта создания (простой формат)                                            |
| `delete`   | строка | список | URL эндпоинта удаления                                                             |
| `optional` | bool            | Если `true` и значение поля пустое (`null`, `""`, `[]`) — lifecycle не добавляется |
| `extract`  | строка          | Путь извлечения из ответа setup (по умолчанию `data.<имя_поля>`)                   |


#### Legacy-формат (поддерживается)

Старый стиль с `provider` + `action_create` / `action_delete` по-прежнему работает:

```json
"vrf_name": {
  "provider": "/vrf",
  "action_create": "add",
  "action_delete": "delete",
  "extract": "data.vrf_name",
  "optional": true
}
```

Эквивалентно `create`/`delete` на `/vrf` с payload `{"action": "add", "vrf_name": "..."}`.

#### Action-aware lifecycle (поле `action` в main_test)

Если в пейлоаде `main_test` есть `action`, генератор выбирает фазы lifecycle для **field_mappings на том же эндпоинте**, что и setup/teardown зависимости:


| `main_test.action` | Зависимость на **другом** эндпоинте (prerequisite) | Зависимость на **том же** эндпоинте |
| ------------------ | -------------------------------------------------- | ----------------------------------- |
| `add`              | setup + teardown                                   | только teardown                     |
| `delete`           | setup + teardown                                   | только setup                        |
| `modify`           | setup + teardown                                   | setup + teardown                    |
| нет `action`       | setup + teardown                                   | setup + teardown                    |


**Prerequisite** — когда setup или teardown указывает на эндпоинт, отличный от тестируемого. Пример: тест `POST /acl/filter/filter_ipv4` с полем `vrf` → setup на `/vrf` всегда нужен, независимо от `action`.

Форматы `action` в пейлоаде, которые распознаёт генератор:

```json
{ "action": "add", "vrf_name": "test" }
```

```json
{
  "action": {
    "add": { "vrf_name": "test", "chain": "input" }
  }
}
```

Поддерживаемые глаголы: `add`, `delete`, `modify`, `clear`.

#### Пример: ACL с вложенным action

```json
"acl_name": {
  "setup": {
    "endpoint": "/acl/acl_ipv4",
    "payload": {
      "action": {
        "add": {
          "acl_name": "{{acl_name}}",
          "rule": { "dpi": { "dpi_protocol": "sina(weibo)" } }
        }
      }
    },
    "extract_to_variable": "created_acl_name",
    "response_extract": "data.acl_name"
  },
  "teardown": {
    "endpoint": "/acl/acl_ipv4",
    "payload": {
      "action": {
        "delete": { "acl_name": "{{acl_name}}" }
      }
    }
  },
  "optional": true
}
```

---

### interface_rules

Срабатывает для полей, перечисленных в этом разделе (сейчас — `ifname`). Поиск **рекурсивный**: `ifname`, `port[0].ifname`, `settings.ifname` и т.д.

Правила внутри одного поля проверяются **сверху вниз** — побеждает **первое** совпадение.

#### Формат секции

**Рекомендуемый** — массив `rules`:

```json
"interface_rules": {
  "ifname": {
    "rules": [
      { "pattern": "^eth0$", "env": "DEVICE_ETH_IFNAMES", "teardown": { ... } },
      { "prefix": "bond", "create": "/interfaces/bonding/add", "delete": "/interfaces/bonding/delete" }
    ]
  }
}
```

**Legacy** — объект `prefixes` (автоматически разворачивается в `rules`):

```json
"interface_rules": {
  "ifname": {
    "prefixes": {
      "bond": {
        "create": "/interfaces/bonding/add",
        "delete": "/interfaces/bonding/delete"
      }
    }
  }
}
```

#### Поля одного правила


| Поле                 | Описание                                                                      |
| -------------------- | ----------------------------------------------------------------------------- |
| `pattern`            | Regex **полного** совпадения имени (`^eth(0|[1-9][0-9]{0,3})$`)               |
| `prefix`             | Имя **начинается** с строки (`bond`, `br`, `tunnel`, `vlan`)                  |
| `env`                | Имя переменной в `.env` / `os.environ` со списком имён через запятую          |
| `allowed`            | Список имён прямо в JSON (альтернатива `env`)                                 |
| `physical`           | Legacy: пометить как физический интерфейс без lifecycle (если нет `teardown`) |
| `setup` / `teardown` | Кастомные шаги (объект или список)                                            |
| `create` / `delete`  | Простые эндпоинты (строка или список)                                         |


`pattern` и `prefix` взаимоисключающие в одном правиле — используется тот, что задан.

#### Инвентарь имён (`env` / `allowed`)

Поля `env` и `allowed` влияют на **генерацию пейлоадов**, а не только на lifecycle:

1. Генератор читает список имён: `os.environ` → `.env` → `allowed`.
2. В OpenAPI-схеме для подходящего `pattern` подставляется `enum` с реальными именами.
3. JSF и покрытие значений используют только эти имена.

Пример `.env`:

```env
DEVICE_ETH_IFNAMES=eth0,eth1
DEVICE_SWITCHPORT_IFNAMES=switchport1,switchport2
```

#### Типовые паттерны правил

**Физический порт — только сброс после теста:**

```json
{
  "pattern": "^eth(0|[1-9][0-9]{0,3})$",
  "env": "DEVICE_ETH_IFNAMES",
  "setup": {
    "endpoint": "/interfaces/shutdown",
    "payload": { "ifname": "{{ifname}}", "adm_state": true },
    "note": "Включить порт перед тестом"
  },
  "teardown": [{
    "endpoint": "/interfaces/common/reset_interface",
    "payload": { "ifname": "{{ifname}}" },
    "note": "Reset physical ethernet to defaults"
  }]
}
```

**Создаваемый интерфейс — простой create/delete:**

```json
{
  "prefix": "bond",
  "create": "/interfaces/bonding/add",
  "delete": "/interfaces/bonding/delete"
}
```

**Tunnel — кастомный setup (нужны доп. поля в settings):**

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
  "teardown": [{
    "endpoint": "/interfaces/tunnel/delete",
    "payload": { "ifname": "{{ifname}}" }
  }]
}
```

**Только teardown (switchport):**

```json
{
  "pattern": "^switchport[1-8]$",
  "env": "DEVICE_SWITCHPORT_IFNAMES",
  "teardown": {
    "endpoint": "/interfaces/common/reset_interface",
    "payload": { "ifname": "{{ifname}}" }
  }
}
```

Один и тот же `ifname` обрабатывается **один раз** за сценарий (дедупликация по значению).

---

### endpoint_rules

Правила для **самого тестируемого эндпоинта**, когда в пейлоаде есть `action`. Ключ — путь API (`"/acl/filter/filter_ipv4"`).

```json
"endpoint_rules": {
  "/acl/filter/filter_ipv4": {
    "bind_fields": ["vrf", "chain", "acl_name"],
    "add": {
      "teardown": {
        "endpoint": "/acl/filter/filter_ipv4",
        "payload": {
          "action": {
            "delete": {
              "vrf": "{{vrf}}",
              "chain": "{{chain}}",
              "acl_name": "{{acl_name}}"
            }
          }
        }
      }
    },
    "delete": {
      "setup": [
        { "endpoint": "/acl/acl_ipv4", "payload": { ... }, "note": "Create acl" },
        { "endpoint": "/acl/filter/filter_ipv4", "payload": { ... }, "note": "Create filter" }
      ]
    },
    "modify": {
      "setup": [ ... ],
      "teardown": [ ... ]
    }
  }
}
```

#### Поля


| Поле                                  | Описание                                                                |
| ------------------------------------- | ----------------------------------------------------------------------- |
| `bind_fields`                         | Список полей из блока `action`, которые станут плейсхолдерами `{{...}}` |
| `add` / `delete` / `modify` / `clear` | Объект с ключами `setup` и/или `teardown` (объект или список шагов)     |


**Важно:** `endpoint_rules` срабатывают **только** если в `main_test.payload` распознан `action`. Для эндпоинтов без `action` (например `POST /interfaces/tunnel/add`) используйте `interface_rules` или `field_mappings`.

Self-skip для `endpoint_rules` **не применяется** — шаги добавляются как описано (логика «add → только teardown» зашита в сами правила).

---

### Self-skip setup

Если endpoint шага **setup** совпадает с тестируемым эндпоинтом, шаг **пропускается** — ресурс создаётся в `main_test`.

**Исключение:** `main_test.action == "delete"` — setup нужен (сначала создать, потом удалить в main_test).

Пример: тест `POST /interfaces/tunnel/add` с `ifname: tunnel0` — setup из `interface_rules` на тот же `/interfaces/tunnel/add` не добавляется; teardown (delete) остаётся.

В логе (`test.log`, уровень DEBUG): `Self-skip setup: тестируем /interfaces/tunnel/add, пропускаю /interfaces/tunnel/add`.

Teardown **никогда** не пропускается по self-skip.

---

### Несколько шагов в одном lifecycle

Любой ключ может быть массивом:

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

---

### Чеклист: добавить новую зависимость

**Поле в пейлоаде (VRF, ACL, имя сущности):**

1. Добавить ключ в `field_mappings` с `setup`/`teardown` или `create`/`delete`.
2. Указать `optional: true`, если поле может отсутствовать.
3. Перезапустить `python main.py`.

**Интерфейс по имени (`ifname`):**

1. Добавить правило в `interface_rules.ifname.rules` (**выше** менее специфичных правил).
2. Для физических портов — `pattern` + `env` в `.env`.
3. Для создаваемых (`bond`, `br`, `tunnel`) — `prefix` + `create`/`delete` или кастомный `setup`.
4. Перезапустить генерацию.

**Эндпоинт с `action` (add/delete/modify):**

1. Добавить ключ в `endpoint_rules` с путём API.
2. Для каждого глагола описать нужные `setup`/`teardown`.
3. Перечислить `bind_fields` для плейсхолдеров.

---

### Полный минимальный пример

```json
{
  "field_mappings": {
    "vrf_name": {
      "create": "/vrf",
      "delete": "/vrf",
      "action_create": "add",
      "action_delete": "delete",
      "extract": "data.vrf_name",
      "optional": true
    }
  },
  "interface_rules": {
    "ifname": {
      "rules": [
        {
          "prefix": "br",
          "create": "/interfaces/bridge/add",
          "delete": "/interfaces/bridge/delete"
        }
      ]
    }
  },
  "endpoint_rules": {}
}
```

При тесте `POST /interfaces/bridge/add` с `{ "ifname": "br0", "vrf_name": "mgmt" }` получится:

- setup: `POST /vrf` (создать vrf)
- main_test: `POST /interfaces/bridge/add`
- teardown: delete bridge, delete vrf (self-skip не мешает — create bridge в main_test)

## .env — инвентарь интерфейсов

Список реальных имён на тестовом устройстве. Привязывается к правилу в `interface_rules` через `"env": "DEVICE_ETH_IFNAMES"`. Подробнее — в разделе [interface_rules](#interface_rules).

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
- Уровень INFO по умолчанию; `-v` включает DEBUG

Полезные сообщения:

- `Покрытие значений: N пейлоадов, целей X/Y` — покрытие enum/boolean/границ
- `Не покрыто целевых значений` — пейлоады не прошли jsonschema
- `Self-skip setup` — setup пропущен, т.к. тестируем тот же эндпоинт
- `Инвентарь для pattern` — какие имена подставлены из `.env`

## Ollama (опционально)

В `main.py` установите `USE_OLLAMA = True` — тогда `description` тестов будет генерироваться через локальную Ollama (`qwen2.5-coder:7b`). По умолчанию выключено.

## Зависимости


| Пакет        | Зачем                           |
| ------------ | ------------------------------- |
| `jsf`        | генерация данных по JSON Schema |
| `jsonschema` | валидация пейлоадов             |
| `requests`   | только для Ollama               |


## Типичные задачи

### Добавить новый эндпоинт

1. Проверить путь в `openapi.json`
2. `python main.py -e /новый/путь`

### Тест использует vrf/acl, которых нет на устройстве

Добавить поле в `field_mappings` — см. [field_mappings](#field_mappings).

### Тест ломает физический порт

Правило с `teardown` → `reset_interface` в [interface_rules](#interface_rules).

### Создаваемый интерфейс (bond, br, tunnel)

`prefix` + `create`/`delete` или кастомный `setup`/`teardown` в [interface_rules](#interface_rules).

### Эндпоинт с action (add/delete/modify)

Правила в [endpoint_rules](#endpoint_rules).

### Не все значения поля покрыты

Смотреть `test.log` → `Не покрыто целевых значений`. Обычно это вложенный `oneOf` или невалидная комбинация required-полей. Генератор пытается собрать минимальный валидный контейнер вокруг тестируемого поля.
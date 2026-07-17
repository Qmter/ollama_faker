<!-- ollama_faker run analysis v5 -->
<!-- generated: 2026-07-17 15:42:42 -->
<!-- source: logs\run_20260717_151407_interfaces.log -->

# Отчёт анализа прогона тестов

**Лог прогона:** `logs\run_20260717_151407_interfaces.log`
**Время:** 695.69 с

## Сводка

- **Прошло:** 252
- **Не прошло:** 8
- **Всего:** 260

## Упавшие тесты

| Эндпоинт | # | Класс | Серьёзность | Диагноз |
| --- | ---: | --- | --- | --- |
| `/interfaces/bonding/mode` | 7 | Ошибка маршрутизатора | Ну и ладно | Операция не поддерживается на этом железе |
| `/interfaces/description` | 1 | Ошибка теста/генератора | Ну и ладно | Нет ключа description в payload |
| `/interfaces/description` | 3 | Ошибка теста/генератора | Ну и ладно | Отсутствие ключа description в payload |
| `/interfaces/ethernet/capability` | 9 | Ошибка маршрутизатора | Нужно чинить | HTTP 500 на main |
| `/interfaces/switchport/capability` | 37 | Ошибка теста/генератора | Нужно чинить | Main: ресурс не найден |
| `/interfaces/tunnel/ip_address` | 6 | Ошибка теста/генератора | Ну и ладно | Нет settings.destination в coverage_keys |
| `/interfaces/tunnel/ip_address` | 7 | Ошибка теста/генератора | Ну и ладно | Нет settings.destination в coverage_keys |
| `/interfaces/tunnel/ip_address` | 8 | Ошибка теста/генератора | Ну и ладно | Нет settings.destination в coverage_keys |

**Итого по классам:** Ошибка теста/генератора: 6; Ошибка маршрутизатора: 2

## Детальный разбор

### `/interfaces/bonding/mode` — тест #7

**Класс:** Ошибка маршрутизатора · **Серьёзность:** Ну и ладно · **Источник:** ollama
**Факт:** main → HTTP 500 (ожидали 200, errCode: -1, Error setting mode: Cannot connect interface eth1: (122, 'Operation not supported'))

**Coverage vs payload:**
- Coverage задал: `mode.mode_type="balance-alb"`
- В payload есть поля: `ifname`, `mode`, `mode.mode_type`, `mode.primary_interface`
- Пробел coverage→payload: settings.destination (нужен для gre/gretap)

**Запрос:**
```json
{
  "ifname": "bond2085",
  "mode": {
    "primary_interface": "eth1",
    "mode_type": "balance-alb"
  }
}
```

**Ответ:**
```json
{
  "errCode": [
    -1,
    "Error setting mode: Cannot connect interface eth1: (122, 'Operation not supported')"
  ],
  "result": null,
  "request": {
    "id": 2314,
    "rw": "w",
    "var": "interfaces/bonding/mode",
    "ifname": "bond2085",
    "mode": {
      "primary_interface": "eth1",
      "mode_type": "balance-alb"
    }
  }
}
```

**Диагноз:**
Устройство отклонило `/interfaces/bonding/mode` как неподдерживаемую на текущем NIC/платформе.

**Как исправить:**
1) Исключить эндпоинт/тест на этой платформе.
2) Сузить DEVICE_* в .env под реальное железо.
3) Не считать багом генератора, если на другом устройстве PASS.

### `/interfaces/description` — тест #1

**Класс:** Ошибка теста/генератора · **Серьёзность:** Ну и ладно · **Источник:** ollama
**Факт:** main → HTTP 500 (ожидали 200, errCode: -1, 'description')

**Coverage vs payload:**
- Coverage задал: `__minimal__`, `ifname="vlan100"`
- В payload есть поля: `ifname`

**Запрос:**
```json
{
  "ifname": "vlan100"
}
```

**Ответ:**
```json
{
  "errCode": [
    -1,
    "'description'"
  ],
  "result": null,
  "request": {
    "id": 6837,
    "rw": "w",
    "var": "interfaces/description",
    "ifname": "vlan100"
  }
}
```

**Диагноз:**
В схеме API для метода POST /interfaces/description требуется поле description, но оно отсутствует в тестовом запросе.

**Рассуждение:**
Тест не проходит из-за отсутствия обязательного поля description в payload. Нужно добавить это поле и обновить метаданные теста.

**Как исправить:**
1) Добавить поле description в payload запроса.
2) Обновить coverage_keys для включения ключа description.
3) Убедиться, что dependencies.json содержит необходимые зависимости для выполнения теста.

### `/interfaces/description` — тест #3

**Класс:** Ошибка теста/генератора · **Серьёзность:** Ну и ладно · **Источник:** ollama
**Факт:** main → HTTP 500 (ожидали 200, errCode: -1, 'description')

**Coverage vs payload:**
- Coverage задал: `ifname="vlan200"`
- В payload есть поля: `ifname`

**Запрос:**
```json
{
  "ifname": "vlan200"
}
```

**Ответ:**
```json
{
  "errCode": [
    -1,
    "'description'"
  ],
  "result": null,
  "request": {
    "id": 6888,
    "rw": "w",
    "var": "interfaces/description",
    "ifname": "vlan200"
  }
}
```

**Диагноз:**
В схеме API для метода POST /interfaces/description требуется поле description, но оно отсутствует в тестовом запросе.

**Рассуждение:**
Тестовый запрос не содержит необходимого поля description, что приводит к ошибке на устройстве. Нужно добавить это поле в payload и убедиться, что схема API соответствует требованиям.

**Как исправить:**
1) Добавить поле description в payload тестового запроса.
2) Обновить схему API для метода POST /interfaces/description, чтобы она требовала обязательного поля description.

### `/interfaces/ethernet/capability` — тест #9

**Класс:** Ошибка маршрутизатора · **Серьёзность:** Нужно чинить · **Источник:** ollama
**Факт:** main → HTTP 500 (ожидали 200, errCode: -1, Setting multicast MAC-address not allowed)

**Coverage vs payload:**
- Coverage задал: `capability.mac_address="BF-9B-4D-d0-D5-Ed"`
- В payload есть поля: `capability`, `capability.mac_address`, `ifname`

**Запрос:**
```json
{
  "ifname": "eth1",
  "capability": {
    "mac_address": "BF-9B-4D-d0-D5-Ed"
  }
}
```

**Ответ:**
```json
{
  "errCode": [
    -1,
    "Setting multicast MAC-address not allowed"
  ],
  "result": null,
  "request": {
    "id": 3499,
    "rw": "w",
    "var": "interfaces/ethernet/capability",
    "ifname": "eth1",
    "capability": {
      "mac_address": "BF-9B-4D-d0-D5-Ed"
    }
  }
}
```

**Диагноз:**
Сервер вернул 5xx — вероятная ошибка прошивки/API маршрутизатора.

**Рассуждение:**
HTTP-статус 500 указывает на внутреннюю ошибку сервера. Вероятно, это связано с ограничением или багом в прошивке маршрутизатора, который запрещает изменение multicast MAC-адреса.

**Как исправить:**
1) Повторить тот же payload вручную.
2) Если стабильно воспроизводится — эскалировать баг API с request/response из лога.
3) Генератор не трогать, пока не доказана ошибка теста.

### `/interfaces/switchport/capability` — тест #37

**Класс:** Ошибка теста/генератора · **Серьёзность:** Нужно чинить · **Источник:** ollama
**Факт:** main → HTTP 400 (ожидали 200, errCode: 9, MAC address/mask pair 5c:f5:1b:1b:ac:5a/d2:a2:4e:47:29:5a is not found)

**Coverage vs payload:**
- Coverage задал: `voicevlan.vlan=null`
- В payload есть поля: `voicevlan`, `voicevlan.mac_addr`, `voicevlan.mac_mask`, `voicevlan.vlan`

**Запрос:**
```json
{
  "voicevlan": {
    "mac_addr": "5cf5.1B1b.ac5A",
    "mac_mask": "D2A2.4e47.295A",
    "vlan": null
  }
}
```

**Ответ:**
```json
{
  "errCode": [
    9,
    "MAC address/mask pair 5c:f5:1b:1b:ac:5a/d2:a2:4e:47:29:5a is not found"
  ],
  "result": null,
  "request": {
    "id": 9911,
    "rw": "w",
    "var": "interfaces/switchport/capability",
    "voicevlan": {
      "mac_addr": "5cf5.1B1b.ac5A",
      "mac_mask": "D2A2.4e47.295A",
      "vlan": null
    }
  }
}
```

**Диагноз:**
Main обратился к несуществующему ресурсу (интерфейс/VLAN/MAC) — setup не создал нужное состояние или coverage ссылается на чужие данные.

**Как исправить:**
1) Сверить setup и bind_fields в dependencies.json.
2) Проверить mock_data для связанных полей.
3) Перегенерировать тест.

### `/interfaces/tunnel/ip_address` — тест #6

**Класс:** Ошибка теста/генератора · **Серьёзность:** Ну и ладно · **Источник:** ollama
**Факт:** main → HTTP 500 (ожидали 200, errCode: -1, Address must have 32-len prefix)

**Coverage vs payload:**
- Coverage задал: `peer_address="10.0.0.1"`
- В payload есть поля: `ifname`, `ip_addr`, `peer_address`

**Запрос:**
```json
{
  "ifname": "tunnel35",
  "ip_addr": "10.0.0.1/24",
  "peer_address": "10.0.0.1"
}
```

**Ответ:**
```json
{
  "errCode": [
    -1,
    "Address must have 32-len prefix"
  ],
  "result": null,
  "request": {
    "id": 2870,
    "rw": "w",
    "var": "interfaces/tunnel/ip_address",
    "ifname": "tunnel35",
    "ip_addr": "10.0.0.1/24",
    "peer_address": "10.0.0.1"
  }
}
```

**Диагноз:**
В coverage_keys отсутствует ключ для settings.destination, что не совместимо с требованиями API.

**Рассуждение:**
Тест не проверяет все необходимые параметры для создания туннеля, что приводит к ошибке на устройстве.

**Как исправить:**
1) Добавить ключ "settings.destination="10.0.0.2"" в coverage_keys.
2) Убедиться, что этот ключ корректен и соответствует требованиям API.

### `/interfaces/tunnel/ip_address` — тест #7

**Класс:** Ошибка теста/генератора · **Серьёзность:** Ну и ладно · **Источник:** ollama
**Факт:** main → HTTP 500 (ожидали 200, errCode: -1, Address must have 32-len prefix)

**Coverage vs payload:**
- Coverage задал: `peer_address="10.0.0.2"`
- В payload есть поля: `ifname`, `ip_addr`, `peer_address`

**Запрос:**
```json
{
  "ifname": "tunnel0",
  "ip_addr": "10.0.0.1/24",
  "peer_address": "10.0.0.2"
}
```

**Ответ:**
```json
{
  "errCode": [
    -1,
    "Address must have 32-len prefix"
  ],
  "result": null,
  "request": {
    "id": 5266,
    "rw": "w",
    "var": "interfaces/tunnel/ip_address",
    "ifname": "tunnel0",
    "ip_addr": "10.0.0.1/24",
    "peer_address": "10.0.0.2"
  }
}
```

**Диагноз:**
В coverage_keys отсутствует ключ для settings.destination, что не совместимо с ожидаемым payload.

**Рассуждение:**
Тест требует наличия settings.destination в coverage_keys для полной проверки payload. Отсутствие этого ключа приводит к ошибке на устройстве.

**Как исправить:**
1) Добавить settings.destination в coverage_keys.
2) Убедиться, что в dependencies.json есть соответствующий параметр.
3) Обновить mock_data для корректного тестирования.

### `/interfaces/tunnel/ip_address` — тест #8

**Класс:** Ошибка теста/генератора · **Серьёзность:** Ну и ладно · **Источник:** ollama
**Факт:** main → HTTP 500 (ожидали 200, errCode: -1, Address must have 32-len prefix)

**Coverage vs payload:**
- Coverage задал: `peer_address="10.0.0.3"`
- В payload есть поля: `ifname`, `ip_addr`, `peer_address`

**Запрос:**
```json
{
  "ifname": "tunnel0",
  "ip_addr": "10.0.0.1/24",
  "peer_address": "10.0.0.3"
}
```

**Ответ:**
```json
{
  "errCode": [
    -1,
    "Address must have 32-len prefix"
  ],
  "result": null,
  "request": {
    "id": 2092,
    "rw": "w",
    "var": "interfaces/tunnel/ip_address",
    "ifname": "tunnel0",
    "ip_addr": "10.0.0.1/24",
    "peer_address": "10.0.0.3"
  }
}
```

**Диагноз:**
В coverage_keys отсутствует ключ для settings.destination, что не совместимо с требованиями API.

**Рассуждение:**
Тест требует полного покрытия всех параметров, указанных в схеме OpenAPI. Отсутствие settings.destination приводит к ошибке на устройстве.

**Как исправить:**
1) Добавить ключ "settings.destination="10.0.0.2"" в coverage_keys.
2) Убедиться, что этот ключ корректен и соответствует требованиям API.

## Итог

### Нужно чинить

- `/interfaces/ethernet/capability` #9 — HTTP 500 на main
- `/interfaces/switchport/capability` #37 — Main: ресурс не найден

### Ну и ладно

- `/interfaces/bonding/mode` #7 — Операция не поддерживается на этом железе
- `/interfaces/description` #1 — Нет ключа description в payload
- `/interfaces/description` #3 — Отсутствие ключа description в payload
- `/interfaces/tunnel/ip_address` #6 — Нет settings.destination в coverage_keys
- `/interfaces/tunnel/ip_address` #7 — Нет settings.destination в coverage_keys
- `/interfaces/tunnel/ip_address` #8 — Нет settings.destination в coverage_keys

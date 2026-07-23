import requests
import json
import warnings
from urllib3.exceptions import InsecureRequestWarning
from deepdiff import DeepDiff

# Для красивых таблиц (если нет — будет вывод на f-строках)
try:
    from tabulate import tabulate
    USE_TABULATE = True
except ImportError:
    USE_TABULATE = False

warnings.simplefilter('ignore', InsecureRequestWarning)

# === Функция извлечения spec ===
def extract_spec_from_html(html: str) -> dict:
    start = html.find('var spec = {')
    if start == -1:
        raise Exception("Not found 'var spec = {'")
    start += len('var spec = ')
    
    depth, in_string, end = 0, False, start
    for i in range(start, len(html)):
        c = html[i]
        if in_string and c == '\\' and i + 1 < len(html):
            i += 1
            continue
        if c == '"':
            in_string = not in_string
        if not in_string:
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    return json.loads(html[start:end])


# === Формирование таблицы изменений ===
def format_diff_table(diff: dict, max_rows: int = 50) -> list[list[str]]:
    """Преобразует diff deepdiff в список строк для таблицы"""
    rows = []
    
    # 1. Изменённые значения
    if 'values_changed' in diff:
        for path, change in diff['values_changed'].items():
            old = change.get('old_value', '—')
            new = change.get('new_value', '—')
            old_str = json.dumps(old, ensure_ascii=False)[:50]
            new_str = json.dumps(new, ensure_ascii=False)[:50]
            if len(str(old)) > 50: old_str += '…'
            if len(str(new)) > 50: new_str += '…'
            rows.append([path, 'Changed', old_str, new_str])
    
    # 2. Добавленные поля
    if 'dictionary_item_added' in diff:
        items = diff['dictionary_item_added']
        if hasattr(items, '__iter__') and not isinstance(items, (str, dict)):
            items = list(items)
        for path in items[:max_rows]:
            rows.append([path, '+ Added', '—', 'NEW'])
    
    # 3. Удалённые поля
    if 'dictionary_item_removed' in diff:
        items = diff['dictionary_item_removed']
        if hasattr(items, '__iter__') and not isinstance(items, (str, dict)):
            items = list(items)
        for path in items[:max_rows]:
            rows.append([path, '- Deleted', 'X', '—'])
    
    # 4. Изменения в массивах
    if 'iterable_item_added' in diff:
        for path, new_val in diff['iterable_item_added'].items():
            rows.append([path, 'Added to array', '—', json.dumps(new_val)[:50]])
    
    if 'iterable_item_removed' in diff:
        for path, old_val in diff['iterable_item_removed'].items():
            rows.append([path, 'Removed from array', json.dumps(old_val)[:50], '—'])
    
    return rows[:max_rows]


# === Печать таблицы ===
def print_diff_table(rows: list[list[str]]):
    headers = ["Path", "Type", "Before", "After"]
    
    if not rows:
        print("No changes to display")
        return
    
    if USE_TABULATE:
        print(tabulate(rows, headers=headers, tablefmt="grid", stralign="left"))
    else:
        print(f"\n{headers[0]:<60} {headers[1]:<15} {headers[2]:<30} {headers[3]:<30}")
        print("-" * 140)
        for row in rows:
            print(f"{row[0]:<60} {row[1]:<15} {row[2]:<30} {row[3]:<30}")


# === Основной код ===
url = "https://10.65.5.125:8082"
html = requests.get(url, verify=False).text
target_spec = extract_spec_from_html(html)

print(f"Target openapi:")
print(f"Version: {target_spec.get('openapi')}")
print(f"Header: {target_spec.get('info', {}).get('title')}")
print(f"Endpoints: {len(target_spec.get('paths', {}))}")

# Загружаем базовый spec для сравнения
with open('openapi.json', 'r', encoding='utf-8') as f:
    current_spec = json.load(f)

# Сравниваем
diff = DeepDiff(current_spec, target_spec, ignore_order=True)

if not diff:
    print("\nThe specifications are identical!")
else:
    print("\nTable of changes:")
    rows = format_diff_table(diff)
    print_diff_table(rows)
    
    # Summary with clear labels
    total = len(rows)
    added = sum(1 for r in rows if 'Added' in r[1])
    removed = sum(1 for r in rows if 'Deleted' in r[1])
    changed = sum(1 for r in rows if 'Changed' in r[1])
    print(f"\nTotal: {total} changes | Added: {added} | Deleted: {removed} | Changed: {changed}")

# === Запрос на сохранение ===
if diff:  # спрашиваем только если есть изменения
    answer = input("\nReplace file openapi.json? [Y/n]: ").strip().lower()
    if answer in ('y', 'yes', ''):  # пустой ввод = да по умолчанию
        with open('openapi.json', 'w', encoding='utf-8') as f:
            json.dump(target_spec, f, indent=2, ensure_ascii=False)
        print("The openapi.json file has been updated.")
    elif answer in ('n', 'no'):
        print("The file has not been modified.")
    else:
        print("Unclear response. File not modified.")
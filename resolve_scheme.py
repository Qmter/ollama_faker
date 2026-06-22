import json
import logging

class ResolveScheme:

    @staticmethod
    def _resolve_ref(obj, components, seen=None):
        """
        Рекурсивно разрешает ссылки $ref в схеме OpenAPI.
        Обрабатывает циклические ссылки.
        """
        try:
            if seen is None: # Инициализируем множество для отслеживания циклических ссылок
                seen = set() 
            
            if isinstance(obj, dict):
                if "$ref" in obj:  # Если это ссылка на другую схему
                    try:
                        ref = obj["$ref"] # Получаем имя схемы из ссылки
                        if not isinstance(ref, str):
                            logging.debug(f"$ref не является строкой: {ref}, тип: {type(ref)}")
                            return obj
                            
                        if ref.startswith("#/components/schemas/"):  # Если это ссылка на схему
                            name = ref.split("/")[-1]  # Извлекаем имя схемы из ссылки
                            if not name:
                                logging.debug(f"Имя схемы пустое в ссылке: {ref}")
                                return obj
                                
                            key = f"schemas/{name}"  # Создаем ключ для схемы
                            if key in seen:  # Если мы уже видели эту схему
                                logging.debug(f"Обнаружена циклическая ссылка: {ref}")
                                return {"type": "object", "x-circular": True}  # Возвращаем объект с типом "object" и меткой "x-circular"
                            
                            if "schemas" not in components:
                                logging.debug(f"Компоненты не содержат схем: {components.keys()}")
                                return obj
                            
                            schema = components["schemas"].get(name) # Получаем схему из компонентов
                            if not schema: # Если схема не существует
                                logging.debug(f"Схема '{name}' не найдена в компонентах")
                                return obj
                                
                            seen.add(key) # Добавляем ключ в множество для отслеживания
                            resolved = ResolveScheme._resolve_ref(schema, components, seen) # Рекурсивно разрешаем схему
                            seen.discard(key) # Удаляем ключ из множества для отслеживания
                            return resolved # Возвращаем разрешенную схему
                        else:
                            logging.debug(f"Игнорируем ссылку, не начинающуюся с '#/components/schemas/': {ref}")
                            return obj # Возвращаем исходный объект, если это не ссылка на схему
                    except Exception as e:
                        logging.debug(f"Ошибка при разрешении ссылки {obj.get('$ref', 'unknown')}: {e}")
                        return obj
                
                # Рекурсивно разрешаем все значения в словаре
                result = {}
                for k, v in obj.items():
                    try:
                        result[k] = ResolveScheme._resolve_ref(v, components, seen)
                    except Exception as e:
                        logging.debug(f"Ошибка при разрешении ключа '{k}': {e}")
                        result[k] = v  # Сохраняем исходное значение при ошибке
                return result
                
            elif isinstance(obj, list):  # Если это список
                result = []
                for item in obj:
                    try:
                        resolved_item = ResolveScheme._resolve_ref(item, components, seen)
                        result.append(resolved_item)
                    except Exception as e:
                        logging.debug(f"Ошибка при разрешении элемента списка: {e}")
                        result.append(item)  # Сохраняем исходный элемент при ошибке
                return result
                
            return obj # Возвращаем исходный объект, если это не словарь или список
            
        except Exception as e:
            logging.debug(f"Критическая ошибка в _resolve_ref: {e}")
            raise

    @staticmethod
    def find_all_patterns_min_max(schema):
        """
        Находит ВСЕ pattern, minimum, maximum в УЖЕ РАЗРЕШЕННОЙ схеме.
        Если anyOf содержит несколько паттернов, возвращает их как массив.
        Если паттерн один, возвращает его как строку.
        """
        results = {} # Инициализируем словарь для хранения результатов
        
        try:
            def _deep_search(obj, field_name=""):
                try:
                    if isinstance(obj, dict):
                        # Проверяем текущий объект на наличие pattern, min, max
                        current_rules = {} # Инициализируем словарь для хранения результатов

                        # Обработка pattern
                        if 'pattern' in obj: # Если есть pattern
                            pattern_value = obj['pattern']
                            if isinstance(pattern_value, str):
                                current_rules.setdefault('pattern', []).append(pattern_value)
                            else:
                                logging.debug(f"Pattern не является строкой: {pattern_value}, тип: {type(pattern_value)}")
                        
                        # Обработка minimum и maximum
                        if 'minimum' in obj: # Если есть minimum
                            min_value = obj['minimum']
                            if isinstance(min_value, (int, float)):
                                current_rules['minimum'] = min_value
                            else:
                                logging.debug(f"Minimum не является числом: {min_value}, тип: {type(min_value)}")
                                
                        if 'maximum' in obj: # Если есть maximum
                            max_value = obj['maximum']
                            if isinstance(max_value, (int, float)):
                                current_rules['maximum'] = max_value
                            else:
                                logging.debug(f"Maximum не является числом: {max_value}, тип: {type(max_value)}")

                        # Обработка anyOf
                        if 'anyOf' in obj: # Если есть anyOf
                            patterns_from_anyof = [] # Инициализируем список для хранения паттернов из anyOf
                            for item in obj['anyOf']:  # Обходим каждый элемент в anyOf
                                try:
                                    if isinstance(item, dict) and 'pattern' in item:  # Если это словарь и есть pattern
                                        pattern_val = item['pattern']
                                        if isinstance(pattern_val, str):
                                            patterns_from_anyof.append(pattern_val)
                                        else:
                                            logging.debug(f"Pattern в anyOf не является строкой: {pattern_val}")
                                except Exception as e:
                                    logging.debug(f"Ошибка при обработке элемента anyOf: {e}")
                                    continue
                            
                            if patterns_from_anyof: # Если есть паттерны из anyOf
                                # Если паттернов несколько, сохраняем как массив
                                if len(patterns_from_anyof) > 1:
                                    current_rules['pattern'] = patterns_from_anyof
                                else:
                                    # Если паттерн один, сохраняем как строку
                                    current_rules['pattern'] = patterns_from_anyof[0]

                        # Сохраняем если нашли правила И есть имя поля
                        if current_rules and field_name:
                            results[field_name] = current_rules # Сохраняем результаты в словарь
                        
                        # Рекурсивно обходим properties - сохраняем имена полей
                        if 'properties' in obj:
                            properties = obj['properties']
                            if isinstance(properties, dict):
                                for prop_name, prop_schema in properties.items(): # Обходим свойства
                                    if isinstance(prop_name, str):
                                        _deep_search(prop_schema, prop_name) # Рекурсивно обходим свойства
                                    else:
                                        logging.debug(f"Имя свойства не является строкой: {prop_name}")
                            else:
                                logging.debug(f"Properties не является словарем: {properties}")
                        
                        # Рекурсивно обходим остальные значения без изменения имени поля
                        for key, value in obj.items():
                            if key not in ['properties', 'anyOf']:  # properties и anyOf уже обработали
                                _deep_search(value, field_name)  # Рекурсивно обходим остальные значения
                                
                    elif isinstance(obj, list): # Если это список
                        for item in obj: # Обходим элементы списка
                            _deep_search(item, field_name)  # Рекурсивно обходим элементы списка
                            
                except Exception as e:
                    logging.debug(f"Ошибка в _deep_search для поля '{field_name}': {e}")
            
            # Ищем в requestBody схемы
            request_body = schema.get('requestBody', {})
            if request_body and isinstance(request_body, dict):  # Если есть requestBody
                content = request_body.get('content', {})
                if isinstance(content, dict):
                    application_json = content.get('application/json', {})
                    if isinstance(application_json, dict):
                        json_schema = application_json.get('schema', {})
                        if json_schema:
                            _deep_search(json_schema)  # Рекурсивно обходим схему для application/json
                    else:
                        logging.debug("application/json не является словарем")
                else:
                    logging.debug("Content не является словарем")
            else:
                logging.debug("RequestBody отсутствует или не является словарем")
            
            # Ищем в parameters (query parameters)
            parameters = schema.get('parameters', []) # Получаем параметры
            if isinstance(parameters, list):
                for param in parameters:  # Обходим параметры
                    try:
                        if isinstance(param, dict):
                            param_schema = param.get('schema', {})  # Получаем схему
                            param_name = param.get('name', '')  # Получаем имя параметра
                            if param_name and isinstance(param_name, str):  # Если есть имя параметра
                                _deep_search(param_schema, param_name)  # Рекурсивно обходим схему параметра
                    except Exception as e:
                        logging.debug(f"Ошибка при обработке параметра: {e}")
                        continue
            else:
                logging.debug("Parameters не является списком")
            
            return results # Возвращаем словарь с результатами
            
        except Exception as e:
            logging.debug(f"Критическая ошибка в find_all_patterns_min_max: {e}")
            return {}

    @staticmethod
    def resolve_endpoint(openapi_file: str, endpoint_path: str, method: str = 'post'):
        """
        Разрешает схему для конкретного эндпоинта из OpenAPI спецификации.
        """
        try:
            # Проверяем входные параметры
            if not isinstance(openapi_file, str):
                raise ValueError(f"openapi_file должен быть строкой, получен: {type(openapi_file)}")
            
            if not isinstance(endpoint_path, str):
                raise ValueError(f"endpoint_path должен быть строкой, получен: {type(endpoint_path)}")
            
            method = method.lower()
            if method not in ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']:
                logging.debug(f"Неподдерживаемый метод HTTP: {method}, используется 'post'")
                # method = 'post'
                raise ValueError(f"Неподдерживаемый метод HTTP: {method}")
            
            # Проверяем существование файла
            import os
            if not os.path.exists(openapi_file):
                raise FileNotFoundError(f"Файл OpenAPI не найден: {openapi_file}")
            
            if not os.path.isfile(openapi_file):
                raise ValueError(f"Путь не является файлом: {openapi_file}")
            
            # Читаем файл OpenAPI
            try:
                with open(openapi_file, 'r', encoding='utf-8') as f:
                    schema = json.load(f)
            except json.JSONDecodeError as e:
                logging.debug(f"Ошибка парсинга JSON в файле {openapi_file}: {e}")
                raise
            except UnicodeDecodeError as e:
                logging.debug(f"Ошибка декодирования файла {openapi_file}: {e}")
                raise
            except Exception as e:
                logging.debug(f"Ошибка чтения файла {openapi_file}: {e}")
                raise
            
            # Проверяем структуру OpenAPI
            if 'paths' not in schema:
                raise ValueError(f"Файл {openapi_file} не содержит ключа 'paths'")
            
            paths = schema.get('paths', {})
            if not isinstance(paths, dict):
                raise ValueError(f"'paths' должен быть словарем, получен: {type(paths)}")
            
            if endpoint_path not in paths:
                available_paths = list(paths.keys())[:10]  # Показываем первые 10 путей
                raise KeyError(f"Эндпоинт '{endpoint_path}' не найден в OpenAPI. Доступные пути: {available_paths}")
            
            endpoint_info = paths[endpoint_path]
            if not isinstance(endpoint_info, dict):
                raise ValueError(f"Информация об эндпоинте '{endpoint_path}' не является словарем")
            
            if method not in endpoint_info:
                available_methods = list(endpoint_info.keys())
                raise KeyError(f"Метод '{method}' не найден для эндпоинта '{endpoint_path}'. Доступные методы: {available_methods}")
            
            endpoint = endpoint_info[method]
            if not isinstance(endpoint, dict):
                raise ValueError(f"Эндпоинт для метода '{method}' не является словарем")
            
            components = schema.get('components', {}) # Получаем компоненты
            if not isinstance(components, dict):
                logging.debug("Components не является словарем, используется пустой словарь")
                components = {}
            
            # Разрешаем ссылки в эндпоинте
            resolved_endpoint = ResolveScheme._resolve_ref(endpoint, components)
            
            return resolved_endpoint # Возвращаем разрешенный endpoint
            
        except FileNotFoundError as e:
            logging.debug(f"Ошибка: {e}")
            raise
        except json.JSONDecodeError as e:
            logging.debug(f"Ошибка формата JSON: {e}")
            raise
        except KeyError as e:
            logging.debug(f"Ошибка ключа: {e}")
            raise
        except ValueError as e:
            logging.debug(f"Ошибка значения: {e}")
            raise
        except Exception as e:
            logging.debug(f"Критическая ошибка в resolve_endpoint: {e}")
            raise
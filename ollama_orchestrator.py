import json
import re
import time
import requests
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)

class OllamaOrchestrator:
    def __init__(self, model: str = "qwen2.5-coder:7b", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        logger.info(f"Ollama orchestrator инициализирован: модель={model}, URL={base_url}")

    def _safe_log(self, text: str, max_len: int = 150) -> str:
        return text[:max_len] + "..." if len(text) > max_len else text

    def _extract_json(self, text: str) -> Dict[str, Any]:
        text = re.sub(r'^```(?:json)?\s*', '', text.strip())
        text = re.sub(r'\s*```$', '', text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        logger.warning("Ollama вернул невалидный JSON. Верну пустой dict.")
        return {}

    def _call_ollama(self, system: str, user: str, temperature: float = 0.2) -> Dict[str, Any]:
        payload = {
            "model": self.model,
            "prompt": user,
            "system": system,
            "format": "json",
            "stream": False,
            "options": {"temperature": temperature}
        }
        logger.debug(f"Запрос к Ollama | System: {self._safe_log(system)} | User: {self._safe_log(user)}")
        start = time.time()
        try:
            resp = self.session.post(f"{self.base_url}/api/generate", json=payload, timeout=120)
            resp.raise_for_status()
            raw = resp.json().get("response", "{}")
            elapsed = time.time() - start
            logger.debug(f"Ответ получен за {elapsed:.2f}с | Status: {resp.status_code} | Размер: {len(raw)} байт")
            return self._extract_json(raw)
        except requests.exceptions.RequestException as e:
            logger.warning(f"Сетевая ошибка Ollama: {e}")
            return {}
        except Exception as e:
            logger.warning(f"Ожидание от Ollama прервано: {e}")
            return {}

    def fix_payload(self, rules: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        logger.debug("Запуск fix_payload...")
        system = (
            "You are an API testing expert. Fix the provided JSON payload to strictly match the schema rules. "
            "Return ONLY the corrected JSON object. No markdown, no explanations. "
            "Rules: 1) Match types/patterns/enums/min-max. 2) Keep original structure. "
            "3) Fix wrong types. 4) Preserve existing valid fields."
        )
        user = f"Rules:\n{json.dumps(rules, indent=2)}\n\nPayload to fix:\n{json.dumps(payload, indent=2)}"
        result = self._call_ollama(system, user)
        if result:
            logger.info("fix_payload: успешно исправлен")
        else:
            logger.warning("fix_payload: вернул пустой ответ, использую оригинал")
        return result

    def enrich_test_metadata(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        logger.debug("Запуск enrich_test_metadata...")
        system = (
            "Analyze this API test scenario. Return ONLY a JSON object with these exact keys: "
            '"description" (string), "risk_level" (low/medium/high), '
            '"expected_behavior" (string), "notes" (string or array).'
        )
        user = f"Scenario:\n{json.dumps(scenario, indent=2)}"
        result = self._call_ollama(system, user)
        if result and isinstance(result, dict):
            for k in ("description", "risk_level", "expected_behavior", "notes"):
                if k in result:
                    scenario[k] = result[k]
            logger.info("enrich_test_metadata: добавлены метаданные")
        else:
            logger.warning("enrich_test_metadata: пропущено")
        return scenario

    def review_setup_teardown(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        logger.debug("Запуск review_setup_teardown...")
        system = (
            "Review the test scenario. Ensure setup creates exactly what main_test needs, "
            "and teardown cleans it up. Return ONLY the improved full scenario JSON."
        )
        user = f"Scenario:\n{json.dumps(scenario, indent=2)}"
        result = self._call_ollama(system, user)
        if result and isinstance(result, dict) and "main_test" in result and "setup" in result:
            logger.info("review_setup_teardown: структура обновлена")
            return result
        logger.warning("review_setup_teardown: валидный ответ не получен, оставляю как есть")
        return scenario
"""Optional Ollama integration for test generation."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("OLLAMA")

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_CACHE_DIR = ".ollama_cache"
DEFAULT_FEATURES = frozenset({"describe"})
OLLAMA_CLI_FEATURES = "describe,enrich"
ENRICH_FIELD_SUFFIXES = ("_name", "_id")


def _resolve_schema_type(schema: dict) -> str | None:
    field_type = schema.get("type")
    if isinstance(field_type, list):
        non_null = [t for t in field_type if t != "null"]
        return non_null[0] if non_null else None
    return field_type


def _should_enrich_field(field_name: str, value, field_schema: dict) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if field_schema.get("enum") or field_schema.get("const"):
        return False
    if _resolve_schema_type(field_schema) not in (None, "string"):
        return False
    if field_name.endswith(ENRICH_FIELD_SUFFIXES):
        return True
    return field_name in ("chain", "vrf")


def _collect_enrich_candidates(
    payload,
    field_schemas: dict[str, dict],
    path: str = "",
) -> list[dict]:
    candidates: list[dict] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child_path = f"{path}.{key}" if path else key
            if isinstance(value, str):
                schema = field_schemas.get(child_path)
                if schema and _should_enrich_field(key, value, schema):
                    candidates.append({
                        "path": child_path,
                        "field": key,
                        "value": value,
                        "schema": schema,
                    })
            else:
                candidates.extend(
                    _collect_enrich_candidates(value, field_schemas, child_path)
                )
    elif isinstance(payload, list):
        item_path = f"{path}[]" if path else "[]"
        for item in payload:
            candidates.extend(
                _collect_enrich_candidates(item, field_schemas, item_path)
            )
    return candidates


def _set_at_dotted_path(obj: dict, path: str, value: str) -> None:
    parts = [p for p in path.split(".") if p and p != "[]"]
    if not parts:
        return
    current = obj
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
        if isinstance(current, list):
            if not current:
                return
            current = current[0]
    last = parts[-1]
    if isinstance(current, dict):
        current[last] = value


def _parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("No JSON object in model response")
    return json.loads(cleaned[start : end + 1])


def _fits_field_schema(value: str, field_schema: dict) -> bool:
    try:
        import jsonschema

        jsonschema.validate(instance=value, schema=field_schema)
        return True
    except Exception:
        return False


def _fits_root_schema(payload: dict, schema: dict) -> bool:
    try:
        import jsonschema

        jsonschema.validate(instance=payload, schema=schema)
        return True
    except Exception:
        return False


def _fallback_name(field_name: str, payload_index: int, seq: int, used: set[str]) -> str:
    base = field_name
    for suffix in ENRICH_FIELD_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    base = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "resource"
    candidate = f"test_{base}_{payload_index}_{seq}"
    while candidate in used:
        seq += 1
        candidate = f"test_{base}_{payload_index}_{seq}"
    return candidate


class OllamaOrchestrator:
    """Thin client around Ollama /api/generate with cache and graceful fallback."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.3,
        timeout_sec: int = 120,
        cache_dir: str | Path = DEFAULT_CACHE_DIR,
        features: frozenset[str] | None = None,
    ):
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout_sec = timeout_sec
        self.cache_dir = Path(cache_dir)
        self.features = features or DEFAULT_FEATURES
        self._available: bool | None = None

    @classmethod
    def from_cli(cls, use_ollama: bool, features: str | None = None) -> OllamaOrchestrator:
        if use_ollama:
            raw = features or OLLAMA_CLI_FEATURES
            feat = frozenset(part.strip() for part in raw.split(",") if part.strip())
        else:
            feat = DEFAULT_FEATURES
        orchestrator = cls(enabled=use_ollama, features=feat)
        if not use_ollama:
            return orchestrator
        if orchestrator.is_available():
            logger.info(
                "Ollama: available (features: %s)",
                ", ".join(sorted(orchestrator.features)),
            )
        else:
            logger.warning("Ollama requested but unavailable; using defaults.")
        return orchestrator

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        if self._available is not None:
            return self._available
        try:
            import requests

            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            self._available = resp.status_code == 200
        except Exception as exc:
            logger.warning("Ollama unavailable: %s", exc)
            self._available = False
        return self._available

    def has_feature(self, name: str) -> bool:
        return name in self.features and self.is_available()

    def _cache_key(self, prompt: str) -> str:
        payload = f"{self.model}|{self.temperature}|{prompt}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _read_cache(self, key: str) -> str | None:
        path = self.cache_dir / f"{key}.txt"
        if path.is_file():
            logger.debug("Cache hit: %s", key[:12])
            return path.read_text(encoding="utf-8")
        return None

    def _write_cache(self, key: str, value: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{key}.txt"
        path.write_text(value, encoding="utf-8")

    def generate(self, prompt: str, *, use_cache: bool = True) -> str:
        """Low-level call to Ollama /api/generate."""
        if not self.is_available():
            raise RuntimeError("Ollama is not available")

        cache_key = self._cache_key(prompt)
        if use_cache:
            cached = self._read_cache(cache_key)
            if cached is not None:
                return cached

        import requests

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        logger.debug("Ollama request: model=%s, prompt_len=%d", self.model, len(prompt))
        resp = requests.post(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
        text = text.replace("```", "").strip()

        if use_cache and text:
            self._write_cache(cache_key, text)

        return text

    def generate_test_description(self, scenario: dict) -> str:
        """One-sentence English description for a test scenario."""
        prompt = (
            "Write a single sentence in English describing this API test. "
            "Only plain text, no quotes or markdown:\n"
            f"{json.dumps(scenario['main_test'], indent=2)}"
        )
        text = self.generate(prompt)
        return text if text else "Automatically generated test description."

    def _suggest_replacements(
        self,
        endpoint: str,
        method: str,
        payload: dict,
        candidates: list[dict],
        payload_index: int,
        used_names: set[str],
    ) -> dict[str, str]:
        fields = {item["path"]: item["value"] for item in candidates}
        taken = ", ".join(sorted(used_names)[:20]) or "(none)"
        prompt = (
            "You generate readable test identifiers for a network device REST API.\n"
            f"Endpoint: {method} {endpoint}\n"
            f"Test index: {payload_index}\n"
            "Replace random strings with short readable unique names.\n"
            "Rules:\n"
            "- lowercase letters, digits, underscores only\n"
            "- max 32 characters\n"
            "- each value must be unique\n"
            "- hint from field name and payload action when possible\n"
            f"- do not reuse these names: {taken}\n\n"
            "Fields to replace:\n"
            f"{json.dumps(fields, indent=2)}\n\n"
            "Payload context:\n"
            f"{json.dumps(payload, indent=2)}\n\n"
            "Reply with ONLY a JSON object mapping dotted field paths to new string values."
        )
        try:
            raw = self.generate(prompt)
            parsed = _parse_json_object(raw)
            if not isinstance(parsed, dict):
                raise ValueError("Model response is not a JSON object")
            return {
                str(key): str(value)
                for key, value in parsed.items()
                if isinstance(value, str)
            }
        except Exception as exc:
            logger.warning(
                "Enrich fallback for payload %d: %s",
                payload_index,
                exc,
            )
            return {}

    def enrich_payloads(
        self,
        payloads: list[dict],
        schema: dict,
        field_schemas: dict[str, dict],
        endpoint: str,
        *,
        method: str = "POST",
    ) -> list[dict]:
        """Replace random resource names with readable test identifiers."""
        if not self.has_feature("enrich"):
            return payloads

        enriched_payloads: list[dict] = []
        used_names: set[str] = set()
        replaced_total = 0

        for idx, payload in enumerate(payloads, 1):
            enriched = copy.deepcopy(payload)
            candidates = _collect_enrich_candidates(enriched, field_schemas)
            if not candidates:
                enriched_payloads.append(enriched)
                continue

            suggestions = self._suggest_replacements(
                endpoint, method, enriched, candidates, idx, used_names,
            )
            for seq, cand in enumerate(candidates):
                path = cand["path"]
                new_value = suggestions.get(path) or suggestions.get(cand["field"])
                if not isinstance(new_value, str) or not new_value:
                    new_value = _fallback_name(cand["field"], idx, seq, used_names)
                if not _fits_field_schema(new_value, cand["schema"]):
                    new_value = _fallback_name(cand["field"], idx, seq, used_names)
                while new_value in used_names:
                    seq += 1
                    new_value = _fallback_name(cand["field"], idx, seq, used_names)

                if new_value != cand["value"]:
                    _set_at_dotted_path(enriched, path, new_value)
                    replaced_total += 1
                    logger.debug("Enrich %s: %r -> %r", path, cand["value"], new_value)
                used_names.add(new_value)

            if _fits_root_schema(enriched, schema):
                enriched_payloads.append(enriched)
            else:
                logger.warning(
                    "Enriched payload #%d failed schema validation; keeping original",
                    idx,
                )
                enriched_payloads.append(copy.deepcopy(payload))

        logger.info(
            "Enrich: updated %d field(s) across %d payload(s)",
            replaced_total,
            len(payloads),
        )
        return enriched_payloads

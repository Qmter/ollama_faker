"""Unit-тесты: подмена jsf до импорта main (совместимость pydantic/jsf)."""

from __future__ import annotations

import sys
import types


def _install_jsf_stub() -> None:
    if "jsf" in sys.modules:
        return

    jsf_mod = types.ModuleType("jsf")

    class JSF:
        def __init__(self, *args, **kwargs):
            self._schema = args[0] if args else {}

        def generate(self):
            schema = self._schema if isinstance(self._schema, dict) else {}
            field_type = schema.get("type")
            if field_type == "integer":
                if "minimum" in schema:
                    return schema["minimum"]
                if "maximum" in schema:
                    return schema["maximum"]
                return 1
            if field_type == "string":
                if schema.get("enum"):
                    return schema["enum"][0]
                return "test"
            if field_type == "boolean":
                return True
            if field_type == "array":
                return []
            if field_type == "object" or "properties" in schema:
                return {}
            return None

    jsf_mod.JSF = JSF
    sys.modules["jsf"] = jsf_mod


_install_jsf_stub()

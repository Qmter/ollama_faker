"""
Юнит-тесты имён файлов логов (модуль log_paths.py).

Проверяем три вещи:
  1) sanitization путей → безопасные токены в имени файла
  2) правильные префиксы gen / run / clear
  3) --log-file переопределяет авто-путь, а wrapper main.build_generation_log_path
     по-прежнему строит gen_*.log
"""

import unittest
from pathlib import Path

from log_paths import (
    build_log_path,
    resolve_cli_log_file,
    sanitize_log_scope_token,
)


class LogPathTests(unittest.TestCase):
    def test_sanitize_prefix(self):
        # -d /interfaces → в имени файла только "interfaces"
        self.assertEqual(sanitize_log_scope_token("/interfaces"), "interfaces")

    def test_sanitize_endpoint(self):
        # Слэши пути API становятся подчёркиваниями
        self.assertEqual(
            sanitize_log_scope_token("/dns/server/zone/master/add"),
            "dns_server_zone_master_add",
        )

    def test_gen_all_scope(self):
        # Без -e/-d: каталог logs/ и хвост _all
        path = build_log_path("gen")
        self.assertEqual(path.parent, Path("logs"))
        self.assertRegex(path.name, r"^gen_\d{8}_\d{6}_all\.log$")

    def test_gen_dir_scope(self):
        # -d /interfaces → ..._interfaces.log
        path = build_log_path("gen", dir_prefixes=["/interfaces"])
        self.assertRegex(path.name, r"^gen_\d{8}_\d{6}_interfaces\.log$")

    def test_gen_single_endpoint_scope(self):
        # -e /dns/client → ..._dns_client.log
        path = build_log_path("gen", endpoints=["/dns/client"])
        self.assertRegex(path.name, r"^gen_\d{8}_\d{6}_dns_client\.log$")

    def test_run_prefix(self):
        # run_tests.py использует префикс "run"
        path = build_log_path("run", dir_prefixes=["/interfaces"])
        self.assertRegex(path.name, r"^run_\d{8}_\d{6}_interfaces\.log$")

    def test_clear_prefix(self):
        # clear_for_tests.py использует префикс "clear"
        path = build_log_path("clear", endpoints=["/dns/client"])
        self.assertRegex(path.name, r"^clear_\d{8}_\d{6}_dns_client\.log$")

    def test_resolve_cli_explicit_override(self):
        # Явный --log-file важнее автогенерации имени
        path = resolve_cli_log_file(
            "custom.log",
            "run",
            dir_prefixes=["/interfaces"],
        )
        self.assertEqual(path, Path("custom.log"))

    def test_build_generation_log_path_compat(self):
        # Обёртка в main.py не должна менять схему имён (префикс всегда gen)
        from main import build_generation_log_path

        path = build_generation_log_path(dir_prefixes=["/interfaces"])
        self.assertRegex(path.name, r"^gen_\d{8}_\d{6}_interfaces\.log$")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Запуск всех unit-тестов: python run_unit_tests.py [-v]"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unit-тесты llm_faker")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "-k",
        "--pattern",
        default="test_*.py",
        help="Glob паттерн файлов в unit_tests/ (по умолчанию: test_*.py)",
    )
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=str(project_root / "unit_tests"),
        pattern=args.pattern,
        top_level_dir=str(project_root),
    )
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

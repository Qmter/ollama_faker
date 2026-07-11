"""Пути к JSON-сценариям по API endpoint."""

from __future__ import annotations

from pathlib import Path


def endpoint_to_test_file(endpoint: str, tests_dir: Path, method: str = "post") -> Path:
    """
    Сопоставляет путь API с файлом сценария в tests/.

    Имя файла как раньше (все сегменты через _), первый сегмент — подкаталог:
      /interfaces/bonding/mode   → tests/interfaces/interfaces_bonding_mode_post.json
      /acl/filter/filter_ipv4    → tests/acl/acl_filter_filter_ipv4_post.json
      /fail2ban/jail/add         → tests/fail2ban/fail2ban_jail_add_post.json
    """
    parts = [part for part in endpoint.strip("/").split("/") if part]
    if not parts:
        raise ValueError(f"Invalid endpoint path: {endpoint!r}")

    suffix = f"_{method}.json"
    filename = f"{'_'.join(parts)}{suffix}"
    if len(parts) == 1:
        return tests_dir / filename

    return tests_dir / parts[0] / filename

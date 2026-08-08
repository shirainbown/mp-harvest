"""应用本地设置（data/settings.json，gitignored，不含密钥）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mp_harvest.infra.platform.paths import data_dir


def settings_path(root_dir: str | Path | None = None) -> Path:
    """默认解析到 data_dir()；显式传 root_dir 时兼容旧布局 root/data/。"""
    base = (Path(root_dir) / "data") if root_dir else data_dir()
    return base / "settings.json"


def load_settings(root_dir: str | Path | None = None) -> dict[str, Any]:
    p = settings_path(root_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(root_dir: str | Path | None, payload: dict[str, Any]) -> None:
    p = settings_path(root_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)

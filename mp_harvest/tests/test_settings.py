"""应用本地设置读写测试。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.settings import load_settings, save_settings, settings_path  # noqa: E402


def test_settings_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert settings_path(root).name == "settings.json"
        assert load_settings(root) == {}
        save_settings(root, {"proxy": "http://127.0.0.1:7897"})
        assert load_settings(root)["proxy"] == "http://127.0.0.1:7897"


def test_load_settings_invalid_file():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = settings_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("不是 JSON", encoding="utf-8")
        assert load_settings(root) == {}

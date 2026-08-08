"""AccountStore：默认名称、rename 持久化（2026-08-09）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from mp_harvest.core.store import DEFAULT_ACCOUNT_NAME, AccountStore


def test_add_pending_default_name_and_rename():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "accounts.json"
        store = AccountStore(path)
        row = store.add_pending(name="", article_url="https://mp.weixin.qq.com/s/1")
        assert row["name"] == DEFAULT_ACCOUNT_NAME

        renamed = store.rename(row["id"], "半导体行业观察")
        assert renamed is not None
        assert renamed["name"] == "半导体行业观察"

        # 持久化：重新加载后名称仍在
        reloaded = AccountStore(path)
        assert reloaded.get(row["id"])["name"] == "半导体行业观察"

        # 相同名称幂等，不重复保存也不报错
        same = store.rename(row["id"], "半导体行业观察")
        assert same is not None and same["name"] == "半导体行业观察"
        # 空名称/不存在 id 返回 None
        assert store.rename(row["id"], "  ") is None
        assert store.rename("nope", "任意") is None

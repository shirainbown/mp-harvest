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


# ── fresh_credentials：只放行未过期凭证（2026-09-13）─────────────────
#
# 过期的 pass_ticket/wxuin 会让微信风控把请求当成「带作废票据的请求」，
# 比不带更容易触发环境校验页 —— 抓正文只在凭证新鲜时才带。


def _store_with_cred(tmp_path: Path, *, expired: bool):
    from datetime import timedelta

    from mp_harvest.core import store as store_mod

    store = AccountStore(tmp_path / "accounts.json")
    row = store.add_pending(name="号A", article_url="https://mp.weixin.qq.com/s/1")
    store.apply_credentials(row["id"], {"__biz": "b", "uin": "u", "key": "k",
                                        "pass_ticket": "p"})
    if expired:
        # 把过期时间直接改到过去（等价于等 30 分钟）
        past = store_mod._iso(store_mod._now() - timedelta(minutes=31))
        for r in store._accounts:
            r["expires_at"] = past
        store.save()
    return store, row["id"]


def test_fresh_credentials_returns_creds_when_fresh(tmp_path):
    store, aid = _store_with_cred(tmp_path, expired=False)
    cred = store.fresh_credentials(aid)
    assert cred.get("pass_ticket") == "p"


def test_fresh_credentials_empty_when_expired(tmp_path):
    store, aid = _store_with_cred(tmp_path, expired=True)
    assert store.fresh_credentials(aid) == {}


def test_fresh_credentials_empty_for_unknown_account(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    assert store.fresh_credentials("nope") == {}

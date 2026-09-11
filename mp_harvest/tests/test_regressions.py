"""2026-09 一轮修复的回归测试。

每个用例对应一个已确认的缺陷（括号里是当时的症状），失败即回归：

导出
  · test_index_survives_records_failure      记录库异常时目录页被空页覆盖
  · test_index_accumulates_across_accounts   分批导出不同公众号时目录页丢前面的条目
  · test_skip_by_article_not_filename        标题/链接漂移导致同一篇重复导出
  · test_cred_error_does_not_override_skip   凭证过期时已导出的文章被记成失败
  · test_like_wildcards_escaped              out_dir 含 "_" 时捞到兄弟目录的记录
  · test_image_assets_do_not_collide         开启图片本地化后所有文章串图

AI
  · test_failure_not_persisted_to_cache      一次调用失败把文章永久拉黑
  · test_legacy_cache_is_migrated            旧格式缓存不迁移 → 两阶段流程卡死
  · test_coercion_tolerates_bad_model_output 畸形字段让整批判定崩掉 / "false" 判成 True

抓取与缓存
  · test_failed_fetch_does_not_advance_last_fetch 失败拉取把「最近拉取」筛成空
  · test_verdict_merge_keeps_fetch_fields    判定合并不回滚抓取到的字段
  · test_append_article_is_atomic            补录与后台拉取并发时丢文章
  · test_sightings_sees_external_writes      addon 写的目击看不到 / 被补录抹掉

接口契约
  · test_model_put_without_name              「+ 添加模型」整批 422，所有模型存不下来
  · test_article_id_includes_biz             聚合视图 id 撞车（Vue key / 勾选串号）
  · test_add_account_success_has_no_mitm_message 成功添加却弹红色错误
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.article_reader import batch_export_articles  # noqa: E402
from mp_harvest.core.export_records import ExportRecords  # noqa: E402


# ── 公共夹具 ──────────────────────────────────────────────────────────


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """隔离数据目录。

    注意 paths.data_dir 之外，settings/sightings/ai_filter 是**直接 import 了名字**
    的，只 patch paths 模块会漏掉它们（真实数据目录因此被测试污染过）。
    """
    import mp_harvest.core.ai_filter as ai_mod
    import mp_harvest.core.settings as settings_mod
    import mp_harvest.core.sightings as sightings_mod
    import mp_harvest.infra.platform.paths as paths_mod

    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    for mod in (paths_mod, settings_mod, sightings_mod, ai_mod):
        monkeypatch.setattr(mod, "data_dir", lambda *a, **k: d, raising=False)

    from mp_harvest.server import state

    state.reset()
    yield d
    state.reset()


def _art(i: int, *, aid: str = "acc1", name: str = "测试号", title: str | None = None,
         link: str | None = None, body_html: str = "<p>x</p>") -> dict:
    return {
        "identity": f"a{i}",
        "link": link or f"https://mp.weixin.qq.com/s/A{i}",
        "title": title or f"文章{i}",
        "publish_at": "2026-08-05 10:00",
        "publish_ts": 1754400000,
        "_account_id": aid,
        "account": name,
        "body_html": body_html,
    }


def _fetch(url: str, cred=None):
    return {
        "title": "t",
        "link": url,
        "body_text": "x",
        "body_html": "<p>x</p>",
        "publish_at": "2026-08-05 10:00",
        "publish_ts": 1754400000,
    }


# ── 导出 ──────────────────────────────────────────────────────────────


def test_index_survives_records_failure(tmp_path):
    """记录库抛异常时，已累积的 index.html 不能被写成空白页。"""

    class Broken(ExportRecords):
        def list_exports(self, *a, **k):  # type: ignore[override]
            raise RuntimeError("simulated sqlite failure")

    out = tmp_path / "out"
    out.mkdir()
    index = out / "index.html"
    index.write_text("<html>OLD CATALOG</html>", encoding="utf-8")

    batch_export_articles([_art(1)], out_dir=out, fetch_article=_fetch, records=Broken(out / "db"))

    assert "OLD CATALOG" in index.read_text(encoding="utf-8")


def test_index_falls_back_to_batch_rows(tmp_path):
    """记录库不可用且还没有目录页时，用本批次行兜底（而不是空页）。"""
    out = tmp_path / "out"
    out.mkdir()

    class Empty(ExportRecords):
        def list_exports(self, *a, **k):  # type: ignore[override]
            return []

    batch_export_articles([_art(1)], out_dir=out, fetch_article=_fetch, records=Empty(out / "db"))
    assert "文章1" in (out / "index.html").read_text(encoding="utf-8")


def test_index_accumulates_across_accounts(tmp_path):
    """分批导出不同公众号到同一目录，目录页必须累积（不能只剩最后一批）。"""
    out = tmp_path / "out"
    db = ExportRecords(out / "db")
    batch_export_articles([_art(1, aid="accA", name="公众号A")], out_dir=out,
                          fetch_article=_fetch, records=db, account_name="公众号A")
    batch_export_articles([_art(2, aid="accB", name="公众号B")], out_dir=out,
                          fetch_article=_fetch, records=db, account_name="公众号B")

    html = (out / "index.html").read_text(encoding="utf-8")
    assert "文章1" in html and "文章2" in html


def test_skip_by_article_not_filename(tmp_path):
    """同一篇（identity 相同）即使标题变了，也只应落地一个文件。"""
    out = tmp_path / "out"
    db = ExportRecords(out / "db")
    batch_export_articles([_art(1, title="标题A")], out_dir=out, fetch_article=_fetch, records=db)
    res = batch_export_articles([_art(1, title="标题B")], out_dir=out,
                                fetch_article=_fetch, records=db)

    files = [p.relative_to(out).as_posix() for p in out.rglob("*.html") if p.name != "index.html"]
    assert len(files) == 1, files
    assert res["skipped"] == 1 and res["exported"] == 0


def test_cred_error_does_not_override_skip(tmp_path):
    """已导出过的文章不需要凭证，凭证过期不应把它们记成失败。"""
    out = tmp_path / "out"
    db = ExportRecords(out / "db")
    batch_export_articles([_art(1)], out_dir=out, fetch_article=_fetch, records=db)
    res = batch_export_articles(
        [dict(_art(1), _cred_error="凭证缺失或已过期")],
        out_dir=out, fetch_article=_fetch, records=db,
    )
    assert res["skipped"] == 1 and res["failed"] == 0, res


def test_like_wildcards_escaped(tmp_path):
    """out_dir 含 "_" 时，LIKE 不应把兄弟目录的记录也算进来。"""
    base = tmp_path / "base"
    db = ExportRecords(base / "db")
    db.record_export(article_id="a1", out_path=str(base / "a_c" / "f1.html"), title="dir=a_c")
    db.record_export(article_id="a2", out_path=str(base / "abc" / "f2.html"), title="dir=abc")

    got = [r["title"] for r in db.list_exports(out_dir=base / "a_c")]
    assert got == ["dir=a_c"], got


def test_image_assets_do_not_collide(tmp_path, monkeypatch):
    """开启图片本地化后，各篇图片必须落在各自的命名空间里（否则串图）。"""
    from mp_harvest.core import article_reader as ar

    class _Resp:
        def __init__(self, body: bytes) -> None:
            self.content = body
            self.headers = {"Content-Type": "image/jpeg"}

        def raise_for_status(self) -> None:
            pass

    class _Session:
        def __init__(self, *a, **k) -> None:
            self.trust_env = True

        def get(self, url, **k):
            return _Resp(("BYTES:" + url).encode())

    monkeypatch.setattr(ar.requests, "Session", _Session)

    out = tmp_path / "out"
    db = ExportRecords(out / "db")
    arts = [
        _art(1, body_html='<img src="https://mmbiz.qpic.cn/IMG-1.jpg">'),
        _art(2, body_html='<img src="https://mmbiz.qpic.cn/IMG-2.jpg">'),
    ]
    by_link = {a["link"]: a for a in arts}

    def fetch_with_body(url: str, cred=None):
        # 正文必须回带 img 标签，否则 localize_images 根本没图可下
        return dict(_fetch(url), body_html=by_link[url]["body_html"])

    batch_export_articles(arts, out_dir=out, fetch_article=fetch_with_body,
                          download_images=True, records=db)

    assets = sorted(p.name for p in (out / "assets").iterdir())
    assert len(assets) == 2, assets
    bodies = {(out / "assets" / n).read_bytes() for n in assets}
    assert len(bodies) == 2, "两篇文章的图片被写成了同一张"


# ── AI ────────────────────────────────────────────────────────────────


def _cfg():
    from mp_harvest.core.ai_filter import ModelConfig

    return [ModelConfig(id="m1", name="t", base_url="http://127.0.0.1:1",
                        api_key="k", model="x", enabled=True, format="openai")]


def _arts(n: int = 3) -> list[dict]:
    return [{"identity": f"id{i}", "link": f"https://mp.weixin.qq.com/s/A{i}",
             "title": f"文章{i}", "publish_ts": 1754400000} for i in range(1, n + 1)]


def test_failure_not_persisted_to_cache(tmp_path, monkeypatch):
    """模型调用失败只影响本轮展示，不能写进持久缓存（否则文章被永久拉黑）。"""
    from mp_harvest.core import ai_filter as af

    cp = tmp_path / "cache.json"

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(af, "_call_model", boom)
    af.judge_articles(_arts(), _cfg(), prompt="P", cache_path=cp, batch_size=10, workers=1)

    assert json.loads(cp.read_text(encoding="utf-8")).get("entries") == {}


def test_legacy_cache_is_migrated(tmp_path, monkeypatch):
    """旧格式（无前缀 keep）缓存必须迁移成 title_keep，否则标题筛选恒「通过 0 篇」。"""
    from mp_harvest.core import ai_filter as af

    cp = tmp_path / "cache.json"
    rows = _arts()
    cp.write_text(
        json.dumps({af.article_key(a): {"keep": True, "reason": "旧判定", "at": "x",
                                        "model": "old"} for a in rows}, ensure_ascii=False),
        encoding="utf-8",
    )

    def healthy(cfg, prompt, user, **k):
        items = json.loads(user)
        return json.dumps({"items": [{"idx": it["idx"], "keep": True, "category": "t",
                                      "relevance_score": 4, "technical_depth": 4,
                                      "confidence": "high", "reason": "r"} for it in items]},
                          ensure_ascii=False)

    monkeypatch.setattr(af, "_call_model", healthy)
    af.judge_articles(rows, _cfg(), prompt="P", cache_path=cp, prefix="title_",
                      batch_size=10, workers=1)

    data = json.loads(cp.read_text(encoding="utf-8"))
    assert data["__version__"] == af._CACHE_VERSION
    first = data["entries"][af.article_key(rows[0])]
    assert first.get("title_keep") is True, first
    # 迁移前先备份原文件
    assert list(tmp_path.glob("cache.json.bak-*"))


def test_coercion_tolerates_bad_model_output(tmp_path, monkeypatch):
    """畸形字段不能让整批崩掉；字符串 "false" 必须解析成 False。"""
    from mp_harvest.core import ai_filter as af

    cp = tmp_path / "cache.json"

    def weird(cfg, prompt, user, **k):
        items = json.loads(user)
        return json.dumps({"items": [
            {"idx": it["idx"], "keep": "false" if i == 0 else "true",
             "relevance_score": "high", "technical_depth": None,
             "confidence": "low", "reason": "x"}
            for i, it in enumerate(items)
        ]}, ensure_ascii=False)

    monkeypatch.setattr(af, "_call_model", weird)
    res = af.judge_articles(_arts(), _cfg(), prompt="P", cache_path=cp, prefix="title_",
                            batch_size=10, workers=1)

    assert res["errors"] == []
    assert len(res["dropped"]) == 1, res
    entries = json.loads(cp.read_text(encoding="utf-8"))["entries"]
    assert entries[af.article_key(_arts()[0])]["title_keep"] is False


def test_unjudged_articles_not_cached(tmp_path, monkeypatch):
    """模型漏答的篇目不入缓存，且要作为错误上报（否则一次漏答永久拉黑）。"""
    from mp_harvest.core import ai_filter as af

    cp = tmp_path / "cache.json"

    def partial(cfg, prompt, user, **k):
        items = json.loads(user)
        return json.dumps({"items": [{"idx": items[0]["idx"], "keep": True, "category": "t",
                                      "relevance_score": 3, "technical_depth": 3,
                                      "confidence": "high", "reason": "ok"}]}, ensure_ascii=False)

    monkeypatch.setattr(af, "_call_model", partial)
    res = af.judge_articles(_arts(), _cfg(), prompt="P", cache_path=cp, prefix="title_",
                            batch_size=10, workers=1)

    assert res["ok"] is False and res["errors"]
    assert len(json.loads(cp.read_text(encoding="utf-8"))["entries"]) == 1


# ── 抓取与缓存 ────────────────────────────────────────────────────────


def test_failed_fetch_does_not_advance_last_fetch(data_dir):
    """失败的拉取（空结果）不能推进 last_fetch_ts，否则「最近拉取」被筛空。"""
    from mp_harvest.server import state

    arts = [{"identity": f"i{i}", "link": f"https://x/{i}", "title": f"文章{i}",
             "publish_ts": 1754400000 + i} for i in (1, 2)]
    t1 = int(time.time()) - 100
    state.merge_articles("acc1", arts, fetched_ts=t1)
    assert len(state.time_filter(state.get_articles("acc1"),
                                 latest_ts=state.get_last_fetch_ts("acc1"))) == 2

    state.merge_articles("acc1", [], fetched_ts=int(time.time()), advance_last_fetch=False)
    rows = state.get_articles("acc1")
    assert len(rows) == 2
    assert len(state.time_filter(rows, latest_ts=state.get_last_fetch_ts("acc1"))) == 2


def test_verdict_merge_keeps_fetch_fields(data_dir):
    """判定合并只写判定字段，不能把抓取到的 title/link 用旧快照回滚。"""
    from mp_harvest.server import state

    state.merge_articles("acc1", [
        {"identity": "i1", "link": "https://new/1", "title": "新标题",
         "publish_ts": 1754400000, "publish_at": "2026-09-01 10:00"}
    ], fetched_ts=1)
    state.merge_article_verdicts("acc1", [{
        "identity": "i1", "link": "https://old/1", "title": "旧标题",
        "publish_at": "2000-01-01 00:00", "keep": True, "reason": "通过",
    }])

    row = state.get_articles("acc1")[0]
    assert row["title"] == "新标题" and row["link"] == "https://new/1"
    assert row["keep"] is True and row["reason"] == "通过"


def test_append_article_is_atomic(data_dir):
    """补录原子追加：并发拉取写入的文章不能被整体写回覆盖掉。"""
    from mp_harvest.server import state

    state.merge_articles("acc1", [
        {"identity": "e1", "link": "https://x/1", "title": "已存在", "publish_ts": 1754400001}
    ], fetched_ts=1)
    # 模拟补录期间后台拉取又落了一篇
    state.append_article("acc1", {"identity": "e2", "link": "https://x/2", "title": "并发拉到",
                                  "publish_ts": 1754400002, "fetched_ts": 2})
    state.append_article("acc1", {"identity": "e3", "link": "https://x/3", "title": "补录",
                                  "publish_ts": 1754400003, "fetched_ts": 2})

    ids = {r["identity"] for r in state.get_articles("acc1")}
    assert {"e1", "e2", "e3"} <= ids, ids


def test_sightings_sees_external_writes(data_dir, monkeypatch):
    """addon（独立进程）写入的目击必须可见，且保存时不能被抹掉。"""
    import os

    from mp_harvest.core.sightings import SightingsStore

    path = data_dir / "article_sightings.json"
    store = SightingsStore(path)
    store.upsert({"link": "https://mp.weixin.qq.com/s/MINE", "title": "我补录的",
                  "source": "manual"})

    # 模拟 mitmproxy addon 直接改文件
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sightings"].insert(0, {"link": "https://mp.weixin.qq.com/s/ADDON",
                                    "title": "addon抓到的", "identity": "s:ADDON",
                                    "source": "sighting", "seen_at": "2026-09-12T01:00:00"})
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))

    assert "addon抓到的" in [r["title"] for r in store.list_for_biz("")]

    store.upsert({"link": "https://mp.weixin.qq.com/s/MINE2", "title": "再补一条",
                  "source": "manual"})
    on_disk = [r["title"] for r in json.loads(path.read_text(encoding="utf-8"))["sightings"]]
    assert "addon抓到的" in on_disk, on_disk


# ── 接口契约 ──────────────────────────────────────────────────────────


def test_model_put_without_name(data_dir, monkeypatch):
    """「+ 添加模型」推入的卡片不带 name；PUT 必须接受（否则整批 422，什么都没保存）。"""
    from fastapi.testclient import TestClient

    from mp_harvest.server import get_token
    from mp_harvest.server.app import create_app

    with TestClient(create_app()) as client:
        token = get_token()
        payload = [{"id": "m1", "enabled": True, "base_url": "https://api.example.com",
                    "api_key": "sk-x", "format": "openai", "model": "m"}]
        r = client.put(f"/api/ai/models?token={token}", json=payload)
        assert r.status_code == 200, r.text
        assert client.get(f"/api/ai/models?token={token}").json()["models"][0]["id"] == "m1"


def test_article_id_includes_biz():
    """对外 id 必须带 __biz 前缀，否则聚合视图里跨账号同文 id 撞车。"""
    from mp_harvest.server.mappers import article_out, article_public_id

    row = {"identity": "mid:1|idx:1|sn:x", "link": "https://mp.weixin.qq.com/s/A",
           "title": "T", "publish_ts": 1754400000, "__biz": "MzA"}
    assert article_public_id(row) == "MzA:mid:1|idx:1|sn:x"
    assert article_out(row, account_id="acc1")["id"] == "MzA:mid:1|idx:1|sn:x"
    # 没有 __biz 时退回 identity（兼容旧缓存）
    assert article_public_id({"identity": "i1"}) == "i1"


def test_add_account_success_has_no_mitm_message(data_dir, monkeypatch):
    """代理启动成功时不能回传 mitm_message —— 前端把它当失败弹红色错误。"""
    from fastapi.testclient import TestClient

    from mp_harvest.server import get_token, state
    from mp_harvest.server.app import create_app

    class _Svc:
        running = False
        port = 8088

        def start(self, **k):
            self.running = True
            return True, "已开启系统代理（停止抓包后自动恢复原设置）。"

    monkeypatch.setattr(state, "get_mitm", lambda: _Svc())
    with TestClient(create_app()) as client:
        token = get_token()
        r = client.post(f"/api/accounts?token={token}",
                        json={"name": "号", "url": "https://mp.weixin.qq.com/s/AAA"})
        assert r.status_code == 201, r.text
        assert "mitm_message" not in r.json(), r.json()


def test_normalize_empty_export_dir_is_blank():
    """清空「默认目录」不能把进程 CWD 存成导出目录。"""
    import os

    from mp_harvest.server.routes.settings import _normalize

    assert _normalize("") == ""
    assert _normalize("   ") == ""
    assert os.path.isabs(_normalize("~/Downloads/x"))


# ── 检查更新（2026-09：GitHub API 限流）──────────────────────────────


def test_update_check_falls_back_to_atom_on_rate_limit(monkeypatch):
    """API 返回 403 限流时回退到 releases.atom，而不是报「无法访问 GitHub」。

    实测：未认证的 GitHub API 每小时仅 60 次（走共享代理出口更易耗尽），
    旧代码把它当成网络故障 → 界面提示「无法访问 GitHub（请检查网络 / 代理设置）」，
    用户读作「无法连接 github」。
    """
    import urllib.error

    from mp_harvest.infra.platform import base as pb
    from mp_harvest.infra.platform.mac import MacUpdater

    atom = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:Repository/123456/v9.9.9</id>
    <title>v9.9.9</title>
    <content type="html">&lt;h2&gt;修复若干问题&lt;/h2&gt;</content>
  </entry>
</feed>"""

    class _Resp:
        status = 200

        def __init__(self, body: str) -> None:
            self._body = body.encode()

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a) -> None:
            return None

    class _Opener:
        def open(self, req, timeout=None):  # noqa: ANN001
            url = getattr(req, "full_url", str(req))
            if "api.github.com" in url:
                raise urllib.error.HTTPError(url, 403, "rate limit exceeded", {}, None)
            return _Resp(atom)

    monkeypatch.setattr(pb.GithubUpdater, "_opener", lambda self, proxy: _Opener())

    result = MacUpdater().check(proxy="http://127.0.0.1:7897")

    assert result.ok is True, result
    assert result.version == "v9.9.9", result
    # 下载地址按 CI 命名约定拼（MP-Harvest-mac-<ver>.zip）
    assert result.zip_url.endswith("/v9.9.9/MP-Harvest-mac-9.9.9.zip"), result.zip_url
    assert "修复若干问题" in result.notes, result.notes


def test_update_check_reports_rate_limit_when_atom_also_fails(monkeypatch):
    """连 atom 兜底也失败时，提示语要区分「限流」与「网络不通」。"""
    import urllib.error

    from mp_harvest.infra.platform import base as pb
    from mp_harvest.infra.platform.mac import MacUpdater

    class _Opener:
        def open(self, req, timeout=None):  # noqa: ANN001
            raise urllib.error.HTTPError(
                getattr(req, "full_url", "x"), 403, "rate limit exceeded", {}, None
            )

    monkeypatch.setattr(pb.GithubUpdater, "_opener", lambda self, proxy: _Opener())

    result = MacUpdater().check(proxy=None)
    assert result.ok is False
    assert "限流" in result.message, result.message


def test_system_proxy_uses_scutil_when_getproxies_empty(monkeypatch):
    """_scproxy 缺失（冻结版常见）时，仍能通过 scutil --proxy 拿到系统代理。"""
    import urllib.request

    from mp_harvest.infra.platform import base as pb

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    scutil_out = """<dictionary> {
  HTTPEnable : 0
  HTTPSEnable : 1
  HTTPSPort : 7897
  HTTPSProxy : 127.0.0.1
}"""
    monkeypatch.setattr(pb.sys, "platform", "darwin")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: type("P", (), {"stdout": scutil_out, "returncode": 0})(),
    )
    assert pb._system_proxy() == "http://127.0.0.1:7897"


# ── 导出按日期归档（2026-09 需求）────────────────────────────────────


def _dated_art(i: int, publish_at: str, title: str) -> dict:
    return {
        "identity": f"d{i}",
        "link": f"https://mp.weixin.qq.com/s/D{i}",
        "title": title,
        "publish_at": publish_at,
        "publish_ts": 0,
        "_account_id": "acc1",
        "account": "测试号",
        "body_html": f'<img src="https://mmbiz.qpic.cn/IMG-{i}.jpg">',
    }


def test_export_groups_files_by_month(tmp_path):
    """按发布日期归档到 YYYY-MM/；日期缺失的落根目录（不硬塞进错误月份）。"""
    out = tmp_path / "out"
    db = ExportRecords(out / "db")
    arts = [
        _dated_art(1, "2026-08-05 10:00", "八月文章"),
        _dated_art(2, "2026-09-01 09:00", "九月文章"),
        _dated_art(3, "", "无日期文章"),
    ]
    by_link = {a["link"]: a for a in arts}

    def fetch(url: str, cred=None):
        a = by_link[url]
        return {
            "title": a["title"],
            "link": url,
            "body_html": a["body_html"],
            "body_text": "x",
            "publish_at": a["publish_at"],
            "publish_ts": 0,
        }

    res = batch_export_articles(arts, out_dir=out, fetch_article=fetch, records=db)
    assert res["exported"] == 3, res

    bodies = sorted(
        p.relative_to(out).as_posix() for p in out.rglob("*.html") if p.name != "index.html"
    )
    assert any(b.startswith("2026-08/") for b in bodies), bodies
    assert any(b.startswith("2026-09/") for b in bodies), bodies
    assert any("/" not in b for b in bodies), bodies  # 无日期 → 根目录


def test_export_index_links_are_relative(tmp_path):
    """目录页的 file 必须是相对 out_dir 的路径，且指向真实文件（归档到子目录后仍可点）。"""
    import re

    out = tmp_path / "out"
    db = ExportRecords(out / "db")
    arts = [_dated_art(1, "2026-08-05 10:00", "八月文章")]
    by_link = {a["link"]: a for a in arts}

    def fetch(url: str, cred=None):
        a = by_link[url]
        return {"title": a["title"], "link": url, "body_html": a["body_html"],
                "body_text": "x", "publish_at": a["publish_at"], "publish_ts": 0}

    batch_export_articles(arts, out_dir=out, fetch_article=fetch, records=db)

    index = (out / "index.html").read_text(encoding="utf-8")
    files = set(re.findall(r'href="([^"]+\.html)"', index))
    assert files, index
    for rel in files:
        assert (out / rel).is_file(), rel
        assert rel.startswith("2026-08/"), rel


def test_subdir_article_images_use_parent_relative_path(tmp_path, monkeypatch):
    """归档到子目录后，图片引用必须是 ../assets/… 否则图片全断。"""
    import re

    from mp_harvest.core import article_reader as ar

    class _Resp:
        def __init__(self, body: bytes) -> None:
            self.content = body
            self.headers = {"Content-Type": "image/jpeg"}

        def raise_for_status(self) -> None:
            pass

    class _Sess:
        def __init__(self, *a, **k) -> None:
            self.trust_env = True

        def get(self, url, **k):
            return _Resp(("B:" + url).encode())

    monkeypatch.setattr(ar.requests, "Session", _Sess)

    out = tmp_path / "out"
    db = ExportRecords(out / "db")
    arts = [
        _dated_art(1, "2026-08-05 10:00", "八月文章"),
        _dated_art(2, "", "无日期文章"),
    ]
    by_link = {a["link"]: a for a in arts}

    def fetch(url: str, cred=None):
        a = by_link[url]
        return {"title": a["title"], "link": url, "body_html": a["body_html"],
                "body_text": "x", "publish_at": a["publish_at"], "publish_ts": 0}

    batch_export_articles(arts, out_dir=out, fetch_article=fetch,
                          download_images=True, records=db)

    for html in (p for p in out.rglob("*.html") if p.name != "index.html"):
        refs = re.findall(r'src="([^"]*assets/[^"]+)"', html.read_text(encoding="utf-8"))
        assert refs, html
        for ref in refs:
            assert (html.parent / ref).resolve().is_file(), f"{html.name} -> {ref}"
        expected = "../assets/" if html.parent != out else "assets/"
        assert refs[0].startswith(expected), (html.name, refs[0])


def test_update_proxy_respects_three_modes(data_dir, monkeypatch):
    """更新代理三态：直连不带出残留值 / 跟随系统代理 / 自定义。

    另外「未配置时按跟随系统代理处理」是新装默认 —— 否则国内用户开着 Clash
    也永远「检查更新失败」。
    """
    import json

    import mp_harvest.infra.platform.base as pb
    from mp_harvest.server.routes.update import _settings_proxy

    monkeypatch.setattr(pb, "_system_proxy", lambda: "http://127.0.0.1:7899")
    path = data_dir / "settings.json"

    if path.exists():
        path.unlink()
    assert _settings_proxy() == "http://127.0.0.1:7899", "未配置时应跟随系统代理"

    path.write_text(json.dumps({"mode": "system"}), encoding="utf-8")
    assert _settings_proxy() == "http://127.0.0.1:7899"

    # 直连：即使残留着自定义地址也不能带出来走代理
    path.write_text(
        json.dumps({"mode": "direct", "proxy": "http://127.0.0.1:7897"}), encoding="utf-8"
    )
    assert _settings_proxy() == ""

    path.write_text(
        json.dumps({"mode": "custom", "proxy": "http://127.0.0.1:7897"}), encoding="utf-8"
    )
    assert _settings_proxy() == "http://127.0.0.1:7897"

"""server 契约测试 fixtures。

core / infra.mitm 由另一 agent 并行平移，可能尚未就绪——这里用
``monkeypatch.setitem(sys.modules, ...)`` 注入语义等价的 fake 模块，
只验证 server 层契约（路由/参数/任务/广播），不依赖 core 真实实现。
"""

from __future__ import annotations

import json
import sys
import time
import types
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


# ── fake core 模块 ────────────────────────────────────────────────


def _fake_store() -> types.ModuleType:
    mod = types.ModuleType("mp_harvest.core.store")
    mod.DEFAULT_ACCOUNT_NAME = "未命名公众号"

    class AccountStore:
        def __init__(self, path: Path) -> None:
            self.path = Path(path)
            self._rows: list[dict[str, Any]] = []

        def list_accounts(self):
            return [dict(r) for r in self._rows]

        def get(self, account_id: str):
            for r in self._rows:
                if r["id"] == account_id:
                    return dict(r)
            return None

        def add_pending(self, *, name: str, article_url: str):
            row = {
                "id": uuid.uuid4().hex[:8],
                "name": (name or "").strip() or "未命名公众号",
                "article_url": article_url,
                "credentials": {},
                "expires_at": None,
                "status": "awaiting",
            }
            self._rows.insert(0, row)
            return dict(row)

        def delete(self, account_id: str) -> None:
            self._rows = [r for r in self._rows if r["id"] != account_id]

        def rename(self, account_id: str, name: str):
            for r in self._rows:
                if r["id"] == account_id:
                    r["name"] = (name or "").strip() or r["name"]
                    return dict(r)
            return None

        def set_awaiting(self, account_id: str) -> None:
            for r in self._rows:
                if r["id"] == account_id:
                    r["status"] = "awaiting"
                    break

    mod.AccountStore = AccountStore
    return mod


def _fake_credentials() -> types.ModuleType:
    mod = types.ModuleType("mp_harvest.core.credentials")
    mod.credentials_to_json = lambda cred: json.dumps(cred, ensure_ascii=False, indent=2)
    return mod


def _fake_batch_import() -> types.ModuleType:
    mod = types.ModuleType("mp_harvest.core.batch_import")

    def parse_batch_lines(text: str) -> list[dict]:
        out = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            url = next((p for p in parts if p.startswith("http")), "")
            name = " ".join(p for p in parts if p != url).strip()
            entry = {"name": name, "url": url, "duplicate": False}
            if not url:
                entry["error"] = "无链接"
            out.append(entry)
        return out

    def dedupe_by_name(entries: list[dict]) -> list[dict]:
        seen: set[str] = set()
        for e in entries:
            key = (e.get("name") or "").strip().lower()
            if key and key in seen:
                e["duplicate"] = True
            elif key:
                seen.add(key)
        return entries

    def split_fresh_duplicates(entries, existing_urls, existing_names):
        known_urls = {str(u).strip() for u in existing_urls}
        known_names = {str(n).strip().lower() for n in existing_names}
        fresh, dup_urls, dup_names = [], [], []
        for e in entries:
            url = (e.get("url") or "").strip()
            if not url:
                continue
            if e.get("duplicate") or url in known_urls:
                dup_urls.append(e)
            elif (e.get("name") or "").strip().lower() in known_names:
                dup_names.append(e)
            else:
                fresh.append(e)
        return fresh, dup_urls, dup_names

    mod.parse_batch_lines = parse_batch_lines
    mod.dedupe_by_name = dedupe_by_name
    mod.split_fresh_duplicates = split_fresh_duplicates
    return mod


def _fake_sightings() -> types.ModuleType:
    mod = types.ModuleType("mp_harvest.core.sightings")

    class SightingsStore:
        def __init__(self, path: Path) -> None:
            self.rows: list[dict[str, Any]] = []

        def list_for_biz(self, biz: str, *, cutoff_ts: int = 0):
            return [r for r in self.rows if r.get("__biz") == biz]

        def upsert(self, sighting: dict):
            link = str(sighting.get("link") or "").strip()
            title = str(sighting.get("title") or "").strip()
            if not link and not title:
                return None
            row = {
                "title": title or "(无标题)",
                "link": link,
                "__biz": "fakebiz",
                "identity": f"id-{len(self.rows)}",
                "publish_ts": 1700000000,
                "source": sighting.get("source", "manual"),
            }
            self.rows.append(row)
            return dict(row)

    mod.SightingsStore = SightingsStore
    return mod


def _fake_history_client() -> types.ModuleType:
    mod = types.ModuleType("mp_harvest.core.history_client")
    mod.pages_before_return = 2  # cancel 测试可改大

    def fetch_history_days(cred, *, days=7, on_progress=None, sightings=None, **kw):
        articles = []
        for i in range(mod.pages_before_return):
            if on_progress:
                on_progress(f"正在拉取第 {i + 1} 页")
            articles.append(
                {
                    "title": f"文章{i}",
                    "link": f"https://mp.weixin.qq.com/s/x{i}",
                    "publish_ts": 1700000000 + i,
                    "identity": f"art-{i}",
                }
            )
        return {
            "ok": True,
            "articles": articles,
            "pages": mod.pages_before_return,
            "warning": "",
            "nickname": "真实公众号",
        }

    mod.fetch_history_days = fetch_history_days
    return mod


def _fake_history_export() -> types.ModuleType:
    mod = types.ModuleType("mp_harvest.core.history_export")
    mod.render_export = (
        lambda articles, *, fmt, account_name="", days=7: f"FMT={fmt};N={len(articles)};A={account_name};D={days}"
    )
    mod.default_export_filename = (
        lambda *, account_name, days, ext: f"{account_name}_{days}d.{ext}"
    )
    return mod


def _fake_article_reader() -> types.ModuleType:
    mod = types.ModuleType("mp_harvest.core.article_reader")

    def batch_export_articles(articles, *, out_dir, cred=None, account_name="", on_progress=None):
        written = []
        for i, a in enumerate(articles, 1):
            if on_progress:
                on_progress(f"正在导出 {i}/{len(articles)}")
            written.append(str(Path(out_dir) / f"a{i}.html"))
        # 与真实 article_reader 契约一致：out_dir 下生成 index.html 说明页
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text(
            "<html><body><h1>fake index</h1>"
            + "".join(f"<p>{i}</p>" for i in range(len(articles)))
            + "</body></html>",
            encoding="utf-8",
        )
        return {
            "ok": len(articles),
            "failed": 0,
            "errors": [],
            "written": written,
            "out_dir": str(out_dir),
            "fmt": "html",
            "index": str(out / "index.html"),
        }

    mod.batch_export_articles = batch_export_articles

    def fetch_and_parse_article(url, *, cred=None, timeout=25.0):
        return {
            "title": "fake title",
            "body_text": "这是用于内容筛选的正文，包含足够的技术细节与实现方法，长度超过二十个字。",
            "body_html": "<p>这是用于内容筛选的正文，包含足够的技术细节与实现方法，长度超过二十个字。</p>",
            "link": url,
        }

    mod.fetch_and_parse_article = fetch_and_parse_article
    return mod


def _fake_ai_filter() -> types.ModuleType:
    mod = types.ModuleType("mp_harvest.core.ai_filter")

    @dataclass
    class ModelConfig:
        id: str = ""
        name: str = ""
        base_url: str = ""
        api_key: str = ""
        model: str = ""
        enabled: bool = True
        format: str = "openai"

        @classmethod
        def from_dict(cls, data):
            return cls(
                id=str(data.get("id") or ""),
                name=str(data.get("name") or "未命名模型"),
                base_url=str(data.get("base_url") or ""),
                api_key=str(data.get("api_key") or ""),
                model=str(data.get("model") or ""),
                enabled=bool(data.get("enabled", True)),
                format=str(data.get("format") or "openai"),
            )

        def to_dict(self):
            return {
                "id": self.id,
                "name": self.name,
                "base_url": self.base_url,
                "api_key": self.api_key,
                "model": self.model,
                "enabled": self.enabled,
                "format": self.format,
            }

    mod.ModelConfig = ModelConfig
    mod.DEFAULT_PRINCIPLES = "内置默认原则"
    mod.DEFAULT_CONTENT_PRINCIPLES = "内置默认内容原则"
    mod._models: list = [ModelConfig(name="m1", api_key="k")]
    mod._principles = "默认原则"
    mod._content_principles = "默认内容原则"

    mod.load_models = lambda path: list(mod._models)

    def save_models(path, models):
        mod._models = list(models)

    mod.save_models = save_models
    mod.load_principles = lambda path: mod._principles

    def save_principles(path, text):
        mod._principles = text

    mod.save_principles = save_principles
    mod.load_content_principles = lambda path: mod._content_principles

    def save_content_principles(path, text):
        mod._content_principles = text

    mod.save_content_principles = save_content_principles
    mod.build_system_prompt = lambda principles=None: f"PROMPT:{principles}"

    def test_connection(cfg):
        if cfg.api_key:
            return True, "连接成功"
        return False, "缺少 api_key"

    mod.test_connection = test_connection

    def fetch_models(cfg):
        if not cfg.api_key:
            return False, "未填写 API Key"
        if cfg.format == "anthropic":
            return False, "Anthropic 接口不支持拉取模型列表"
        if "bad" in cfg.base_url:
            return False, "HTTP 401：API Key 无效"
        return True, ["deepseek-chat", "deepseek-reasoner"]

    mod.fetch_models = fetch_models

    def judge_articles(articles, models, *, prompt="", cache_path=None, on_progress=None, on_batch=None, **kw):
        if not [m for m in models if m.enabled]:
            raise ValueError("没有启用的 AI 模型")
        kept = []
        for i, a in enumerate(articles):
            if on_progress:
                on_progress(i + 1, len(articles))
            row = {**a, "keep": True, "reason": "fake"}
            kept.append(row)
            if on_batch:
                on_batch([row], None)
        return {
            "ok": True,
            "kept": kept,
            "dropped": [],
            "errors": [],
            "used_models": [m.name for m in models],
            "cached": 0,
            "judged": len(kept),
        }

    mod.judge_articles = judge_articles
    return mod


def _fake_settings() -> types.ModuleType:
    mod = types.ModuleType("mp_harvest.core.settings")
    mod._data: dict[str, Any] = {"proxy": ""}
    mod.load_settings = lambda root_dir=None: dict(mod._data)

    def save_settings(root_dir, payload):
        mod._data = dict(payload)

    mod.save_settings = save_settings
    return mod


def _fake_mitm_capture() -> types.ModuleType:
    mod = types.ModuleType("mp_harvest.infra.mitm.mitm_capture")

    class MitmCaptureService:
        def __init__(self, app_root: Path) -> None:
            self.app_root = Path(app_root)
            self.port = 8088
            self._running = False
            self.reset_called = False

        @property
        def running(self) -> bool:
            return self._running

        def start(self, *, set_system_proxy: bool = True):
            self._running = True
            return True, "抓包代理已启动 127.0.0.1:8088"

        def stop(self, *, restore_proxy: bool = True):
            self._running = False
            return True, "抓包代理已停止"

        def reset_capture_state(self) -> None:
            self.reset_called = True

    mod.MitmCaptureService = MitmCaptureService
    return mod


def _fake_capture_target() -> types.ModuleType:
    mod = types.ModuleType("mp_harvest.core.capture_target")

    def expected_biz(row: dict) -> str:
        cred = row.get("credentials") or {}
        return str(row.get("biz") or cred.get("__biz") or "")

    mod.expected_biz = expected_biz
    return mod


FAKE_MODULES = {
    "mp_harvest.core.store": _fake_store,
    "mp_harvest.core.credentials": _fake_credentials,
    "mp_harvest.core.batch_import": _fake_batch_import,
    "mp_harvest.core.capture_target": _fake_capture_target,
    "mp_harvest.core.sightings": _fake_sightings,
    "mp_harvest.core.history_client": _fake_history_client,
    "mp_harvest.core.history_export": _fake_history_export,
    "mp_harvest.core.article_reader": _fake_article_reader,
    "mp_harvest.core.ai_filter": _fake_ai_filter,
    "mp_harvest.core.settings": _fake_settings,
    "mp_harvest.infra.mitm.mitm_capture": _fake_mitm_capture,
}


# ── fake platform ─────────────────────────────────────────────────


class FakePlatform:
    def __init__(self) -> None:
        from mp_harvest.infra.platform.base import (
            DownloadResult,
            InstallResult,
            ProxyResult,
            UpdateCheckResult,
        )

        self.ca = types.SimpleNamespace(
            needs_admin=True,
            install=lambda: InstallResult(ok=True, needs_admin=True, message="CA 已安装"),
            status=lambda: True,
            cert_path=lambda: Path("/fake/mitmproxy-ca-cert.pem"),
        )
        self.proxy = types.SimpleNamespace(
            needs_admin=True,
            enable=lambda port: ProxyResult(ok=True, message=f"代理已开启:{port}"),
            disable=lambda: ProxyResult(ok=True, message="代理已关闭"),
        )

        def _download(url, *, proxy=None, on_progress=None):
            if on_progress:
                on_progress(1, 1)
            return DownloadResult(ok=True, path="/fake/pkg.zip")

        self.updater = types.SimpleNamespace(
            check=lambda proxy=None: UpdateCheckResult(
                ok=True, available=True, version="v9.9.9", zip_url="https://x/y.zip"
            ),
            download=_download,
            apply=lambda p: None,
        )

    def info(self):
        return {
            "os": "mac",
            "ca_needs_admin": True,
            "proxy_needs_admin": True,
            "data_dir": "/fake/data",
            "engine": "fake",
            "version": "2.0.0",
        }

    def shell_open(self, path):
        return None


# ── fixtures ──────────────────────────────────────────────────────


@pytest.fixture()
def fake_core(monkeypatch):
    mods = {}
    for name, factory in FAKE_MODULES.items():
        m = factory()
        monkeypatch.setitem(sys.modules, name, m)
        # 关键：同时把假模块绑定到父包属性上——否则一旦真实子模块被导入过
        # （如 watcher 线程在测试环境里创建真实 store），`from pkg import mod`
        # 会优先取父包已绑定的真实属性，sys.modules 注入即失效（2026-08-09）。
        parent_name, _, attr = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            monkeypatch.setattr(parent, attr, m, raising=False)
        mods[name.rsplit(".", 1)[-1]] = m
    from mp_harvest.server import state

    state.reset()
    yield types.SimpleNamespace(**mods)
    state.reset()


@pytest.fixture()
def fake_platform(monkeypatch):
    plat = FakePlatform()
    from mp_harvest.server.routes import mitm as mitm_route
    from mp_harvest.server.routes import platform as platform_route
    from mp_harvest.server.routes import update as update_route

    monkeypatch.setattr(mitm_route, "get_platform", lambda: plat)
    monkeypatch.setattr(platform_route, "get_platform", lambda: plat)
    monkeypatch.setattr(update_route, "get_platform", lambda: plat)
    return plat


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """隔离数据目录：文章缓存等落盘不污染真实 mp_harvest/data（2026-08-09）。"""
    import mp_harvest.infra.platform.paths as paths_mod

    d = tmp_path / "data"
    monkeypatch.setattr(paths_mod, "data_dir", lambda: d)
    return d


@pytest.fixture()
def client(fake_core, fake_platform, isolated_data_dir):
    from fastapi.testclient import TestClient

    from mp_harvest.server.app import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def token() -> str:
    from mp_harvest.server import get_token

    return get_token()


@pytest.fixture()
def auth(token) -> dict[str, str]:
    return {"token": token}


@pytest.fixture()
def auth_headers(token) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def wait_task(task_id: str, timeout: float = 5.0):
    """轮询任务注册表直到终态。"""
    from mp_harvest.server.tasks import registry

    deadline = time.time() + timeout
    while time.time() < deadline:
        task = registry.get(task_id)
        if task and task.status in ("done", "error", "cancelled"):
            return task
        time.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内结束")


def add_account(client, auth, name="测试号", url="https://mp.weixin.qq.com/s/abc") -> dict:
    resp = client.post("/api/accounts", params=auth, json={"name": name, "url": url})
    assert resp.status_code == 201, resp.text
    return resp.json()  # POST /api/accounts 响应为裸 Account 对象（前端对齐）


def give_credential(account_id: str) -> None:
    from mp_harvest.server import state

    store = state.get_store()
    for row in store._rows:  # fake store 内存行
        if row["id"] == account_id:
            row["credentials"] = {"__biz": "fakebiz", "key": "k"}
            row["biz"] = "fakebiz"
            row["status"] = "active"

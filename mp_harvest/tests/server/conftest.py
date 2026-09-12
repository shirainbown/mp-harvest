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
    # 与真实 core.sightings 契约一致：server.state.get_sightings 经此取统一路径
    mod.default_sightings_path = lambda root=None: Path("/fake/article_sightings.json")
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

    def fetch_history_range(cred, *, start_ts, end_ts=0, on_progress=None, sightings=None, **kw):
        """fake：记录调用参数，返回一篇落在窗口内的文章。"""
        mod.last_range_call = {"start_ts": start_ts, "end_ts": end_ts}
        if on_progress:
            on_progress("正在拉取第 1 页")
        return {
            "ok": True,
            "articles": [
                {
                    "title": "范围内文章",
                    "link": "https://mp.weixin.qq.com/s/range1",
                    "publish_ts": start_ts + 3600,
                    "identity": "art-range-1",
                }
            ],
            "pages": 1,
            "warning": "",
            "nickname": "真实公众号",
        }

    mod.fetch_history_days = fetch_history_days
    mod.fetch_history_range = fetch_history_range
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

    def batch_export_articles(
        articles,
        *,
        out_dir,
        cred=None,
        account_name="",
        download_images=False,
        on_progress=None,
        check_cancelled=None,
        records=None,
    ):
        written = []
        errors = []
        interrupted = False
        for i, a in enumerate(articles, 1):
            try:
                if check_cancelled:
                    check_cancelled()
                if on_progress:
                    on_progress(f"正在导出 {i}/{len(articles)}")
            except Exception:  # noqa: BLE001  # 取消边界
                interrupted = True
                break
            # 2026-09：凭证过期不再阻止导出（真实实现会照常尝试拉正文），
            # 这里保持同样的契约，否则会掩盖真实行为。
            written.append(str(Path(out_dir) / f"a{i}.html"))
        # 与真实 article_reader 契约一致：out_dir 下生成 index.html 说明页
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text(
            "<html><body><h1>fake index</h1>"
            + "".join(f"<p>{i}</p>" for i in range(len(written)))
            + "</body></html>",
            encoding="utf-8",
        )
        return {
            "ok": not interrupted,
            "exported": len(written),
            "skipped": 0,
            "failed": len(errors),
            "errors": errors,
            "written": written,
            "out_dir": str(out),
            "fmt": "html",
            "index": str(out / "index.html"),
            "partial": interrupted,
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

    # 周报归档复用 article_reader 的这几个入口（2026-09）。这里给语义等价的
    # 轻量实现 —— 真实的排版细节由 tests/test_weekly_report.py 用真模块覆盖，
    # 本文件只保证 server 层拿到符合契约的返回值。
    # 相对定位，绝不写绝对路径 —— 测试文件会进公开仓库，写死 /Users/<用户名>/…
    # 会把本机目录结构泄露出去（2026-09 自查发现）
    mod._resolve_template_dir = lambda: (
        Path(__file__).resolve().parents[2] / "core" / "templates"
    )
    mod._article_content_hash = (
        lambda *, link="", body_html="", body_text="": __import__("hashlib")
        .sha256((body_html or body_text or link or "").encode("utf-8", "ignore"))
        .hexdigest()[:8]
    )

    def safe_export_filename(title, *, ext, index=0, date="", account="", content_hash=""):
        import re as _re

        parts = []
        d = _re.sub(r"[^0-9-]+", "", str(date or ""))[:10]
        if d:
            parts.append(d)
        acct = _re.sub(r'[\\/:*?"<>|]+', "_", str(account or "").strip()).strip("_")
        if acct:
            parts.append(acct)
        parts.append(_re.sub(r'[\\/:*?"<>|]+', "_", (title or "article").strip())[:48] or "article")
        h = _re.sub(r"[^0-9a-fA-F]", "", str(content_hash))[:8]
        if h:
            parts.append(h)
        return "_".join(parts) + f".{ext}"

    mod.safe_export_filename = safe_export_filename

    def write_article_export(path, art, **kw):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"<html><body><h1>{art.get('title', '')}</h1>"
            f"<div>{art.get('body_html') or art.get('body_text') or ''}</div></body></html>",
            encoding="utf-8",
        )
        return p

    mod.write_article_export = write_article_export
    mod.render_article_html = lambda art, **kw: f"<html><body>{art.get('title', '')}</body></html>"

    def _render_index_page(rows, *, account_name=""):
        links = "".join(
            f'<a href="{r.get("file", "")}">{r.get("title", "")}</a>' for r in rows
        )
        return f"<html><body><h1>{account_name} · 文章目录</h1>{links}</body></html>"

    mod._render_index_page = _render_index_page
    mod._html_to_text = lambda fragment: __import__("re").sub(r"<[^>]+>", " ", str(fragment or "")).strip()
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
        prefix = kw.get("prefix") or ""
        keep_key = f"{prefix}keep" if prefix else "keep"
        reason_key = f"{prefix}reason" if prefix else "reason"
        kept = []
        for i, a in enumerate(articles):
            if on_progress:
                on_progress(i + 1, len(articles))
            row = {**a, keep_key: True, reason_key: "fake"}
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

    # 周报复用真实传输层（core/weekly_report.llm_json → ai_filter._call_model），
    # 假模块必须提供同名入口，否则测试连打桩都打不上（2026-09）。
    def _call_model(cfg, system_prompt, user_content, max_retries=3, **kw):
        raise NotImplementedError("测试里请自行 monkeypatch _call_model")

    mod._call_model = _call_model

    # 传输层失败的专用异常（真实模块里是 RuntimeError 的子类，2026-09 加）。
    # 周报的批处理靠它区分「端点坏了」与「返回的东西解析不了」—— 两者处置不同：
    # 前者不重试，后者降级逐篇。假模块必须同形，否则 weekly_report 连 import 都过不去。
    class ModelCallError(RuntimeError):
        pass

    mod.ModelCallError = ModelCallError
    # 真实模块用 build_prompt 拼「原则 + 固定输出要求」，周报侧同形，这里给个占位
    mod.FIXED_OUTPUT_REQUIREMENTS = "【输出格式（必须严格遵守，软件固定，不可更改）】"

    # 「其他来源」路由用只读的 load_verdicts 把判定合并进列表行（2026-09）。
    # 与真实实现同契约：读不到 / 坏文件一律空字典，且**不写盘**。
    _verdicts: dict[str, Any] = {"title": {}, "content": {}}

    def load_verdicts(cache_path, prefix=""):
        key = "content" if prefix else "title"
        return dict(_verdicts[key])

    mod.load_verdicts = load_verdicts
    mod._verdicts = _verdicts
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

        def _download(url, *, proxy=None, on_progress=None, should_cancel=None):
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
    """隔离数据目录：文章缓存等落盘不污染真实 mp_harvest/data（2026-08-09）。

    2026-09 补全：settings / sightings / ai_filter 是 ``from ...paths import
    data_dir`` **直接绑定了名字**的，只 patch paths 模块漏掉它们 —— 于是
    ``export_records.get_records()`` 之类仍会写到真实的 mp_harvest/data
    （曾因此在该目录留下带测试临时路径的 harvest.db 记录）。
    """
    import mp_harvest.core.ai_filter as ai_mod
    import mp_harvest.core.event_log as log_mod
    import mp_harvest.core.external_sources as ext_mod
    import mp_harvest.core.export_records as rec_mod
    import mp_harvest.core.settings as settings_mod
    import mp_harvest.core.sightings as sightings_mod
    import mp_harvest.infra.platform.paths as paths_mod

    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    for mod in (paths_mod, settings_mod, sightings_mod, ai_mod):
        monkeypatch.setattr(mod, "data_dir", lambda *a, **k: d, raising=False)
    # 外部来源库 / 执行日志 / 导出记录都是**进程内单例**，DB 路径在首次取用时才解析。
    # 不复位的话第二个测试仍连着上一个测试的 tmp 数据目录 —— 条目会跨测试串味，
    # 日志则会被写进一个已经删掉的目录（静默丢失，测试也断言不了内容）。
    #
    # 导出记录是 2026-09 补的：`/api/articles` 现在每次请求都会读它（判定「已导出」），
    # 不复位的话第一个测试建的库会被后面所有测试共用，导出标记跨测试乱亮。
    ext_mod.reset_external_store()
    log_mod.reset_event_log()
    rec_mod.reset_records()
    yield d
    ext_mod.reset_external_store()
    log_mod.reset_event_log()
    rec_mod.reset_records()


@pytest.fixture()
def client(isolated_data_dir, fake_core, fake_platform):
    # 注意顺序：isolated_data_dir 必须先于 fake_core 实例化——后者会把假模块
    # 塞进 sys.modules 并绑定到父包属性，之后再 `import mp_harvest.core.*` 会解析到假包。
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

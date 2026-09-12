"""settings / update 路由契约。"""

from __future__ import annotations

import socket
import threading

from mp_harvest.tests.server.conftest import add_account, give_credential, wait_task


def test_settings_get_put(client, auth):
    resp = client.get("/api/settings", params=auth)
    assert resp.status_code == 200
    assert "settings" in resp.json()

    resp = client.put("/api/settings", params=auth, json={"proxy": "http://127.0.0.1:9", "theme": "dark"})
    assert resp.status_code == 200
    resp = client.get("/api/settings", params=auth)
    assert resp.json()["settings"]["theme"] == "dark"


def test_settings_put_non_object_422(client, auth):
    resp = client.put("/api/settings", params=auth, json=["not", "object"])
    assert resp.status_code == 422


def test_test_proxy_no_proxy_400(client, auth):
    resp = client.post("/api/settings/test-proxy", params=auth, json={"proxy": ""})
    assert resp.status_code == 400


def test_test_proxy_unreachable(client, auth):
    # 127.0.0.1:9（discard 端口）本机必然不可达
    resp = client.post(
        "/api/settings/test-proxy", params=auth, json={"proxy": "http://127.0.0.1:9"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_test_proxy_reachable(client, auth):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept(), daemon=True).start()
    try:
        resp = client.post(
            "/api/settings/test-proxy",
            params=auth,
            json={"proxy": f"http://127.0.0.1:{port}"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
    finally:
        srv.close()


def test_test_proxy_bad_address_400(client, auth):
    resp = client.post("/api/settings/test-proxy", params=auth, json={"proxy": "http://nohost"})
    assert resp.status_code == 400


def test_update_check(client, auth):
    resp = client.get("/api/update/check", params=auth)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["available"] is True
    assert data["version"] == "v9.9.9"


def test_update_check_failure_structured(client, auth, fake_platform, monkeypatch):
    from mp_harvest.infra.platform.base import UpdateCheckResult

    monkeypatch.setattr(
        fake_platform.updater,
        "check",
        lambda proxy=None: UpdateCheckResult(ok=False, message="无法访问 GitHub", error="timeout"),
    )
    resp = client.get("/api/update/check", params=auth)
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def _spy_check(fake_platform, monkeypatch, seen: list):
    from mp_harvest.infra.platform.base import UpdateCheckResult

    def _check(proxy=None):
        seen.append(proxy)
        return UpdateCheckResult(ok=True, available=False, version="v2.1.19", message="ok")

    monkeypatch.setattr(fake_platform.updater, "check", _check)


def test_update_check_direct_mode_ignores_stored_proxy(client, auth, fake_platform, monkeypatch):
    """回归：mode=direct 时必须忽略 settings 里残留的自定义代理地址。

    旧版 _settings_proxy 只看 proxy 键，用户切回直连后残留的代理地址仍生效，
    「直连」实际走 Clash 中转 → 共享出口被 GitHub 限流 → 检查更新永远失败。
    """
    import sys

    fake_settings = sys.modules["mp_harvest.core.settings"]
    monkeypatch.setattr(
        fake_settings,
        "load_settings",
        lambda: {"mode": "direct", "proxy": "http://127.0.0.1:7897"},
    )
    seen: list = []
    _spy_check(fake_platform, monkeypatch, seen)
    resp = client.get("/api/update/check", params=auth)
    assert resp.status_code == 200
    assert seen == [None], f"直连模式不应使用存储的代理地址，实际传入: {seen}"


def test_update_check_custom_mode_uses_proxy(client, auth, fake_platform, monkeypatch):
    import sys

    fake_settings = sys.modules["mp_harvest.core.settings"]
    monkeypatch.setattr(
        fake_settings,
        "load_settings",
        lambda: {"mode": "custom", "proxy": "http://127.0.0.1:7897"},
    )
    seen: list = []
    _spy_check(fake_platform, monkeypatch, seen)
    resp = client.get("/api/update/check", params=auth)
    assert resp.status_code == 200
    assert seen == ["http://127.0.0.1:7897"]


def test_update_download_task(client, auth):
    resp = client.post(
        "/api/update/download", params=auth, json={"zip_url": "https://x/y.zip"}
    )
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    assert task.result["path"] == "/fake/pkg.zip"


def test_update_download_failure_task_error(client, auth, fake_platform, monkeypatch):
    from mp_harvest.infra.platform.base import DownloadResult

    monkeypatch.setattr(
        fake_platform.updater,
        "download",
        lambda url, *, proxy=None, on_progress=None, should_cancel=None: DownloadResult(ok=False, error="网络中断"),
    )
    resp = client.post(
        "/api/update/download", params=auth, json={"zip_url": "https://x/y.zip"}
    )
    assert resp.status_code == 202
    task = wait_task(resp.json()["task_id"])
    assert task.status == "error"
    assert "网络中断" in task.error


# ── update/apply（应用已下载更新）─────────────────────────────────


def test_update_apply_not_downloaded_409(client, auth, monkeypatch, tmp_path):
    monkeypatch.setattr("mp_harvest.infra.platform.paths.data_dir", lambda: tmp_path)
    resp = client.post("/api/update/apply", params=auth)
    assert resp.status_code == 409
    assert "尚未下载" in resp.json()["detail"]


def test_update_apply_ok(client, auth, fake_platform, monkeypatch, tmp_path):
    update_dir = tmp_path / "update"
    update_dir.mkdir()
    pkg = update_dir / "MP Harvest-mac.zip"
    pkg.write_bytes(b"fake-pkg")
    monkeypatch.setattr("mp_harvest.infra.platform.paths.data_dir", lambda: tmp_path)

    applied = []
    monkeypatch.setattr(fake_platform.updater, "apply", lambda p: applied.append(str(p)))
    resp = client.post("/api/update/apply", params=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert applied == [str(pkg)]


def test_update_apply_platform_error_500(client, auth, fake_platform, monkeypatch, tmp_path):
    from mp_harvest.infra.platform.base import PlatformError

    update_dir = tmp_path / "update"
    update_dir.mkdir()
    (update_dir / "MP Harvest-mac.zip").write_bytes(b"fake-pkg")
    monkeypatch.setattr("mp_harvest.infra.platform.paths.data_dir", lambda: tmp_path)

    def _boom(p):
        raise PlatformError(f"更新包不存在：{p}")

    monkeypatch.setattr(fake_platform.updater, "apply", _boom)
    resp = client.post("/api/update/apply", params=auth)
    assert resp.status_code == 500


# ── 拉取节奏设置真的生效（2026-09）────────────────────────────────
#
# 「设置页能改」只是第一步；真正会坏在中间那段：设置键名 → `_fetch_policy()`
# → core 的 FetchPolicy。中间任何一环对不上，界面照样能填、能存、返回 200，
# 而拉取时用的还是老一套 —— 用户看不出任何异常。

FETCH_KEYS = {
    "fetch.delay_min": ("delay_min", 7),
    "fetch.delay_max": ("delay_max", 9),
    "fetch.cooldown_pages": ("cooldown_every", 3),
    "fetch.cooldown_seconds": ("cooldown_seconds", 11),
    "fetch.retries": ("retries", 5),
    "fetch.max_pages": ("max_pages", 42),
}


def _set_fetch_settings(client, auth, **over):
    body = {k: v for k, v in over.items()}
    resp = client.put("/api/settings", params=auth, json=body)
    assert resp.status_code == 200, resp.text


def test_fetch_settings_reach_the_fetch_policy(client, auth):
    """六个设置键逐个走到 FetchPolicy 的同名字段上。

    用**每个字段各不相同**的值，任何一处键名错位（比如 cooldown_pages 写进了
    cooldown_seconds）都会露馅；全都填同一个值就会互相顶包。
    """
    from mp_harvest.core import history_client as hc

    acc = add_account(client, auth)
    give_credential(acc["id"])
    _set_fetch_settings(client, auth, **{k: v[1] for k, v in FETCH_KEYS.items()})

    resp = client.post(
        "/api/history/fetch", params=auth, json={"account_id": acc["id"], "days": 7}
    )
    assert resp.status_code == 202, resp.text
    wait_task(resp.json()["task_id"])

    policy = hc.last_policy
    assert policy is not None, "路由没有把 policy 传给 history_client"
    for key, (field, want) in FETCH_KEYS.items():
        got = getattr(policy, field)
        assert float(got) == float(want), f"{key} → policy.{field} 应为 {want}，实际 {got}"


def test_fetch_settings_are_clamped(client, auth):
    """设置页边上能填的值也要夹住：后端不能假设前端一定校验过。

    越界的后果不是报错而是**拉取变哑**：`retries` 填成 1000 会把一次网络抖动
    放大成上千次请求，`max_pages` 填 0 则一页都不翻（看着像「拉不到文章」）。
    """
    from mp_harvest.core import history_client as hc

    acc = add_account(client, auth)
    give_credential(acc["id"])
    _set_fetch_settings(
        client, auth,
        **{"fetch.delay_min": -5, "fetch.delay_max": 99999,
           "fetch.cooldown_pages": -1, "fetch.cooldown_seconds": 99999,
           "fetch.retries": 1000, "fetch.max_pages": 0},
    )

    resp = client.post(
        "/api/history/fetch", params=auth, json={"account_id": acc["id"], "days": 7}
    )
    wait_task(resp.json()["task_id"])

    policy = hc.last_policy
    assert policy.delay_min == 0 and policy.delay_max == 300
    assert policy.cooldown_every == 0 and policy.cooldown_seconds == 3600
    assert policy.retries == 10
    assert policy.max_pages == 1, "max_pages 必须有下界，0 会一页都不翻"


def test_rate_limited_is_passed_through_to_the_ui(client, auth):
    """限流要**单独透出** `rate_limited`，不能只丢一句 error。

    前端靠这个标记把提示做成「先别急着重试」；缺了它，界面上只是一条普通红色
    报错 —— 而用户的下一动作就是再点一次，那正是让封锁变久的那一下。
    """
    from mp_harvest.core import history_client as hc

    acc = add_account(client, auth)
    give_credential(acc["id"])
    hc.next_result = {
        "ok": False,
        "rate_limited": True,
        "error": hc.RATE_LIMIT_MESSAGE,
        "articles": [],
        "pages": 1,
    }
    try:
        resp = client.post(
            "/api/history/fetch", params=auth, json={"account_id": acc["id"], "days": 7}
        )
        task = wait_task(resp.json()["task_id"])
    finally:
        hc.next_result = None

    # 契约：单账号拉取返回的是**扁平**结果，rate_limited 在顶层
    # （前端 articles.ts 读的就是这些顶层字段；它缺失时用户只会看到一条普通红报错）
    assert task.result.get("rate_limited") is True, f"rate_limited 没透出：{task.result}"
    # 文案由 core 给出、路由**原样透出**（内容本身在 test_history_range 里钉）
    assert task.result["error"] == hc.RATE_LIMIT_MESSAGE


def test_batch_results_keep_rate_limited(client, auth):
    """批量拉取的每个子结果也要带 rate_limited（前端据此提示「换个号也没用」）。"""
    from mp_harvest.core import history_client as hc

    acc = add_account(client, auth, name="批量号")
    give_credential(acc["id"])
    hc.next_result = {
        "ok": False,
        "rate_limited": True,
        "error": hc.RATE_LIMIT_MESSAGE,
        "articles": [],
        "pages": 1,
    }
    try:
        resp = client.post(
            "/api/history/fetch-batch",
            params=auth,
            json={"account_ids": [acc["id"]], "days": 7},
        )
        task = wait_task(resp.json()["task_id"])
    finally:
        hc.next_result = None

    assert task.result["results"][0]["rate_limited"] is True

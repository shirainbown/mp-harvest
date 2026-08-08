"""settings / update 路由契约。"""

from __future__ import annotations

import socket
import threading

from mp_harvest.tests.server.conftest import wait_task


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
        lambda url, *, proxy=None, on_progress=None: DownloadResult(ok=False, error="网络中断"),
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

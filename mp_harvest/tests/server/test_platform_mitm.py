"""platform / mitm / ca 路由契约。"""

from __future__ import annotations

from mp_harvest.infra.platform.base import InstallResult


def test_platform_info(client, auth):
    resp = client.get("/api/platform", params=auth)
    assert resp.status_code == 200
    data = resp.json()
    for key in ("os", "ca_needs_admin", "proxy_needs_admin", "data_dir", "engine", "version"):
        assert key in data


def test_mitm_status(client, auth):
    """GET /api/mitm/status → {running, port}（前端进凭证页时拉取）。"""
    resp = client.get("/api/mitm/status", params=auth)
    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is False
    assert data["port"] == 8088

    resp = client.post("/api/mitm/start", params=auth)
    assert resp.status_code == 200
    resp = client.get("/api/mitm/status", params=auth)
    assert resp.json()["running"] is True


def test_mitm_start_stop(client, auth):
    resp = client.post("/api/mitm/start", params=auth)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] and data["running"] and data["port"] == 8088

    resp = client.post("/api/mitm/stop", params=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["running"] is False


def test_mitm_start_failure_500(client, auth, fake_core):
    from mp_harvest.server import state

    svc = state.get_mitm()
    svc.start = lambda **kw: (False, "端口 8088 被占用")
    resp = client.post("/api/mitm/start", params=auth)
    assert resp.status_code == 500
    assert "8088" in resp.json()["detail"]


def test_ca_install_ok(client, auth):
    resp = client.post("/api/ca/install", params=auth)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["needs_admin"] is True  # mac 语义：前端据此提示授权


def test_ca_install_failure_structured(client, auth, fake_platform, monkeypatch):
    monkeypatch.setattr(
        fake_platform.ca,
        "install",
        lambda: InstallResult(ok=False, needs_admin=True, error="user canceled", message="用户取消了授权"),
    )
    resp = client.post("/api/ca/install", params=auth)
    assert resp.status_code == 200  # 结构化错误内嵌，不是 5xx
    data = resp.json()
    assert data["ok"] is False
    assert data["error"] == "user canceled"


def test_ca_status(client, auth):
    resp = client.get("/api/ca/status", params=auth)
    assert resp.status_code == 200
    data = resp.json()
    assert data["installed"] is True
    # 契约双写：前端按 trusted 判断（2026-08-09 修复字段不一致）
    assert data["trusted"] is True
    assert "cert_path" in data


def test_ca_open_opens_cert_folder(client, auth, monkeypatch):
    """POST /api/ca/open 调平台 shell_open 打开证书所在目录（2026-08-09 补）。"""
    from pathlib import Path
    from types import SimpleNamespace

    opened: list[str] = []
    from mp_harvest.server.routes import mitm as mitm_routes

    def fake_get():
        return SimpleNamespace(
            ca=SimpleNamespace(cert_path=lambda: Path("/fake/mitmproxy-ca-cert.pem")),
            shell_open=lambda path: opened.append(str(path)),
        )

    monkeypatch.setattr(mitm_routes, "get_platform", fake_get)
    resp = client.post("/api/ca/open", params=auth)
    assert resp.status_code == 200, resp.text
    assert opened == ["/fake"]  # cert_path=/fake/mitmproxy-ca-cert.pem → 父目录

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


def test_ca_open_opens_cert_file(client, auth, monkeypatch):
    """POST /api/ca/open 调平台 shell_open 直接打开证书文件本身（B13 修复：不再打开数据目录）。"""
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
    assert opened == ["/fake/mitmproxy-ca-cert.pem"]  # 打开文件本身，而非父目录


# ── POST /api/shell/open：打开本地文件/目录（2026-09 修复）───────────
#
# 背景：前端原用 openExternal('file://' + path)，而 shell 侧 open_external
# 只放行 http(s)，file:// 一律返回 False，且 desktop.ts 没 await 那个 Promise
# —— 于是周报的「打开报告/打开目录/上次输出」和「其他来源」的
# 「打开本地正文/打开 PDF」五个按钮点得动却毫无反应，也不报错。


def _platform_with_shell_open(opened: list[str], raises=None):
    from types import SimpleNamespace

    def _open(path):
        if raises is not None:
            raise raises
        opened.append(str(path))

    return SimpleNamespace(shell_open=_open)


def test_shell_open_opens_directory(client, auth, monkeypatch, tmp_path):
    """目录要走平台层 shell_open —— 它能把目录交给 Finder，
    webbrowser.open('file://目录') 只会让浏览器列个目录页。"""
    from mp_harvest.server.routes import platform as platform_routes

    d = tmp_path / "第17期_2026-09-07"
    d.mkdir()
    opened: list[str] = []
    monkeypatch.setattr(platform_routes, "get_platform",
                        lambda: _platform_with_shell_open(opened))

    resp = client.post("/api/shell/open", json={"path": str(d)}, params=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "path": str(d), "is_dir": True}
    assert opened == [str(d)]


def test_shell_open_opens_file(client, auth, monkeypatch, tmp_path):
    from mp_harvest.server.routes import platform as platform_routes

    f = tmp_path / "report.html"
    f.write_text("<html></html>", encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(platform_routes, "get_platform",
                        lambda: _platform_with_shell_open(opened))

    resp = client.post("/api/shell/open", json={"path": str(f)}, params=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_dir"] is False
    assert opened == [str(f)]


def test_shell_open_strips_file_scheme(client, auth, monkeypatch, tmp_path):
    """历史调用方传的是 file:// 形式 —— 去掉前缀后仍能打开，
    否则老前端代码会一直拿到 404。"""
    from mp_harvest.server.routes import platform as platform_routes

    f = tmp_path / "a.html"
    f.write_text("x", encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(platform_routes, "get_platform",
                        lambda: _platform_with_shell_open(opened))

    resp = client.post("/api/shell/open", json={"path": f"file://{f}"}, params=auth)
    assert resp.status_code == 200, resp.text
    assert opened == [str(f)]


def test_shell_open_missing_path_404(client, auth, monkeypatch, tmp_path):
    """路径不存在要给 404 + 人话错误，而不是让 PlatformError 冒成 500。"""
    from mp_harvest.server.routes import platform as platform_routes

    opened: list[str] = []
    monkeypatch.setattr(platform_routes, "get_platform",
                        lambda: _platform_with_shell_open(opened))

    missing = tmp_path / "不存在"
    resp = client.post("/api/shell/open", json={"path": str(missing)}, params=auth)
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]
    assert opened == []          # 没到平台层就挡住了


def test_shell_open_empty_path_400(client, auth):
    resp = client.post("/api/shell/open", json={"path": "   "}, params=auth)
    assert resp.status_code == 400


def test_shell_open_platform_failure_500(client, auth, monkeypatch, tmp_path):
    """平台层拒绝（权限等）要变成 500 + 原因，前端才能提示「打开失败」。"""
    from mp_harvest.infra.platform.base import PlatformError
    from mp_harvest.server.routes import platform as platform_routes

    f = tmp_path / "a.html"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        platform_routes, "get_platform",
        lambda: _platform_with_shell_open([], raises=PlatformError("open 失败：权限不足")),
    )
    resp = client.post("/api/shell/open", json={"path": str(f)}, params=auth)
    assert resp.status_code == 500
    assert "权限不足" in resp.json()["detail"]


def test_shell_open_requires_token(client, tmp_path):
    """端点能开任意本地路径，必须留在 token 鉴权之后。"""
    f = tmp_path / "a.html"
    f.write_text("x", encoding="utf-8")
    resp = client.post("/api/shell/open", json={"path": str(f)})
    assert resp.status_code == 401


def test_frontend_never_sends_file_scheme_to_open_external():
    """源码级护栏：本地文件一律走 /api/shell/open，不许再用 openExternal('file://…')。

    shell 侧 open_external 只放行 http(s)，传 file:// 只会被挡下 ——
    这正是 2026-09 那批按钮「点了没反应」的成因。前端没有单测，
    用这条扫描把回归挡在 CI 里。
    """
    import re
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    pat = re.compile(r"openExternal\(\s*[`'\"]file://")
    offenders: list[str] = []
    for f in src_root.rglob("*"):
        if f.suffix not in (".ts", ".vue"):
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            # 只跳过**整行注释**，不能按 "//" 切分 —— `file://` 自己就含 "//"，
            # 一切就把要找的东西切没了（这条护栏第一版就是这么失效的：
            # 变异测试把它抓了出来）。注释里提到 file:// 是允许的（说明用）。
            if line.lstrip().startswith(("//", "*", "/*")):
                continue
            if pat.search(line):
                offenders.append(f"{f.relative_to(src_root)}:{i}")
    assert not offenders, f"这些地方仍在把 file:// 交给 openExternal：{offenders}"

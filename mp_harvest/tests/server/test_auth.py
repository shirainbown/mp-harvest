"""token 中间件契约（设计稿 §3.5）：/api/* 与 /ws 全覆盖。"""

from __future__ import annotations


def test_api_without_token_401(client):
    resp = client.get("/api/platform")
    assert resp.status_code == 401
    assert resp.json()["detail"]


def test_api_wrong_token_401(client):
    resp = client.get("/api/platform", params={"token": "wrong"})
    assert resp.status_code == 401


def test_api_with_query_token(client, auth):
    resp = client.get("/api/platform", params=auth)
    assert resp.status_code == 200


def test_api_with_bearer_header(client, auth_headers):
    resp = client.get("/api/platform", headers=auth_headers)
    assert resp.status_code == 200


def test_root_no_token_ok(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_ws_without_token_rejected(client):
    import pytest

    with pytest.raises(Exception):  # 4401 关闭 → 连接失败
        with client.websocket_connect("/ws"):
            pass


def test_ws_with_token_ok(client, auth):
    with client.websocket_connect("/ws", params=auth):
        pass

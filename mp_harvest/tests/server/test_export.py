"""export 路由契约：列表导出（同步）+ HTML 正文导出（任务）。"""

from __future__ import annotations

from mp_harvest.tests.server.conftest import add_account, give_credential, wait_task


def _prepare_articles(client, auth):
    from mp_harvest.server import state

    acc = add_account(client, auth)
    give_credential(acc["id"])
    state.set_articles(
        acc["id"],
        [
            {"title": "A", "link": "https://x/1", "publish_ts": 2, "identity": "art-0"},
            {"title": "B", "link": "https://x/2", "publish_ts": 1, "identity": "art-1"},
        ],
    )
    return acc


def test_export_list_formats(client, auth):
    """纯文本响应（前端复制/下载附件），格式映射与 Content-Disposition 文件名。"""
    acc = _prepare_articles(client, auth)
    core_fmt = {"title+links": "title_links", "md": "markdown"}
    for fmt in ("json", "csv", "tsv", "md", "links", "title+links"):
        resp = client.get(
            "/api/articles/export-list",
            params={**auth, "account_id": acc["id"], "format": fmt},
        )
        assert resp.status_code == 200, fmt
        assert resp.headers["content-type"].startswith("text/plain")
        assert f"FMT={core_fmt.get(fmt, fmt)}" in resp.text
        assert "attachment" in resp.headers["content-disposition"]


def test_export_list_view_filter(client, auth):
    """view=all/keep/drop：始终只导出当前视图（§5.5）。"""
    acc = _prepare_articles(client, auth)
    from mp_harvest.server import state

    articles = state.get_articles(acc["id"])
    articles[0]["keep"] = True
    articles[1]["keep"] = False
    state.set_articles(acc["id"], articles)

    for view, expected in (("all", 2), ("keep", 1), ("drop", 1)):
        resp = client.get(
            "/api/articles/export-list",
            params={**auth, "account_id": acc["id"], "view": view, "format": "json"},
        )
        assert resp.status_code == 200
        assert f"N={expected}" in resp.text


def test_export_list_bad_view_400(client, auth):
    acc = _prepare_articles(client, auth)
    resp = client.get(
        "/api/articles/export-list",
        params={**auth, "account_id": acc["id"], "view": "bogus"},
    )
    assert resp.status_code == 400


def test_export_list_bad_format_400(client, auth):
    acc = _prepare_articles(client, auth)
    resp = client.get(
        "/api/articles/export-list",
        params={**auth, "account_id": acc["id"], "format": "docx"},
    )
    assert resp.status_code == 400


def test_export_list_unknown_account_404(client, auth):
    resp = client.get(
        "/api/articles/export-list", params={**auth, "account_id": "nope", "format": "json"}
    )
    assert resp.status_code == 404


def test_export_html_task(client, auth):
    acc = _prepare_articles(client, auth)
    resp = client.post(
        "/api/articles/export-html", params=auth, json={"account_id": acc["id"]}
    )
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    assert task.result["ok"] is True
    assert task.result["exported"] == 2
    assert task.result["skipped"] == 0
    assert task.result["failed"] == 0
    assert isinstance(task.result["out_dir"], str) and task.result["out_dir"]
    assert task.result["fmt"] == "html"  # 正文导出只有 HTML（§6）


def test_export_html_by_ids(client, auth):
    acc = _prepare_articles(client, auth)
    resp = client.post(
        "/api/articles/export-html",
        params=auth,
        json={"account_id": acc["id"], "ids": ["art-0"]},
    )
    assert resp.status_code == 202
    task = wait_task(resp.json()["task_id"])
    assert task.result["ok"] is True
    assert task.result["exported"] == 1


def test_export_html_all_accounts(client, auth):
    """account_id 为空 = 导出全部公众号当前视图的文章。"""
    from mp_harvest.server import state

    acc1 = add_account(client, auth)
    # B18 起相同 article_url 返回 409，第二个账号需用不同链接
    acc2 = add_account(client, auth, url="https://mp.weixin.qq.com/s/abc2")
    give_credential(acc1["id"])
    give_credential(acc2["id"])
    state.set_articles(
        acc1["id"],
        [{"title": "A", "link": "https://x/1", "publish_ts": 2, "identity": "art-0", "keep": True}],
    )
    state.set_articles(
        acc2["id"],
        [{"title": "B", "link": "https://x/2", "publish_ts": 1, "identity": "art-1", "keep": False}],
    )
    resp = client.post(
        "/api/articles/export-html",
        params=auth,
        json={"account_id": "", "view": "keep"},
    )
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    assert task.result["ok"] is True
    assert task.result["exported"] == 1


def test_export_html_custom_dir_and_view(client, auth, tmp_path):
    """指定目录 + 视图过滤（2026-08-09）：out_dir 生效且生成 index.html 说明页。"""
    acc = _prepare_articles(client, auth)
    from mp_harvest.server import state

    articles = state.get_articles(acc["id"])
    articles[0]["keep"] = True
    articles[1]["keep"] = False
    state.set_articles(acc["id"], articles)
    out = tmp_path / "custom-export"

    resp = client.post(
        "/api/articles/export-html",
        params=auth,
        json={"account_id": acc["id"], "view": "keep", "out_dir": str(out)},
    )
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    assert task.result["ok"] is True
    assert task.result["exported"] == 1
    assert task.result["out_dir"] == str(out)
    assert (out / "index.html").is_file()
    assert task.result["index"] == str(out / "index.html")


def test_export_html_no_articles_400(client, auth):
    acc = add_account(client, auth)
    resp = client.post(
        "/api/articles/export-html", params=auth, json={"account_id": acc["id"]}
    )
    assert resp.status_code == 400


def test_export_html_expired_credential_no_longer_blocks(client, auth, monkeypatch):
    """2026-09 修正：凭证过期**不再阻止**导出。

    微信文章页 /s/... 是公开可读的（实测不带任何凭证也能拿到完整正文），
    所以过期账号的文章照常尝试导出，不像原 B10 那样前置跳过。
    凭证状态只作为「拉不到正文时」的可能原因提示传给导出任务。
    """
    from mp_harvest.server import state

    acc1 = add_account(client, auth)
    acc2 = add_account(client, auth, url="https://mp.weixin.qq.com/s/abc2")
    give_credential(acc1["id"])
    give_credential(acc2["id"])
    state.set_articles(
        acc1["id"],
        [{"title": "A", "link": "https://x/1", "publish_ts": 2, "identity": "art-0"}],
    )
    state.set_articles(
        acc2["id"],
        [{"title": "B", "link": "https://x/2", "publish_ts": 1, "identity": "art-1"}],
    )
    # fake store 无 is_active：注入一个，模拟 acc2 凭证过期
    store = state.get_store()
    monkeypatch.setattr(store, "is_active", lambda aid: aid == acc1["id"], raising=False)

    from mp_harvest.core import article_reader as fake_reader

    captured: dict = {}
    orig = fake_reader.batch_export_articles

    def spy(articles, **kwargs):
        captured["articles"] = [dict(a) for a in articles]
        return orig(articles, **kwargs)

    monkeypatch.setattr(fake_reader, "batch_export_articles", spy)

    resp = client.post(
        "/api/articles/export-html", params=auth, json={"account_id": "", "view": "all"}
    )
    assert resp.status_code == 202, resp.text  # 不整批 409
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    assert task.result["ok"] is True
    # 两个账号都照常导出（凭证过期不再是拦截理由）
    assert task.result["exported"] == 2, task.result
    assert task.result["failed"] == 0, task.result
    assert not any("凭证" in e for e in task.result["errors"]), task.result["errors"]
    # 但提示仍然送达导出任务：拉不到正文时用它说明可能原因
    hinted = [a for a in captured["articles"] if a.get("_cred_error")]
    assert len(hinted) == 1 and "凭证" in hinted[0]["_cred_error"], captured


def test_export_html_download_images_defaults_to_settings(client, auth, monkeypatch):
    """B7：download_images 未传时读后端设置 export.download_images（默认 False）。"""
    from mp_harvest.core import article_reader as fake_reader
    from mp_harvest.core import settings as settings_mod

    calls: dict = {}
    orig = fake_reader.batch_export_articles

    def spy(*args, **kwargs):
        calls["download_images"] = kwargs.get("download_images")
        return orig(*args, **kwargs)

    monkeypatch.setattr(fake_reader, "batch_export_articles", spy)

    acc = _prepare_articles(client, auth)
    resp = client.post(
        "/api/articles/export-html", params=auth, json={"account_id": acc["id"]}
    )
    assert resp.status_code == 202, resp.text
    wait_task(resp.json()["task_id"])
    assert calls["download_images"] is False  # 默认 False

    # 打开设置 → 默认变为 True
    data = settings_mod.load_settings()
    data["export.download_images"] = True
    settings_mod.save_settings(None, data)
    resp = client.post(
        "/api/articles/export-html", params=auth, json={"account_id": acc["id"]}
    )
    assert resp.status_code == 202, resp.text
    wait_task(resp.json()["task_id"])
    assert calls["download_images"] is True

    # 请求体显式 False 覆盖设置 True
    resp = client.post(
        "/api/articles/export-html",
        params=auth,
        json={"account_id": acc["id"], "download_images": False},
    )
    assert resp.status_code == 202, resp.text
    wait_task(resp.json()["task_id"])
    assert calls["download_images"] is False

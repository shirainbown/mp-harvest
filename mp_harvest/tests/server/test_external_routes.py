"""「其他来源」路由契约测试（server/routes/external.py）。

只验证 server 层契约（路由/参数/任务/行形状），目录扫描与写回的语义由
``tests/test_external_sources.py`` 覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

from .conftest import wait_task


def _paper(arxiv_id: str, title: str, date: str = "2026-09-07") -> dict:
    return {
        "title": title,
        "abstract": f"{title} 的摘要",
        "url": f"http://arxiv.org/abs/{arxiv_id}",
        "arxiv_id": arxiv_id,
        "authors": ["A"],
        "date": date,
        "categories": ["cs.DC"],
        "primary_category": "cs.DC",
    }


def _make_dir(tmp_path: Path, name: str = "papers") -> Path:
    root = tmp_path / name
    d = root / "2026-09-07"
    d.mkdir(parents=True, exist_ok=True)
    (d / "papers_data.json").write_text(
        json.dumps([_paper("2608.1v1", "甲"), _paper("2608.2v1", "乙")], ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def _add(client, auth, path, name="论文") -> dict:
    r = client.post("/api/external/sources", params=auth, json={"name": name, "path": str(path)})
    assert r.status_code == 201, r.text
    return r.json()


def _scan(client, auth, source_id: str) -> dict:
    r = client.post(f"/api/external/sources/{source_id}/scan", params=auth)
    assert r.status_code == 202, r.text
    task = wait_task(r.json()["task_id"])
    assert task.status == "done", task.error
    return task.result


# ── 来源目录 CRUD ─────────────────────────────────────────────────


def test_sources_crud(client, auth, tmp_path):
    root = _make_dir(tmp_path)
    src = _add(client, auth, root)
    assert src["name"] == "论文"
    assert src["enabled"] == 1
    assert src["item_count"] == 0

    assert len(client.get("/api/external/sources", params=auth).json()) == 1

    # 改名 / 停用
    r = client.patch(f"/api/external/sources/{src['id']}", params=auth,
                     json={"name": "改后的名字", "enabled": False})
    assert r.status_code == 200
    assert r.json()["name"] == "改后的名字"
    assert r.json()["enabled"] == 0

    # 删除
    r = client.delete(f"/api/external/sources/{src['id']}", params=auth)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.get("/api/external/sources", params=auth).json() == []


def test_source_404s(client, auth):
    assert client.patch("/api/external/sources/无此id", params=auth,
                        json={"name": "x"}).status_code == 404
    assert client.delete("/api/external/sources/无此id", params=auth).status_code == 404
    assert client.post("/api/external/sources/无此id/scan", params=auth).status_code == 404


def test_add_source_rejects_missing_dir(client, auth, tmp_path):
    """登记一个不存在的目录要 400，否则用户得到一个永远扫不出东西的来源。"""
    r = client.post("/api/external/sources", params=auth,
                    json={"name": "x", "path": str(tmp_path / "没有这个目录")})
    assert r.status_code == 400
    assert "目录不存在" in r.json()["detail"]


def test_add_source_is_idempotent(client, auth, tmp_path):
    root = _make_dir(tmp_path)
    a = _add(client, auth, root, "名一")
    b = _add(client, auth, root, "名二")
    assert a["id"] == b["id"]
    assert len(client.get("/api/external/sources", params=auth).json()) == 1


def test_delete_source_keeps_files_on_disk(client, auth, tmp_path):
    """移除登记**只解除注册**，磁盘上的目录与文件一概不动。"""
    root = _make_dir(tmp_path)
    src = _add(client, auth, root)
    _scan(client, auth, src["id"])
    client.delete(f"/api/external/sources/{src['id']}", params=auth)
    assert (root / "2026-09-07" / "papers_data.json").is_file()
    assert len(list((root / "2026-09-07").iterdir())) == 1


# ── 扫描 ──────────────────────────────────────────────────────────


def test_scan_indexes_items(client, auth, tmp_path):
    root = _make_dir(tmp_path)
    src = _add(client, auth, root)
    result = _scan(client, auth, src["id"])
    assert result["ok"] is True
    assert result["seen"] == 2 and result["new"] == 2

    row = client.get("/api/external/sources", params=auth).json()[0]
    assert row["item_count"] == 2
    assert row["last_scan_seen"] == 2


def test_rescan_is_idempotent(client, auth, tmp_path):
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    result = _scan(client, auth, src["id"])
    assert result["new"] == 0
    assert len(client.get("/api/external/items", params=auth).json()) == 2


# ── 条目列表 ──────────────────────────────────────────────────────


def test_items_shape_and_public_id(client, auth, tmp_path):
    """行的 id 必须带来源前缀 —— 否则两个来源下的同一篇会算出同一个 id。"""
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    rows = client.get("/api/external/items", params=auth).json()
    assert len(rows) == 2
    row = rows[0]
    assert row["id"].startswith(f"{src['id']}:ext:")
    assert row["source"] == "外"
    assert row["account_name"] == "论文"
    assert row["title"] == "甲"
    assert row["url"] == "http://arxiv.org/abs/2608.1v1"
    assert row["verdict"] is None
    assert row["domain"] == ""
    assert row["item_key"] == "arxiv:2608.1"


def test_same_paper_in_two_sources_gets_distinct_ids(client, auth, tmp_path):
    """同一篇论文登记在两个目录下 → 两个不同的 id，聚合视图里才不会串号。"""
    a = _add(client, auth, _make_dir(tmp_path, "a"), "来源A")
    b = _add(client, auth, _make_dir(tmp_path, "b"), "来源B")
    _scan(client, auth, a["id"])
    _scan(client, auth, b["id"])
    rows = client.get("/api/external/items", params=auth).json()
    assert len(rows) == 4  # 每个来源各 2 条
    assert len({r["id"] for r in rows}) == 4
    assert {r["account_name"] for r in rows} == {"来源A", "来源B"}


def test_items_search_and_source_filter(client, auth, tmp_path):
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])

    assert len(client.get("/api/external/items", params={**auth, "q": "甲"}).json()) == 1
    assert len(client.get("/api/external/items", params={**auth, "q": "不存在"}).json()) == 0
    assert len(client.get("/api/external/items",
                          params={**auth, "source_id": src["id"]}).json()) == 2
    assert len(client.get("/api/external/items",
                          params={**auth, "source_id": "别的"}).json()) == 0


def test_items_merge_verdicts_from_cache(client, auth, tmp_path, fake_core):
    """判定结果从 AI 缓存合并进列表行（外部条目不另存判定，避免两处真相）。"""
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    fake_core.ai_filter._verdicts["title"]["ext:arxiv:2608.1"] = {
        "keep": True, "reason": "相关", "title_keep": True, "title_reason": "相关"
    }
    rows = client.get("/api/external/items", params=auth).json()
    hit = next(r for r in rows if r["item_key"] == "arxiv:2608.1")
    assert hit["verdict"] == "keep"
    assert hit["reason"] == "相关"

    # 带 verdict 过滤
    only_keep = client.get("/api/external/items", params={**auth, "verdict": "keep"}).json()
    assert [r["item_key"] for r in only_keep] == ["arxiv:2608.1"]


def test_items_excludes_disabled_source_from_aggregate(client, auth, tmp_path):
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    client.patch(f"/api/external/sources/{src['id']}", params=auth, json={"enabled": False})
    assert client.get("/api/external/items", params=auth).json() == []
    # 单独指定仍可查看（用户主动点进去看）
    assert len(client.get("/api/external/items",
                          params={**auth, "source_id": src["id"]}).json()) == 2


# ── 写回 ──────────────────────────────────────────────────────────


def test_export_writes_papers_json_and_bodies(client, auth, tmp_path):
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    out = tmp_path / "out"

    r = client.post("/api/external/export", params=auth,
                    json={"source_id": src["id"], "out_dir": str(out)})
    assert r.status_code == 202, r.text
    task = wait_task(r.json()["task_id"])
    assert task.status == "done", task.error
    assert task.result["written"] == 2

    payload = json.loads((out / "2026-09-07" / "papers_data.json").read_text(encoding="utf-8"))
    assert {p["title"] for p in payload} == {"甲", "乙"}
    assert (out / "2026-09-07" / "2608.1v1.html").is_file()
    assert (out / "2026-09-07" / "2608.2v1.html").is_file()


def test_export_by_ids(client, auth, tmp_path):
    """按 id 导出只写选中的条目（不能把整个来源都写出去）。"""
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    rows = client.get("/api/external/items", params=auth).json()
    out = tmp_path / "out"

    r = client.post("/api/external/export", params=auth,
                    json={"source_id": src["id"], "ids": [rows[0]["id"]], "out_dir": str(out)})
    task = wait_task(r.json()["task_id"])
    assert task.result["written"] == 1
    payload = json.loads((out / "2026-09-07" / "papers_data.json").read_text(encoding="utf-8"))
    assert len(payload) == 1


def test_export_rejects_bad_date_dir(client, auth, tmp_path):
    """``date_dir`` 来自请求体，直接拼路径会穿越出目标目录 —— 必须挡在 400。"""
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    for bad in ("../../etc", "2026-09-07/../..", "not-a-date", ""):
        if bad == "":
            continue
        r = client.post("/api/external/export", params=auth,
                        json={"source_id": src["id"], "date_dir": bad, "out_dir": str(tmp_path)})
        assert r.status_code == 400, f"{bad} 应被拒绝，却得到 {r.status_code}"


def test_export_with_no_items_is_400(client, auth, tmp_path):
    src = _add(client, auth, _make_dir(tmp_path))  # 未扫描
    r = client.post("/api/external/export", params=auth,
                    json={"source_id": src["id"], "out_dir": str(tmp_path / "out")})
    assert r.status_code == 400


def test_export_unknown_source_is_404(client, auth, tmp_path):
    r = client.post("/api/external/export", params=auth,
                    json={"source_id": "无此id", "out_dir": str(tmp_path / "out")})
    assert r.status_code == 404


# ── AI 筛选 ───────────────────────────────────────────────────────


def test_filter_runs_and_reports(client, auth, tmp_path):
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    r = client.post("/api/external/filter", params=auth,
                    json={"source_id": src["id"], "stage": "title"})
    assert r.status_code == 202, r.text
    task = wait_task(r.json()["task_id"])
    assert task.status == "done", task.error
    assert task.result["judged"] == 2


def test_filter_with_no_items_is_400(client, auth, tmp_path):
    src = _add(client, auth, _make_dir(tmp_path))  # 未扫描
    r = client.post("/api/external/filter", params=auth,
                    json={"source_id": src["id"], "stage": "title"})
    assert r.status_code == 400
    assert "没有可筛选" in r.json()["detail"]


def test_content_filter_requires_title_keep(client, auth, tmp_path):
    """没有通过标题筛选的条目时，内容筛选要明确报错而不是空跑。"""
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    r = client.post("/api/external/filter", params=auth,
                    json={"source_id": src["id"], "stage": "content"})
    assert r.status_code == 400
    assert "标题筛选" in r.json()["detail"]


def test_filter_unknown_source_is_404(client, auth):
    r = client.post("/api/external/filter", params=auth,
                    json={"source_id": "无此id", "stage": "title"})
    assert r.status_code == 404

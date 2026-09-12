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
    # 外部条目不走「导出到本地 HTML」那条路，恒为未导出。
    # 这条专门堵住「默认值写错」那类静默 bug：`article_out` 曾经可以有个
    # exported 默认值，翻成 True 时历史那条路照样绿，只有外部行会挂假标记。
    assert row["exported"] is False


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
    # ⚠️ 夹具必须是**真实缓存条目的形状**：真缓存里只有 ``title_keep``/
    # ``title_reason``，**没有** ``keep``/``reason``（那是列表端要现算出来的）。
    # 原先这里顺手塞了 keep/reason，于是「列表端没算最终判定」这个 bug 一直绿
    # —— 夹具比现实更宽容，测试就成了摆设（2026-09）。
    fake_core.ai_filter._verdicts["title"]["ext:arxiv:2608.1"] = {
        "title_keep": True, "title_reason": "相关"
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


def test_content_filter_runs_without_title_stage(client, auth, tmp_path, monkeypatch):
    """外部条目**可以直接做内容筛选**，不要求先跑标题筛选（2026-09 放开）。

    微信侧那条「请先执行标题筛选」的 400 是**成本保护** —— 那边内容筛选要联网
    逐篇抓正文，标题先行能把抓取量压下来。外部条目的正文本地就有
    （``read_external_body``：正文文件 → summary_cn → abstract，全程不联网），
    卡它一道只是照搬了邻居的规则。

    这里同时钉住**内容筛选真正读到的正文是什么** —— 未写回过的来源目录没有
    本地正文文件，读到的就是 papers_data.json 里的英文 abstract。
    """
    from mp_harvest.core import ai_filter as ai_mod

    seen: list[list[dict]] = []
    real = ai_mod.judge_articles

    def recording(articles, models, **kw):
        seen.append([dict(a) for a in articles])
        return real(articles, models, **kw)

    monkeypatch.setattr(ai_mod, "judge_articles", recording)

    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    r = client.post("/api/external/filter", params=auth,
                    json={"source_id": src["id"], "stage": "content"})
    assert r.status_code == 202, r.text          # 直接跑得起来，不再被 400 挡住
    task = wait_task(r.json()["task_id"])
    assert task.status == "done", task.error

    assert seen, "内容筛选没有把条目交给 judge_articles"
    bodies = [str(a.get("body_text") or "") for a in seen[0]]
    assert len(bodies) == 2, "应当把两条都送去判定，而不是先按 title_keep 过滤掉"
    assert all("的摘要" in b for b in bodies), f"正文不是 abstract：{bodies}"


def test_filter_unknown_source_is_404(client, auth):
    r = client.post("/api/external/filter", params=auth,
                    json={"source_id": "无此id", "stage": "title"})
    assert r.status_code == 404


def test_list_does_not_parse_bodies(client, auth, tmp_path, monkeypatch):
    """列表接口**不得**解析原文 —— 它算完就把 body_text 丢掉了。

    读正文现在会去解析本地原文（PDF 可能几百毫秒一篇），而列表对**每一条**都调
    ``_core_row``，结果 ``mappers.article_out`` 根本不返回 ``body_text``：
    打开一个有几百篇论文的来源页会当场冷解析全库、看着像卡死。
    用「被调用次数」断言而不是计时 —— 计时在 CI 上不稳。
    """
    from mp_harvest.core import external_sources as ext

    calls: list[int] = []
    real = ext.read_external_body
    monkeypatch.setattr(
        ext, "read_external_body",
        lambda item: (calls.append(1), real(item))[1],
    )

    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])

    rows = client.get("/api/external/items", params=auth).json()
    assert rows, "前提：确实有条目可列"
    assert calls == [], f"列表路径解析了 {len(calls)} 次正文"


def test_format_endpoint_serves_the_documented_schema(client, auth, tmp_path):
    """格式说明由后端提供（前端不硬编码），且示例必须真能解析。

    这条同时是「文档与解析器同源」的守卫：示例里改了字段名、写坏了 JSON，
    或者说明里列的文件名与扫描白名单不一致，都会在这里红。
    """
    b = client.get("/api/external/format", params=auth).json()
    assert set(b) == {"filenames", "fields", "example"}
    assert "papers_data.json" in b["filenames"]
    names = {f["name"] for f in b["fields"]}
    assert {"title", "url", "fulltext"} <= names

    # 示例必须能被**真的解析器**读出来 —— 这是这一整块存在的意义
    from mp_harvest.core.external_sources import FORMAT_EXAMPLE_ITEM_COUNT, parse_papers_data

    f = tmp_path / "papers_data.json"
    f.write_text(b["example"], encoding="utf-8")
    rows = parse_papers_data(f, fallback_date="2026-09-12")
    assert len(rows) == FORMAT_EXAMPLE_ITEM_COUNT, f"示例只能解析出 {len(rows)} 条"
    # 三条示例要覆盖三种情形：带原文的、带 HTML 原文的、只有摘要的
    assert any(r["fulltext_rel"].endswith(".pdf") for r in rows)
    assert any(r["fulltext_rel"].endswith(".html") for r in rows)
    assert any(not r["fulltext_rel"] for r in rows)


def test_format_text_has_no_markdown_markers(client, auth):
    """格式说明里的文字**不能带 markdown 标记** —— 界面是纯文本渲染的。

    写 `**原文文件**` 会原样显示成「**原文文件**」。这个错我犯过三次（两次在
    模板里、一次在这份字段表里），所以钉一条：新增字段时顺手写上星号就会红。
    示例 JSON 是给用户复制去当文件用的，同样不该有。
    """
    b = client.get("/api/external/format", params=auth).json()
    for f in b["fields"]:
        for key, val in f.items():
            assert "**" not in str(val), f"字段 {f['name']} 的 {key} 里有 markdown 标记：{val}"
    assert "**" not in b["example"]


# ── 外部条目的「最终判定」（2026-09 用户报：筛完看不出区别）──────────
#
# 外部条目不把判定存进自己的库（避免两处真相），判定只活在 ai_filter 的两阶段
# 缓存里，读的时候合并。**合并出 keep/reason 这一步原先漏了**：`_core_row` 只把
# `title_keep`/`title_reason` 放进行里，而界面「判定」列读的是 `keep` ——
# 于是筛完仍然是满屏「待判」，用户看到的结论是「AI 筛选根本没生效」。


def _seed_verdicts(fake_core, *, title=None, content=None) -> None:
    """往假模块的判定桶里塞条目。

    ⚠️ 条目必须是**真实缓存的形状**（只有 ``title_keep``/``title_reason`` 这种
    带前缀的字段，没有 ``keep``/``reason``）—— 列表端的最终判定是**现算**的，
    夹具里顺手塞 keep 会让「没算」这个 bug 一直绿（2026-09 就是这么漏掉的）。

    也**不能往磁盘写缓存文件**：server 契约测试把 ``core.ai_filter`` 换成了假模块，
    它根本不读文件 —— 那样写出来的用例要么恒绿，要么依赖别的用例留下的内存状态
    （第一版两条测试就是这样互相顶包的，单跑必红）。
    """
    for bucket, entries in (("title", title), ("content", content)):
        if entries:
            fake_core.ai_filter._verdicts[bucket].update(entries)


def _items(client, auth) -> list[dict]:
    return client.get("/api/external/items", params=auth).json()


def test_items_derive_final_verdict_from_title_cache(client, auth, tmp_path, fake_core):
    """标题阶段判过 → 列表的 verdict/reason 必须有值（不是「待判」）。"""
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    assert {r["verdict"] for r in _items(client, auth)} == {None}, "前提：还没判定过"

    _seed_verdicts(
        fake_core,
        title={
            "ext:arxiv:2608.1": {"title_keep": True, "title_reason": "标题相关"},
            "ext:arxiv:2608.2": {"title_keep": False, "title_reason": "标题无关"},
        },
    )
    rows = {r["title"]: r for r in _items(client, auth)}
    assert rows["甲"]["verdict"] == "keep" and rows["甲"]["reason"] == "标题相关"
    assert rows["乙"]["verdict"] == "drop" and rows["乙"]["reason"] == "标题无关"
    # 两阶段的字段也照旧透出（界面按它显示阶段明细）
    assert rows["甲"]["title_verdict"] == "keep"


def test_content_verdict_wins_over_title(client, auth, tmp_path, fake_core):
    """内容筛选是第二阶段，它的判定才是最终意见；理由也要跟着它自己那阶段。"""
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    _seed_verdicts(
        fake_core,
        title={"ext:arxiv:2608.1": {"title_keep": True, "title_reason": "标题说留"}},
        content={"ext:arxiv:2608.1": {"content_keep": False, "content_reason": "正文说扔"}},
    )
    row = next(r for r in _items(client, auth) if r["title"] == "甲")
    assert row["verdict"] == "drop", row
    assert row["reason"] == "正文说扔", "理由必须来自内容阶段，不能张冠李戴"
    assert row["title_verdict"] == "keep" and row["content_verdict"] == "drop"


def test_unjudged_item_stays_pending(client, auth, tmp_path, fake_core):
    """没判过的条目保持待判 —— 不能因为「合并判定」这步就冒出一个默认值。"""
    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    _seed_verdicts(
        fake_core,
        title={"ext:arxiv:2608.1": {"title_keep": True, "title_reason": "只有这一条判过"}},
    )
    rows = {r["title"]: r for r in _items(client, auth)}
    assert rows["甲"]["verdict"] == "keep"
    assert rows["乙"]["verdict"] is None and rows["乙"]["reason"] == ""


def test_weekly_and_list_agree_on_external_verdicts(client, auth, tmp_path, fake_core):
    """**漂移守卫**：周报与列表对同一批外部条目必须算出同样的判定。

    2026-09 的 bug 正是这两处不一致：周报那份（``_external_verdicts``）算出了
    最终判定、筛选确实生效；列表那份没算，界面全是「待判」。两处各写一遍优先级
    规则必然再次漂移，所以现在共用 ``core.verdicts.final_verdict``，这条用例盯着
    「共用」这件事本身。

    注：这里用的是假模块的内存判定（契约测试只验证「接线」）；优先级规则本身
    由 ``tests/test_verdicts.py`` 用真实现覆盖。
    """
    from mp_harvest.server.routes import weekly as weekly_mod

    src = _add(client, auth, _make_dir(tmp_path))
    _scan(client, auth, src["id"])
    _seed_verdicts(
        fake_core,
        title={
            "ext:arxiv:2608.1": {"title_keep": True, "title_reason": "标题留"},
            "ext:arxiv:2608.2": {"title_keep": False, "title_reason": "标题扔"},
        },
        content={"ext:arxiv:2608.2": {"content_keep": True, "content_reason": "正文留"}},
    )

    listed = {
        r["id"].split(":", 1)[1]: (r["verdict"], r["reason"]) for r in _items(client, auth)
    }
    weekly = weekly_mod._external_verdicts()
    assert listed, "列表没有条目，用例白跑"
    for key, (verdict, reason) in listed.items():
        w = weekly.get(key)
        assert w is not None, f"周报没认出这条：{key}"
        assert w["keep"] == (verdict == "keep"), f"{key} 判定不一致：周报 {w} vs 列表 {verdict}"
        assert w["reason"] == reason, f"{key} 理由不一致：{w['reason']!r} vs {reason!r}"

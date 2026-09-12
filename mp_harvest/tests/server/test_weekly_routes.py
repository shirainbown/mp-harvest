"""周报路由契约测试（server/routes/weekly.py）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .conftest import add_account, wait_task


def _ts(y: int, m: int, d: int, h: int = 10) -> int:
    """本地时区的某时刻（避免手写 epoch 算错日期、把种子数据落在查询窗口外）。"""
    from datetime import datetime

    return int(datetime(y, m, d, h).timestamp())


def _seed_articles(account_id: str, n: int = 3, *, y: int = 2026, m: int = 9, d: int = 5,
                   tag: str = "") -> None:
    """给某账号塞 n 篇文章。

    ``tag`` 用来区分账号：候选去重是按 ``identity`` 做的，两个账号用同一批
    ``mid:0/1/2`` 会被正确地合并成一份 —— 想造「两个账号各有一批」就得给不同的
    identity（真实数据里 mid 本来就是每篇唯一的）。
    """
    from mp_harvest.server import state

    base = _ts(y, m, d)
    state.set_articles(
        account_id,
        [
            {"title": f"文章{tag}{i}", "link": f"https://mp.weixin.qq.com/s/{tag}a{i}",
             "publish_ts": base + i * 3600, "publish_at": f"{y}-{m:02d}-{d:02d} 10:00",
             "identity": f"mid:{tag}{i}", "body_text": "正文内容足够长以便当作真实候选处理。",
             "body_html": f"<p>正文{i}</p>"}
            for i in range(n)
        ],
    )


def _stub_model(monkeypatch, *, calls: list[dict] | None = None):
    """打分桩**按 user 里的【第 N 篇】逐篇回显**（2026-09 批处理改造后必需）。

    早先一律只回 ``idx: 0``：批模式下只有第 1 篇能对上，其余静默走补漏/兜底，
    测试却照样绿。``calls`` 传入列表时会记下每次调用的 user，供断言请求形状。
    """
    from mp_harvest.core import ai_filter as af

    def fake_call(cfg, system, user, max_retries=3, **kw):
        if calls is not None:
            calls.append({"system": system, "user": user})
        if '"score"' in system:
            n = len(re.findall(r"【第 \d+ 篇】", user))
            return json.dumps({"items": [
                {"idx": i, "score": 8.0 - i * 0.1, "semiconductor": True,
                 "title_cn": "译名", "domain": "AI芯片架构与推理优化",
                 "business_tags": ["公共"], "reason": "理由"} for i in range(max(1, n))]},
                ensure_ascii=False)
        if '"key_innovation"' in system:
            return json.dumps({"key_innovation": "k", "data_results": "d", "summary": "s"},
                              ensure_ascii=False)
        if '"brief"' in system:
            return json.dumps({"items": [{"idx": 0, "brief": "摘要"}]}, ensure_ascii=False)
        return "本期精选若干篇。\n① 看点"

    monkeypatch.setattr(af, "_call_model", fake_call)


# ── 预览 ──────────────────────────────────────────────────────────


def test_preview_counts_and_suggested_issue(client, auth, tmp_path, monkeypatch):
    acc = add_account(client, auth)
    _seed_articles(acc["id"], n=3)
    out = tmp_path / "周报"
    for n in ("第15期_2026-08-24", "第16期_2026-08-31"):
        (out / n).mkdir(parents=True)
    client.put("/api/settings", params=auth, json={"weekly.dir": str(out)})

    r = client.get("/api/weekly/preview", params={**auth, "from_date": "2026-09-01",
                                                 "to_date": "2026-09-07"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["total"] == 3 and b["wechat"] == 3
    assert b["suggested_issue"] == 17          # 扫描已有目录 +1
    assert b["out_dir"] == str(out)
    assert b["template_exists"] is True
    assert b["template_is_custom"] is False


def test_preview_rejects_bad_dates(client, auth):
    assert client.get("/api/weekly/preview",
                      params={**auth, "from_date": "不是日期"}).status_code == 400
    r = client.get("/api/weekly/preview",
                   params={**auth, "from_date": "2026-09-07", "to_date": "2026-09-01"})
    assert r.status_code == 400 and "早于" in r.json()["detail"]


def test_preview_respects_date_window(client, auth):
    acc = add_account(client, auth)
    _seed_articles(acc["id"], n=3)  # 2026-09-05
    old = client.get("/api/weekly/preview",
                     params={**auth, "from_date": "2020-01-01", "to_date": "2020-01-07"})
    assert old.json()["total"] == 0


# ── 提示词 ────────────────────────────────────────────────────────


def test_prompts_get_and_put(client, auth):
    r = client.get("/api/weekly/prompts", params=auth)
    assert r.status_code == 200
    p = r.json()["prompts"]
    assert set(p) == {"scoring", "detail", "intro", "brief"}
    assert p["scoring"]["text"] == p["scoring"]["default"]

    assert client.put("/api/weekly/prompts", params=auth,
                      json={"key": "scoring", "text": "只看光刻"}).status_code == 200
    p2 = client.get("/api/weekly/prompts", params=auth).json()["prompts"]
    assert p2["scoring"]["text"] == "只看光刻"
    assert p2["scoring"]["default"] != "只看光刻"          # 默认值不受影响（可恢复）
    assert p2["detail"]["text"] == p2["detail"]["default"]  # 其它段没被波及


def test_prompts_reject_unknown_key(client, auth):
    r = client.put("/api/weekly/prompts", params=auth, json={"key": "nope", "text": "x"})
    assert r.status_code == 400 and "未知" in r.json()["detail"]


def test_prompt_edit_prunes_only_that_stage_cache(client, auth, isolated_data_dir):
    """改提示词后，该阶段用不到的旧缓存要被清掉（否则文件无限膨胀）。"""
    from mp_harvest.core import weekly_report as wr

    cache = wr.WeeklyCache(isolated_data_dir / "weekly" / "cache.json")
    old_fp = wr.prompt_fingerprint("scoring", wr.DEFAULT_SCORING)
    det_fp = wr.prompt_fingerprint("detail", wr.DEFAULT_DETAIL)
    cache.put("scores", f"{old_fp}:a1", {"score": 1})
    cache.put("details", f"{det_fp}:a1", {"summary": "s"})

    r = client.put("/api/weekly/prompts", params=auth,
                   json={"key": "scoring", "text": "换一套标准"})
    assert r.json()["pruned"] == 1

    fresh = wr.WeeklyCache(isolated_data_dir / "weekly" / "cache.json")
    assert fresh.get("scores", f"{old_fp}:a1") is None       # 旧打分缓存被清
    assert fresh.get("details", f"{det_fp}:a1") == {"summary": "s"}   # 解读缓存不受影响


# ── 往期 ──────────────────────────────────────────────────────────


def test_list_issues(client, auth, tmp_path):
    out = tmp_path / "周报"
    (out / "第16期_2026-08-31").mkdir(parents=True)
    (out / "第16期_2026-08-31" / "报告.html").write_text("x", encoding="utf-8")
    (out / "第17期_2026-09-07" / "data").mkdir(parents=True)
    (out / "第17期_2026-09-07" / "data" / "report.json").write_text("{}", encoding="utf-8")
    (out / "随手建的").mkdir()
    client.put("/api/settings", params=auth, json={"weekly.dir": str(out)})

    rows = client.get("/api/weekly/issues", params=auth).json()
    assert [r["issue_num"] for r in rows] == [17, 16]      # 倒序
    assert rows[0]["has_snapshot"] is True
    assert rows[1]["has_snapshot"] is False
    assert all("随手建的" not in r["name"] for r in rows)   # 非期号目录忽略


# ── 生成 ──────────────────────────────────────────────────────────


def test_generate_requires_candidates(client, auth, tmp_path):
    r = client.post("/api/weekly/generate", params=auth, json={
        "issue_num": 1, "from_date": "2020-01-01", "to_date": "2020-01-02",
        "out_dir": str(tmp_path)})
    assert r.status_code == 400 and "没有候选文章" in r.json()["detail"]


def test_generate_requires_enabled_model(client, auth, tmp_path, fake_core):
    acc = add_account(client, auth)
    _seed_articles(acc["id"], n=2)
    fake_core.ai_filter._models = []          # 没有可用模型
    r = client.post("/api/weekly/generate", params=auth, json={
        "issue_num": 1, "from_date": "2026-09-01", "to_date": "2026-09-07",
        "out_dir": str(tmp_path)})
    assert r.status_code == 400 and "AI 模型" in r.json()["detail"]


def test_generate_rejects_missing_template(client, auth, tmp_path):
    acc = add_account(client, auth)
    _seed_articles(acc["id"], n=2)
    r = client.post("/api/weekly/generate", params=auth, json={
        "issue_num": 1, "from_date": "2026-09-01", "to_date": "2026-09-07",
        "out_dir": str(tmp_path), "template_path": str(tmp_path / "不存在.html")})
    assert r.status_code == 400 and "模板文件不存在" in r.json()["detail"]


def test_generate_end_to_end(client, auth, tmp_path, monkeypatch):
    acc = add_account(client, auth)
    _seed_articles(acc["id"], n=3)
    _stub_model(monkeypatch)
    out = tmp_path / "周报"

    r = client.post("/api/weekly/generate", params=auth, json={
        "issue_num": 3, "from_date": "2026-09-01", "to_date": "2026-09-07",
        "selected_count": 2, "out_dir": str(out), "report_title": "测试快报"})
    assert r.status_code == 202, r.text
    task = wait_task(r.json()["task_id"])
    assert task.status == "done", task.error
    res = task.result
    assert res["ok"] is True, res
    assert res["selected"] == 2 and res["others"] == 1

    root = Path(res["issue_dir"])
    assert root.is_dir() and (root / "articles" / "index.html").is_file()
    assert (root / "data" / "report.json").is_file()
    assert (root / "data" / "scores.json").is_file()
    assert "测试快报" in Path(res["report_path"]).read_text(encoding="utf-8")
    assert res["missing_vars"] == []


# ── 重渲染（改模板不花钱）─────────────────────────────────────────


def test_rerender_uses_snapshot_without_ai(client, auth, tmp_path, monkeypatch):
    """用归档快照重渲染：换模板 → 输出变；且**一次模型调用都不发**。"""
    acc = add_account(client, auth)
    _seed_articles(acc["id"], n=2)
    _stub_model(monkeypatch)
    out = tmp_path / "周报"
    r = client.post("/api/weekly/generate", params=auth, json={
        "issue_num": 1, "from_date": "2026-09-01", "to_date": "2026-09-07",
        "selected_count": 2, "out_dir": str(out)})
    res = wait_task(r.json()["task_id"]).result
    issue_dir = res["issue_dir"]

    # 换个模板重渲染 —— 期间不应有任何模型调用
    from mp_harvest.core import ai_filter as af

    def boom(*a, **k):
        raise AssertionError("重渲染不应该调用模型")

    monkeypatch.setattr(af, "_call_model", boom)
    tpl = tmp_path / "new.html"
    tpl.write_text("改过的模板：{{ issue.num }}期 / {{ stats.selected_count }}篇精选",
                   encoding="utf-8")
    r2 = client.post("/api/weekly/render", params=auth,
                     json={"issue_dir": issue_dir, "template_path": str(tpl)})
    assert r2.status_code == 202, r2.text
    t2 = wait_task(r2.json()["task_id"])
    assert t2.status == "done", t2.error
    assert t2.result["ok"] is True

    html = Path(t2.result["report_path"]).read_text(encoding="utf-8")
    assert html == "改过的模板：1期 / 2篇精选"


def test_rerender_reports_missing_vars(client, auth, tmp_path, monkeypatch):
    acc = add_account(client, auth)
    _seed_articles(acc["id"], n=1)
    _stub_model(monkeypatch)
    r = client.post("/api/weekly/generate", params=auth, json={
        "issue_num": 1, "from_date": "2026-09-01", "to_date": "2026-09-07",
        "selected_count": 1, "out_dir": str(tmp_path / "out")})
    issue_dir = wait_task(r.json()["task_id"]).result["issue_dir"]

    tpl = tmp_path / "typo.html"
    tpl.write_text("{{ ISSUE_NUM }} {{ ARTILCE_NO }}", encoding="utf-8")
    t2 = wait_task(client.post("/api/weekly/render", params=auth,
                               json={"issue_dir": issue_dir,
                                     "template_path": str(tpl)}).json()["task_id"])
    assert t2.result["missing_vars"] == ["ARTILCE_NO", "ISSUE_NUM"]


def test_rerender_404_and_400(client, auth, tmp_path):
    r = client.post("/api/weekly/render", params=auth,
                    json={"issue_dir": str(tmp_path / "没有这期")})
    assert r.status_code == 404

    empty = tmp_path / "第1期_2026-09-07"
    empty.mkdir()
    r = client.post("/api/weekly/render", params=auth, json={"issue_dir": str(empty)})
    assert r.status_code == 400 and "数据快照" in r.json()["detail"]


# ── 打分批次 / 并发（2026-09）──────────────────────────────────────


def test_weekly_score_settings_are_registered(client, auth):
    """两个新键要出现在 GET 里，且**声明了类型** —— 否则 PUT 会放行任意标量
    （前端传 `true` 会被悄悄存成 1，下次生成就按「每批 1 篇」跑）。"""
    s = client.get("/api/settings", params=auth).json()["settings"]
    assert s["weekly.score_batch_size"] == 8
    assert s["weekly.workers"] == 4

    bad = client.put("/api/settings", params=auth,
                     json={"weekly.score_batch_size": True})
    assert bad.status_code == 400, bad.text


def test_generate_uses_weekly_score_settings(client, auth, tmp_path, monkeypatch):
    """周报打分必须读**周报自己的**旋钮 —— 不是 ai.batch_size。

    3 篇候选 + 每批 2 → 打分请求恰好 2 次（2 篇 + 1 篇）。若读错键（ai.batch_size=50），
    就只会发 1 次，这条断言会红。
    """
    calls: list[dict] = []
    _stub_model(monkeypatch, calls=calls)
    acc = add_account(client, auth)
    _seed_articles(acc["id"], n=3)

    s = client.get("/api/settings", params=auth).json()["settings"]
    s.update({"weekly.score_batch_size": 2, "weekly.workers": 1,
              "ai.batch_size": 50, "ai.workers": 4})
    assert client.put("/api/settings", params=auth, json=s).status_code == 200

    r = client.post("/api/weekly/generate", params=auth,
                    json={"issue_num": 1, "from_date": "2026-09-05", "to_date": "2026-09-05",
                          "selected_count": 1, "out_dir": str(tmp_path / "out")})
    assert r.status_code == 202, r.text
    task = wait_task(r.json()["task_id"])
    assert task.status == "done", task.error

    scoring = [c for c in calls if '"score"' in c["system"]]
    batches = [len(re.findall(r"【第 \d+ 篇】", c["user"])) for c in scoring]
    assert batches == [2, 1], f"打分批次不对：{batches}（说明没用 weekly.score_batch_size）"


def test_preview_returns_per_row_candidate_counts(client, auth, tmp_path):
    """逐行的「本区间 N 篇」—— 口径必须与表头的 total **一致**（都在去重之后数）。

    用户看到「其他来源目录 1 / arxiv_paper 59」和「论文 0」并排，以为坏了：
    59 是**全库条目数**（不分日期），0 是**本区间候选数**，两种数字挨着放又都没标。
    现在每行都显示区间内候选数，表头是各行之和。
    """
    a1 = add_account(client, auth, name="号一", url="https://mp.weixin.qq.com/s/one")
    a2 = add_account(client, auth, name="号二", url="https://mp.weixin.qq.com/s/two")
    _seed_articles(a1["id"], n=2, tag="x")   # 2026-09-05
    _seed_articles(a2["id"], n=3, tag="y")

    r = client.get("/api/weekly/preview", params={
        **auth, "from_date": "2026-09-05", "to_date": "2026-09-05"})
    b = r.json()
    assert b["total"] == 5
    assert b["account_counts"] == {a1["id"]: 2, a2["id"]: 3}
    # 不变量：各行之和 == 表头总数（前端两个数字并排显示，对不上就是 bug）
    assert sum(b["account_counts"].values()) == b["wechat"] == 5
    assert b["source_counts"] == {}


def test_preview_source_counts_only_inside_window(client, auth, tmp_path):
    """区间外的条目**不进**计数 —— 这正是「目录里 59 条、本区间 0 篇」的成因。"""
    d = tmp_path / "papers"
    day = d / "2026-09-07"
    day.mkdir(parents=True)
    (day / "papers_data.json").write_text(json.dumps([
        {"title": "区间内", "abstract": "a", "url": "http://arxiv.org/abs/1",
         "arxiv_id": "1v1", "date": "2026-09-07"},
        {"title": "区间外", "abstract": "b", "url": "http://arxiv.org/abs/2",
         "arxiv_id": "2v1", "date": "2026-08-01"},      # 自身日期在区间外
    ], ensure_ascii=False), encoding="utf-8")
    src = client.post("/api/external/sources", params=auth,
                      json={"name": "论文", "path": str(d)}).json()
    wait_task(client.post(f"/api/external/sources/{src['id']}/scan", params=auth)
              .json()["task_id"])

    # 全库 2 条
    assert client.get("/api/external/sources", params=auth).json()[0]["item_count"] == 2

    b = client.get("/api/weekly/preview", params={
        **auth, "from_date": "2026-09-07", "to_date": "2026-09-07"}).json()
    assert b["total"] == 1, "只有自身日期在区间内的那条算候选"
    assert b["source_counts"] == {src["id"]: 1}, "计数按**自身日期**筛，不是目录名"


def test_per_row_counts_survive_same_item_in_two_sources(client, auth, tmp_path):
    """同一篇论文同时登记在两个目录下时，**各行之和不能大于总数**。

    候选是按 item_key 去重的（后者不再计入），所以逐行计数必须在**去重之后**做。
    各自先数再加会得到 2，而表头是 1 —— 前端两个数字并排显示，对不上就是 bug。
    """
    for name in ("dx", "dy"):
        d = tmp_path / name / "2026-09-07"
        d.mkdir(parents=True)
        (d / "papers_data.json").write_text(json.dumps([
            {"title": "同一篇", "abstract": "a", "url": "http://arxiv.org/abs/9",
             "arxiv_id": "9v1", "date": "2026-09-07"},
        ], ensure_ascii=False), encoding="utf-8")

    for name in ("dx", "dy"):
        src = client.post("/api/external/sources", params=auth,
                          json={"name": name, "path": str(tmp_path / name)}).json()
        wait_task(client.post(f"/api/external/sources/{src['id']}/scan", params=auth)
                  .json()["task_id"])

    b = client.get("/api/weekly/preview", params={
        **auth, "from_date": "2026-09-07", "to_date": "2026-09-07"}).json()
    assert b["total"] == 1, "同一篇论文只算一条候选"
    assert sum(b["source_counts"].values()) == b["total"], (
        f"逐行之和 {b['source_counts']} 与总数 {b['total']} 对不上"
    )
    assert len(b["source_counts"]) == 1, "只有真正贡献了候选的那个目录才该有计数"


# ── 候选尊重 AI 筛选结果（2026-09）──────────────────────────────────


def _seed_with_verdict(account_id: str, kept: int, dropped: int, tag: str) -> None:
    """塞两批文章：一批 keep=True，一批 keep=False。"""
    from mp_harvest.server import state

    base = _ts(2026, 9, 5)
    rows = []
    for i in range(kept + dropped):
        rows.append({
            "title": f"文章{tag}{i}", "link": f"https://mp.weixin.qq.com/s/{tag}{i}",
            "publish_ts": base + i * 3600, "publish_at": "2026-09-05 10:00",
            "identity": f"mid:{tag}{i}", "keep": i < kept,
            "body_text": "正文内容足够长以便当作真实候选处理。", "body_html": "<p>x</p>",
        })
    state.set_articles(account_id, rows)


def test_preview_respects_ai_verdict_by_default(client, auth):
    """周报候选**默认**跳过被 AI 筛掉的文章。

    用户报的原话：窗口内 37 篇候选里 33 篇是他早就筛掉的 —— 那次筛选等于白做。
    """
    acc = add_account(client, auth)
    _seed_with_verdict(acc["id"], kept=2, dropped=5, tag="k")

    on = client.get("/api/weekly/preview", params={
        **auth, "from_date": "2026-09-05", "to_date": "2026-09-05"}).json()
    assert on["total"] == 2 and on["only_kept"] is True

    off = client.get("/api/weekly/preview", params={
        **auth, "from_date": "2026-09-05", "to_date": "2026-09-05",
        "only_kept": "false"}).json()
    assert off["total"] == 7 and off["only_kept"] is False, "关掉开关要能看全部"


def test_preview_keeps_unjudged_articles(client, auth):
    """未判定（keep 缺省）**照收** —— 刚拉来还没筛的不该被静默丢掉。"""
    acc = add_account(client, auth)
    _seed_with_verdict(acc["id"], kept=1, dropped=0, tag="u")
    # 再塞一篇完全没判定的
    from mp_harvest.server import state
    state.set_articles(acc["id"], [
        {"title": "没筛过的", "link": "https://mp.weixin.qq.com/s/new",
         "publish_ts": _ts(2026, 9, 5) + 99, "publish_at": "2026-09-05 10:00",
         "identity": "mid:new", "body_text": "正文。", "body_html": "<p>x</p>"},
    ] + state.get_articles(acc["id"]))

    b = client.get("/api/weekly/preview", params={
        **auth, "from_date": "2026-09-05", "to_date": "2026-09-05"}).json()
    assert b["total"] == 2, "被筛过的 + 没筛过的，两篇都该在"


def test_generate_uses_only_kept_flag(client, auth, tmp_path, monkeypatch):
    """生成也要走同一个口径 —— 否则预览显示 2 篇、实际却给模型 7 篇。"""
    calls: list[dict] = []
    _stub_model(monkeypatch, calls=calls)
    acc = add_account(client, auth)
    _seed_with_verdict(acc["id"], kept=1, dropped=3, tag="g")

    r = client.post("/api/weekly/generate", params=auth, json={
        "issue_num": 1, "from_date": "2026-09-05", "to_date": "2026-09-05",
        "selected_count": 5, "out_dir": str(tmp_path / "out")})
    assert r.status_code == 202, r.text
    assert r.json()["total"] == 1, "被筛掉的 3 篇不该进来"
    task = wait_task(r.json()["task_id"])
    assert task.status == "done", task.error
    assert task.result["total"] == 1

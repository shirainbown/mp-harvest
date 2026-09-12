"""周报路由契约测试（server/routes/weekly.py）。"""

from __future__ import annotations

import json
from pathlib import Path

from .conftest import add_account, wait_task


def _ts(y: int, m: int, d: int, h: int = 10) -> int:
    """本地时区的某时刻（避免手写 epoch 算错日期、把种子数据落在查询窗口外）。"""
    from datetime import datetime

    return int(datetime(y, m, d, h).timestamp())


def _seed_articles(account_id: str, n: int = 3, *, y: int = 2026, m: int = 9, d: int = 5) -> None:
    from mp_harvest.server import state

    base = _ts(y, m, d)
    state.set_articles(
        account_id,
        [
            {"title": f"文章{i}", "link": f"https://mp.weixin.qq.com/s/a{i}",
             "publish_ts": base + i * 3600, "publish_at": f"{y}-{m:02d}-{d:02d} 10:00",
             "identity": f"mid:{i}", "body_text": "正文内容足够长以便当作真实候选处理。",
             "body_html": f"<p>正文{i}</p>"}
            for i in range(n)
        ],
    )


def _stub_model(monkeypatch):
    from mp_harvest.core import ai_filter as af

    def fake_call(cfg, system, user, max_retries=3, **kw):
        if '"score"' in system:
            return json.dumps({"items": [{"idx": 0, "score": 8.0, "semiconductor": True,
                                          "title_cn": "译名", "domain": "AI芯片架构与推理优化",
                                          "business_tags": ["公共"], "reason": "理由"}]},
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

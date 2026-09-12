"""周报生成（core/weekly_report.py）单元测试。

重点钉住两件用户明确关心的事：
1. **模板可以随便改** —— 改什么输出就是什么；写错变量名要报出来而不是静默空白。
2. **提示词可以随便改** —— 改哪段只有哪段缓存失效，改回来还能命中。
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core import ai_filter as af  # noqa: E402
from mp_harvest.core import weekly_report as wr  # noqa: E402
from mp_harvest.core.ai_filter import ModelConfig  # noqa: E402


def _cfg(name: str = "m1") -> ModelConfig:
    return ModelConfig(id="m1", name=name, base_url="http://127.0.0.1:1",
                       api_key="k", model="x", enabled=True, format="openai")


def _cand(key: str, title: str, *, ts: int = 1754400000, kind: str = "公众号",
          text: str = "正文内容足够长以便被当作真实候选处理，包含技术细节。") -> dict:
    return {
        "key": key, "kind": kind, "title": title, "title_cn": "", "arxiv_id": "",
        # 真实候选都带 identity（补抓到正文后靠它写回文章缓存）
        "identity": f"mid:{key}", "source_id": "acct-1",
        "source": "测试号" if kind == "公众号" else "arXiv · A",
        "date": "2026-09-07", "publish_ts": ts, "publish_at": "2026-09-07 10:00",
        "url": f"https://example.com/{key}", "text": text,
        "body_html": "<p>正文</p>" if kind == "公众号" else "", "body_text": text,
    }


# ── 提示词 ────────────────────────────────────────────────────────


def test_default_prompts_all_carry_fixed_output():
    """四段默认提示词都必须带代码固定的输出约束 —— 否则解析必崩。"""
    for k in wr.PROMPT_KEYS:
        full = wr.build_prompt(k)
        assert "【输出格式（软件固定，不可更改）】" in full, k
        assert wr.DEFAULT_SCORING in wr.build_prompt("scoring")


def test_user_text_cannot_drop_output_constraint():
    """用户把提示词改得面目全非，固定约束仍在（这是不改崩解析的保证）。"""
    full = wr.build_prompt("scoring", "只看光刻相关，其他都丢")
    assert "只看光刻相关" in full
    assert '{"items"' in full and "只输出严格 JSON" in full


def test_prompt_fingerprint_changes_only_with_its_own_text():
    a = wr.prompt_fingerprint("scoring", "标准A")
    assert a == wr.prompt_fingerprint("scoring", "标准A")       # 同一文本稳定
    assert a != wr.prompt_fingerprint("scoring", "标准B")       # 改了变
    assert a != wr.prompt_fingerprint("detail", "标准A")        # 换一段也变


def test_prompts_roundtrip_and_defaults(tmp_path):
    p = tmp_path / "prompts.json"
    assert wr.load_prompts(p)["scoring"] == wr.DEFAULT_SCORING   # 文件不存在 → 默认
    wr.save_prompts(p, {"scoring": "我的标准", "detail": "", "intro": "x", "brief": "y"})
    got = wr.load_prompts(p)
    assert got["scoring"] == "我的标准"
    assert got["detail"] == wr.DEFAULT_DETAIL                    # 空值回落默认
    payload = wr.prompts_payload(p)
    assert payload["scoring"]["text"] == "我的标准"
    assert payload["scoring"]["default"] == wr.DEFAULT_SCORING
    assert payload["scoring"]["label"] == "选题打分"


def test_corrupt_prompts_file_falls_back(tmp_path):
    p = tmp_path / "prompts.json"
    p.write_text("{ 不是 json", encoding="utf-8")
    assert wr.load_prompts(p)["intro"] == wr.DEFAULT_INTRO


# ── JSON 解析容错 ─────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [
    '{"a":1}',
    '```json\n{"a":1}\n```',
    '好的，以下是结果：\n{"a":1}',
    '{"a":1}\n希望有帮助！',
    '```\n结果如下\n{"a":1}\n```',
])
def test_extract_json_tolerates_formats(raw):
    assert wr._extract_json(raw) == {"a": 1}


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        wr._extract_json("完全不是 JSON 的一段话")


def test_llm_json_retries_then_raises(monkeypatch):
    """解析失败要重试一次，且重试时**追加**「只输出 JSON」的强调（不是原样重发）。"""
    from mp_harvest.core import ai_filter as af

    seen: list[str] = []

    def fake_call(cfg, system, user, max_retries=3, **kw):
        seen.append(system)
        return "这不是 JSON"

    monkeypatch.setattr(af, "_call_model", fake_call)
    with pytest.raises(RuntimeError, match="无法解析"):
        wr.llm_json(_cfg(), "原始提示词", "user")

    assert len(seen) == 2, seen                     # 首次 + 重试一次
    assert seen[0] == "原始提示词"
    assert seen[1].startswith("原始提示词")           # 原文保留
    assert "只输出 JSON" in seen[1]                  # 追加了强调


def test_llm_json_second_attempt_can_succeed(monkeypatch):
    """第一次返回废话、第二次返回 JSON 时应当成功（不能一次失败就放弃）。"""
    from mp_harvest.core import ai_filter as af

    def fake_call(cfg, system, user, max_retries=3, **kw):
        return '{"ok":true}' if "只输出 JSON" in system else "好的，我来分析一下……"

    monkeypatch.setattr(af, "_call_model", fake_call)
    assert wr.llm_json(_cfg(), "sys", "u") == {"ok": True}


def test_llm_json_forwards_max_tokens_and_timeout(monkeypatch):
    """新增的 max_tokens/timeout 必须真的透传给传输层（报告级输出需要更大额度）。"""
    from mp_harvest.core import ai_filter as af

    seen = {}

    def fake_call(cfg, system, user, max_retries=3, **kw):
        seen.update(kw)
        return '{"ok":1}'

    monkeypatch.setattr(af, "_call_model", fake_call)
    assert wr.llm_json(_cfg(), "s", "u", max_tokens=9000, timeout=333) == {"ok": 1}
    assert seen == {"max_tokens": 9000, "timeout": 333}


# ── 缓存随提示词失效 ──────────────────────────────────────────────


def test_cache_key_changes_with_prompt_only_for_that_stage():
    k1 = wr.cache_key("scoring", "标准A", "art1")
    assert k1 == wr.cache_key("scoring", "标准A", "art1")
    assert k1 != wr.cache_key("scoring", "标准B", "art1")
    # 同一段提示词下，文章的键不同
    assert k1 != wr.cache_key("scoring", "标准A", "art2")
    # 打分缓存键与解读缓存键永不相等（即使提示词文本相同）
    assert wr.cache_key("scoring", "同样的字", "a") != wr.cache_key("detail", "同样的字", "a")


def test_weekly_cache_roundtrip_and_tolerance(tmp_path):
    c = wr.WeeklyCache(tmp_path / "cache.json")
    c.put("scores", "k", {"score": 8})
    assert c.get("scores", "k") == {"score": 8}
    assert c.get("scores", "不存在") is None
    # 落盘后可重新读出
    c2 = wr.WeeklyCache(tmp_path / "cache.json")
    assert c2.get("scores", "k") == {"score": 8}
    # 坏文件不抛异常
    (tmp_path / "bad.json").write_text("[[[", encoding="utf-8")
    assert wr.WeeklyCache(tmp_path / "bad.json").get("scores", "k") is None


def test_editing_scoring_prompt_reuses_detail_cache(monkeypatch, tmp_path):
    """改「打分」提示词 → 打分重算，但「深度解读」仍命中缓存（不重复花钱）。"""
    from mp_harvest.core import ai_filter as af

    calls: list[str] = []

    def fake_call(cfg, system, user, max_retries=3, **kw):
        if "打分" in system or "评分" in system or '"score"' in system:
            calls.append("scoring")
            return json.dumps({"items": [{"idx": 0, "score": 8, "semiconductor": True,
                                          "title_cn": "译名", "domain": wr.DOMAINS[0],
                                          "business_tags": ["公共"], "reason": "r"}]},
                              ensure_ascii=False)
        calls.append("detail")
        return json.dumps({"key_innovation": "k", "data_results": "d", "summary": "s"},
                          ensure_ascii=False)

    monkeypatch.setattr(af, "_call_model", fake_call)
    cache = wr.WeeklyCache(tmp_path / "c.json")
    cands = [_cand("a1", "标题")]

    prompts = dict(wr.load_prompts(tmp_path / "none.json"))
    wr.score_candidates(cands, [_cfg()], prompts=prompts, cache=cache, workers=1)
    wr.analyze_selected(cands, [_cfg()], prompts=prompts, cache=cache, workers=1)
    assert calls.count("scoring") == 1 and calls.count("detail") == 1

    # 只改打分标准 → 只有打分重算
    calls.clear()
    p2 = dict(prompts)
    p2["scoring"] = "只保留光刻相关的"
    wr.score_candidates(cands, [_cfg()], prompts=p2, cache=cache, workers=1)
    wr.analyze_selected(cands, [_cfg()], prompts=p2, cache=cache, workers=1)
    assert calls == ["scoring"], calls

    # 改回原文 → 打分又能命中最初那份缓存
    calls.clear()
    wr.score_candidates(cands, [_cfg()], prompts=prompts, cache=cache, workers=1)
    assert calls == [], calls


# ── 渲染：模板可以随便改 ──────────────────────────────────────────


def _tpl(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "t.html"
    p.write_text(body, encoding="utf-8")
    return p


def test_render_expands_loop_and_condition(tmp_path):
    """循环与条件都由模板说了算 —— 改 DOM 就改输出（旧脚本做不到这点）。"""
    t = _tpl(tmp_path, "{% for a in selected %}<h3>{{ a.title_cn }}</h3>"
                       "{% if a.title_en %}<sub>{{ a.title_en }}</sub>{% endif %}"
                       "{% endfor %}")
    ctx = {"selected": [
        {"title_cn": "中文甲", "title_en": "English A"},
        {"title_cn": "中文乙", "title_en": ""},
    ]}
    r = wr.render_report(ctx, template_path=t)
    assert r["ok"] is True
    assert "<h3>中文甲</h3><sub>English A</sub>" in r["html"]
    assert "<h3>中文乙</h3>" in r["html"]
    # 中文文章不出现 subtitle 块
    assert r["html"].count("<sub>") == 1


def test_template_change_changes_output(tmp_path):
    """同一份数据，改模板 → 输出随之变化（这是「模板是唯一真相」的直接验证）。"""
    ctx = {"selected": [{"title_cn": "标题", "title_en": ""}]}
    a = wr.render_report(ctx, template_path=_tpl(tmp_path, "{{ selected[0].title_cn }}"))
    b = wr.render_report(ctx, template_path=_tpl(tmp_path, "<b>{{ selected[0].title_cn }}</b> 加了字段"))
    assert a["html"] == "标题"
    assert b["html"] == "<b>标题</b> 加了字段"


def test_undefined_variable_is_reported_not_swallowed(tmp_path):
    """拼错变量名要报出来 —— 静默渲染成空白是最难查的一类模板错误。"""
    t = _tpl(tmp_path, "{{ selected[0].title_cn }} / {{ ARTILCE_NO }} / {{ typo_field }}")
    r = wr.render_report({"selected": [{"title_cn": "标题"}]}, template_path=t)
    assert r["ok"] is True
    assert set(r["missing"]) == {"ARTILCE_NO", "typo_field"}


def test_undefined_in_loop_body_is_reported(tmp_path):
    t = _tpl(tmp_path, "{% for a in selected %}{{ a.nope }}{% endfor %}")
    r = wr.render_report({"selected": [{"title_cn": "x"}]}, template_path=t)
    assert r["missing"] == ["nope"]


def test_syntax_error_reports_line(tmp_path):
    r = wr.render_report({}, template_path=_tpl(tmp_path, "<p>ok</p>\n<p>{{ unclosed </p>"))
    assert r["ok"] is False
    assert "第 2 行" in r["error"], r["error"]


def test_missing_template_reports_path(tmp_path):
    r = wr.render_report({}, template_path=tmp_path / "不存在.html")
    assert r["ok"] is False and "不存在" in r["error"]


def test_tag_spans_and_nl2br_filters(tmp_path):
    t = _tpl(tmp_path, "{{ tags | tag_spans | safe }}||{{ text | nl2br }}")
    r = wr.render_report({"tags": ["数通", "不存在的标签"], "text": "第一行\n第二行"}, template_path=t)
    assert wr.TAG_COLORS["数通"] in r["html"]
    assert "不存在的标签" not in r["html"]        # 未登记的标签直接略过
    assert "第一行<br>\n第二行" in r["html"]


def test_autoescape_protects_article_text(tmp_path):
    """文章标题/正文里的尖括号不能让模板注入 —— 默认 autoescape 必须开着。"""
    t = _tpl(tmp_path, "{{ selected[0].title_cn }}")
    r = wr.render_report({"selected": [{"title_cn": "<script>x</script>"}]}, template_path=t)
    assert "<script>" not in r["html"]


# ── 候选收集 ──────────────────────────────────────────────────────


def _wrow(identity: str, title: str, ts: int) -> dict:
    """state 文章缓存行（normalize_wechat 的输入形状）。"""
    return {"identity": identity, "title": title, "link": f"https://mp.weixin.qq.com/s/{identity}",
            "publish_ts": ts, "publish_at": "2026-09-07 10:00", "body_text": "正文"}


def _erow(item_key: str, title: str, ts: int) -> dict:
    """外部来源条目（normalize_external 的输入形状）。"""
    return {"item_key": item_key, "title": title, "url": f"https://arxiv.org/abs/{item_key}",
            "publish_ts": ts, "dir_date": "2026-09-07", "abstract": "摘要", "authors": ["A"]}


def test_collect_candidates_window_and_dedupe():
    w = [(_wrow("mid:1", "窗口内", 2000), "号A", "acct-A"),
         (_wrow("mid:2", "窗口外", 100), "号A", "acct-A")]
    e = [_erow("arxiv:9", "外部", 1500)]
    got = wr.collect_candidates(wechat_rows=w, external_items=e, start_ts=1000, end_ts=3000)
    assert {c["key"] for c in got} == {"wechat:mid:1", "ext:arxiv:9"}
    # 按发布时间倒序
    assert [c["key"] for c in got] == ["wechat:mid:1", "ext:arxiv:9"]
    # 同一篇重复出现只留一条
    dup = wr.collect_candidates(wechat_rows=[(_wrow("mid:1", "A", 10), "x", "acct-X"),
                                             (_wrow("mid:1", "B", 10), "y", "acct-Y")])
    assert len(dup) == 1


def test_candidates_carry_their_source_id():
    """候选要带着**来源 id** —— 前端按账号/按来源显示「本区间 N 篇」全靠它。

    公众号的缓存行里只有账号名，id 必须由调用方传进来；外部条目的 id 在行里。
    """
    got = wr.collect_candidates(
        wechat_rows=[(_wrow("mid:1", "甲", 2000), "号A", "acct-A")],
        external_items=[{**_erow("arxiv:9", "乙", 1500), "source_id": "src-1"}],
        start_ts=1000, end_ts=3000,
    )
    by_kind = {c["kind"]: c["source_id"] for c in got}
    assert by_kind == {"公众号": "acct-A", "外部": "src-1"}   # 无 arxiv_id → 归「外部」


def test_candidate_key_never_collides_without_identity_or_link():
    """缺 identity 又缺 link 时，键不能退化成同一个空串（多篇会撞成一条）。"""
    a = wr.normalize_wechat({"title": "甲", "link": "", "identity": ""})
    b = wr.normalize_wechat({"title": "乙", "link": "", "identity": ""})
    assert a["key"] and b["key"] and a["key"] != b["key"]
    c = wr.normalize_external({"title": "丙", "url": "", "item_key": ""})
    d = wr.normalize_external({"title": "丁", "url": "", "item_key": ""})
    assert c["key"] != d["key"]


def test_normalize_drops_rows_without_title_and_link():
    assert wr.normalize_wechat({"title": "", "link": ""}) is None
    assert wr.normalize_external({"title": "", "url": ""}) is None


def test_normalize_external_reads_body_file(tmp_path):
    """外部条目正文优先读本地正文文件（内容是摘要），取不到再退数据库字段。"""
    body = tmp_path / "a.html"
    body.write_text("<html><body><div class='abs'>文件里的正文</div></body></html>", encoding="utf-8")
    item = {"item_key": "arxiv:1", "title": "T", "url": "u", "arxiv_id": "1",
            "body_path": str(body), "abstract": "数据库里的摘要"}
    assert "文件里的正文" in wr.normalize_external(item)["text"]
    # body_path 指向不存在的文件 → 退回 abstract
    item["body_path"] = str(tmp_path / "没有.html")
    assert wr.normalize_external(item)["text"] == "数据库里的摘要"


# ── 归档 ──────────────────────────────────────────────────────────


def test_archive_index_links_resolve(tmp_path):
    """归档目录页就在 articles/ 里，链接必须相对它自己 —— 写成相对 root 会 404。"""
    import re as _re

    root = tmp_path / "第1期_2026-09-07"
    cands = [_cand("a1", "文章一"), _cand("a2", "文章二")]
    art = wr.archive_articles(cands, root)
    assert art["archived"] == 2 and art["errors"] == []
    wr.write_report_outputs(
        root, report_html="<html>x</html>", context={}, scores={}, details={}, briefs={},
        index_rows=art["index_rows"], issue_num=1,
    )

    index = (root / "articles" / "index.html").read_text(encoding="utf-8")
    hrefs = [h for h in _re.findall(r'href="([^"#]+)"', index) if h.endswith(".html")]
    assert hrefs, "目录页应当有文章链接"
    for h in hrefs:
        assert (root / "articles" / h).is_file(), f"目录页链接指向不存在的文件：{h}"
    # rel_map 是相对 root 的（供周报正文链接）
    assert art["rel_map"]["a1"].startswith("articles/")


def test_archive_article_failure_does_not_block(tmp_path, monkeypatch):
    """单篇归档抛异常时记一笔继续，不能把整期带崩。"""
    from mp_harvest.core import article_reader as ar

    real = ar.write_article_export

    def flaky(path, art, **kw):
        if "坏条目" in str(art.get("title") or ""):
            raise RuntimeError("模拟写入失败")
        return real(path, art, **kw)

    monkeypatch.setattr(ar, "write_article_export", flaky)
    res = wr.archive_articles([_cand("bad", "坏条目"), _cand("ok", "好条目")], tmp_path / "issue")
    assert res["archived"] == 1
    assert len(res["errors"]) == 1 and "坏条目" in res["errors"][0]


def test_archive_accepts_article_without_link(tmp_path):
    """没有链接但有正文的条目照样归档 —— 归档用的是本地正文，不需要联网。"""
    c = _cand("nolink", "无链接但有正文")
    c["url"] = ""
    res = wr.archive_articles([c], tmp_path / "issue")
    assert res["archived"] == 1 and res["errors"] == []
    assert not res["index_rows"][0]["link"]


def test_archive_uses_arxiv_id_as_filename(tmp_path):
    c = _cand("ext:arxiv:2609.1", "Some English Paper", kind="arXiv")
    c["arxiv_id"] = "2609.12345v1"
    res = wr.archive_articles([c], tmp_path / "issue")
    assert (tmp_path / "issue" / "articles" / "2609.12345v1.html").is_file()
    assert res["archived"] == 1


def test_write_report_outputs_three_pieces(tmp_path):
    root = tmp_path / "第3期_2026-09-07"
    (root / "articles").mkdir(parents=True)
    out = wr.write_report_outputs(
        root, report_html="<html>周报</html>", context={"issue": {"num": 3}},
        scores={"a": 1}, details={"a": 2}, briefs={"a": "b"},
        index_rows=[{"title": "T", "publish_at": "2026-09-01", "publish_ts": 1,
                     "account": "号", "file": "x.html", "link": "u", "keep": True, "reason": "r"}],
        issue_num=3,
    )
    assert out["ok"] is True
    assert Path(out["report_path"]).is_file()
    assert (root / "data" / "report.json").is_file()
    assert (root / "data" / "scores.json").is_file()
    assert "第3期" in (root / "articles" / "index.html").read_text(encoding="utf-8")
    assert wr.load_saved_context(root) == {"issue": {"num": 3}}


# ── 期号 ──────────────────────────────────────────────────────────


def test_suggest_issue_number(tmp_path):
    assert wr.suggest_issue_number(tmp_path) == 1
    for n in ("第15期_2026-08-24", "第16期_2026-08-31", "随手建的目录"):
        (tmp_path / n).mkdir()
    assert wr.suggest_issue_number(tmp_path) == 17
    assert wr.suggest_issue_number(tmp_path / "不存在") == 1


# ── 整期端到端（打桩模型）─────────────────────────────────────────


def _fake_scoring_reply(user: str, *, score: float = 8.5) -> str:
    """按 user message 里的【第 N 篇】**逐篇回显**记录 —— 批处理测试的前提。

    早先这里一律只回 ``idx: 0`` 一条：批模式下只有第 1 篇能对上，其余全走
    补漏/兜底，测试却照样绿 —— 「结果型断言对批处理不敏感」的典型。回显之后，
    断言才真的在检验「每篇都拿到了自己的记录」。
    """
    n = len(re.findall(r"【第 \d+ 篇】", user))
    # 分数按批内位置递减：既能验证「每篇拿到自己那份」，也保证选题顺序确定
    return json.dumps({"items": [
        {"idx": i, "score": score - i * 0.1, "semiconductor": True,
         "title_cn": "译名", "domain": wr.DOMAINS[0],
         "business_tags": ["数通", "公共"], "reason": "关键数据支撑的入选理由"}
        for i in range(max(1, n))
    ]}, ensure_ascii=False)


def _stub_model(monkeypatch, *, n_candidates: int = 3):
    from mp_harvest.core import ai_filter as af

    def fake_call(cfg, system, user, max_retries=3, **kw):
        if '"score"' in system:
            return _fake_scoring_reply(user)
        if '"key_innovation"' in system:
            return json.dumps({"key_innovation": "创新点；第二点",
                               "data_results": "3x: 加速比",
                               "summary": "背景方法结果意义"}, ensure_ascii=False)
        if '"brief"' in system:
            return json.dumps({"items": [{"idx": 0, "brief": "一句话摘要"}]}, ensure_ascii=False)
        return "本期（2026-09-01 至 2026-09-07）精选 3 篇。\n① 看点一\n② 看点二"

    monkeypatch.setattr(af, "_call_model", fake_call)


def test_generate_issue_end_to_end(monkeypatch, tmp_path):
    _stub_model(monkeypatch)
    cands = [_cand(f"a{i}", f"文章{i}", ts=1754400000 + i) for i in range(3)]
    tpl = _tpl(tmp_path, "第{{ issue.num }}期 共{{ stats.total }}篇｜"
                         "{% for a in selected %}[{{ a.no }}]{{ a.title_cn }}{% endfor %}"
                         "｜{% for a in others %}({{ a.no }}){{ a.brief }}{% endfor %}"
                         "｜{{ intro | safe }}")

    res = wr.generate_issue(
        candidates=cands, models=[_cfg()], prompts=wr.load_prompts(tmp_path / "none.json"),
        cache=wr.WeeklyCache(tmp_path / "c.json"), out_dir=tmp_path / "out",
        issue_num=7, from_date="2026-09-01", to_date="2026-09-07",
        selected_count=2, template_path=tpl, workers=1,
        org={"name": "某某科技", "email": "a@b.c", "archive_url": "https://x"},
    )
    assert res["ok"] is True, res
    assert res["selected"] == 2 and res["others"] == 1
    assert res["missing_vars"] == []

    root = Path(res["issue_dir"])
    html = Path(res["report_path"]).read_text(encoding="utf-8")
    assert "第7期" in html and "共3篇" in html
    assert "[1]译名" in html and "[2]译名" in html
    assert "(3)一句话摘要" in html          # 其他入选的序号从 3 续，不从 1 重来
    assert "① 看点一" in html

    # 三件套齐全
    assert (root / "articles" / "index.html").is_file()
    assert (root / "data" / "report.json").is_file()
    assert (root / "data" / "scores.json").is_file()
    assert res["archived"] == 3


def test_generate_issue_reports_missing_template_vars(monkeypatch, tmp_path):
    """整期跑完时，模板里的拼写错误要一路冒到结果里（用户改模板的主要反馈渠道）。"""
    _stub_model(monkeypatch)
    tpl = _tpl(tmp_path, "{{ issue.num }} {{ ARTILCE_NO }}")
    res = wr.generate_issue(
        candidates=[_cand("a1", "文章")], models=[_cfg()],
        prompts=wr.load_prompts(tmp_path / "none.json"),
        cache=wr.WeeklyCache(tmp_path / "c.json"), out_dir=tmp_path / "out",
        issue_num=1, from_date="a", to_date="b", selected_count=1,
        template_path=tpl, workers=1,
    )
    assert res["ok"] is True
    assert res["missing_vars"] == ["ARTILCE_NO"]


def test_generate_issue_survives_broken_template_without_losing_ai_work(monkeypatch, tmp_path):
    """模板写坏时如实报错，但已花的 AI 结果不能白费 —— 上下文要带回来。"""
    _stub_model(monkeypatch)
    tpl = _tpl(tmp_path, "{% for a in selected %}没闭合")
    res = wr.generate_issue(
        candidates=[_cand("a1", "文章")], models=[_cfg()],
        prompts=wr.load_prompts(tmp_path / "none.json"),
        cache=wr.WeeklyCache(tmp_path / "c.json"), out_dir=tmp_path / "out",
        issue_num=1, from_date="a", to_date="b", selected_count=1,
        template_path=tpl, workers=1,
    )
    assert res["ok"] is False
    assert "行" in res["error"]
    assert res["context"]["selected"][0]["title_cn"] == "译名"   # AI 结果保住了


def test_generate_issue_all_irrelevant_returns_error(monkeypatch, tmp_path):
    from mp_harvest.core import ai_filter as af

    monkeypatch.setattr(af, "_call_model", lambda *a, **k: json.dumps(
        {"items": [{"idx": 0, "score": 1, "semiconductor": False, "title_cn": "x",
                    "domain": wr.DOMAINS[0], "business_tags": ["公共"], "reason": "r"}]},
        ensure_ascii=False))
    res = wr.generate_issue(
        candidates=[_cand("a1", "纯软件文章")], models=[_cfg()],
        prompts=wr.load_prompts(tmp_path / "none.json"),
        cache=wr.WeeklyCache(tmp_path / "c.json"), out_dir=tmp_path / "out",
        issue_num=1, from_date="a", to_date="b", workers=1,
    )
    assert res["ok"] is False and "相关性" in res["error"]


def test_generate_issue_requires_a_model(tmp_path):
    with pytest.raises(RuntimeError, match="AI 模型"):
        wr.score_candidates([_cand("a1", "x")], [], prompts={}, cache=wr.WeeklyCache(tmp_path / "c.json"))


# ── 内置模板 ──────────────────────────────────────────────────────


def _full_ctx(**over) -> dict:
    a = {"no": 1, "title": "English A", "title_cn": "中文甲", "title_en": "English A",
         "source": "arXiv · 张三", "kind": "arXiv", "date": "2026-09-05",
         "url": "https://arxiv.org/abs/1", "domain": wr.DOMAINS[0],
         "business_tags": ["数通", "公共"], "reason": "关键数据支撑",
         "key_innovation": "创新点", "data_results": "3x: 加速比", "summary": "摘要",
         "brief": "", "score": 8.5, "local_file": "articles/a1.html"}
    b = dict(a, no=2, title="中文乙", title_cn="中文乙", title_en="", kind="公众号",
             source="半导体行业观察", business_tags=["公共"], local_file="")
    c = dict(a, no=3, title="English C", title_cn="中文丙", title_en="English C",
             brief="一句话摘要", business_tags=["接入"], local_file="articles/a3.html")
    ctx = {
        "issue": {"num": 17, "from_date": "2026-08-31", "to_date": "2026-09-06",
                  "generated_at": "2026-09-07 01:20", "title": "逻辑芯片行业洞察快报"},
        "stats": {"wechat": 12, "arxiv": 30, "total": 42,
                  "selected_count": 2, "other_count": 1},
        "intro": "本期精选 2 篇。<br>① 看点一<br>② 看点二",
        "selected": [a, b], "others": [c], "tags": wr.TAG_COLORS,
        "org_name": "某某科技", "org_email": "a@b.c",
        "archive_url": "https://example.com/archive",
    }
    ctx.update(over)
    return ctx


def test_builtin_template_renders_without_undefined_vars():
    """内置模板必须能完整渲染，且不引用任何未提供的变量。"""
    r = wr.render_report(_full_ctx())
    assert r["ok"] is True, r["error"]
    assert r["missing"] == [], r["missing"]


def test_builtin_template_output_has_no_leftover_jinja():
    """输出里不能残留模板语法（说明注释块也不能漏进正文）。"""
    h = wr.render_report(_full_ctx())["html"]
    assert "{{" not in h and "{%" not in h and "{#" not in h
    assert "Jinja2）使用说明" not in h


def test_builtin_template_chinese_article_has_no_subtitle():
    """中文文章不加英文副标题（模板用 {% if title_en %} 自动区分）。"""
    h = wr.render_report(_full_ctx())["html"]
    assert '<span class="subtitle">English A</span>' in h      # 英文论文有
    tail = h.split("中文乙")[1][:300]
    assert '<span class="subtitle">' not in tail               # 中文文章没有


def test_builtin_template_optional_blocks_disappear():
    """archive_url 为空、others 为空时，对应板块整块消失（不是留个空标题）。"""
    h = wr.render_report(_full_ctx(archive_url="", others=[]))["html"]
    assert "点击查阅历史快报" not in h
    assert "板块三" not in h


def test_builtin_template_tags_are_colored():
    h = wr.render_report(_full_ctx())["html"]
    for tag in ("数通", "公共", "接入"):
        assert wr.TAG_COLORS[tag] in h, tag


def test_builtin_template_escapes_article_text():
    """文章标题里的尖括号必须被转义（autoescape 生效），不能注入脚本。"""
    ctx = _full_ctx()
    ctx["selected"][0]["title_cn"] = "<script>alert(1)</script>"
    h = wr.render_report(ctx)["html"]
    assert "<script>alert(1)</script>" not in h
    assert "&lt;script&gt;" in h


def test_builtin_template_has_no_local_archive_links():
    """内置模板**不渲染任何本地存档链接**（2026-09 用户要求改掉）。

    原话：「周报中不应该出现本地存档的标签，因为这个周报是会公开发布的，
    因此别人查看时看不到本地存档」。`articles/xxx.html` 是相对本机的路径，
    读者点开必是死链。

    变量本身仍留在上下文里（自定义模板想用可以用），只是内置模板不用它。
    """
    ctx = _full_ctx()
    h = wr.render_report(ctx)["html"]
    assert "本地存档" not in h
    assert 'href="articles/' not in h, "本地存档链接漏进了内置模板"
    # 但归档文件本身照旧产出（本机看得到），相对路径也仍然在上下文里
    assert ctx["selected"][0]["local_file"] == "articles/a1.html"


# ── 业务领域归属：标准要喂给模型，漏答要关键词兜底 ──────────────────
#
# 用户 2026-09-12 的原话：「AI 模型怎么知道哪些文章应该分类到正确的领域中？
# 我记得我在曾经给过分类的标准」。此前 DEFAULT_SCORING 只列了五个标签**名字**，
# 模型只能靠猜 —— 数通/传送/接入是邻接术语，必然漂移且不可复现。


@pytest.mark.parametrize("text,expect", [
    ("基于 DPU 的 RoCEv2 拥塞控制与集合通信优化", "数通"),
    ("硅光 CPO 模块中的 PAM4 SerDes 与相干光 DSP 设计", "传送"),
    ("XGS-PON OLT 与 TSN 时间敏感网络的 IEEE 1588 同步", "接入"),
    ("面向 FPGA 的 HLS 高层次综合与 RTL 形式验证", "公共"),
    ("EUV 光刻与原子层沉积 ALD 工艺中的 FinFET 器件", "芯片硬件"),
])
def test_infer_business_tags_picks_the_right_domain(text, expect):
    """关键词兜底必须把典型文本分到正确领域 —— 这是标准落到代码的那一半。"""
    assert wr.infer_business_tags(text)[0] == expect


def test_infer_business_tags_is_case_insensitive():
    """论文标题里 DPU/cpo 大小写混写很常见，匹配必须归一化。"""
    assert wr.infer_business_tags("a dpu-based smartnic")[0] == "数通"
    assert wr.infer_business_tags("硅光 cpo 模块")[0] == "传送"


def test_lone_short_acronym_is_not_enough_to_classify():
    """一个孤立的短缩写不足以定类 —— 实测 arXiv 语料里 `TAS` 撞上论文名 **Gen-TAS**。

    这类误报比「不分类」更糟：报告里会印出一个看着很确定的错标。
    交还给「公共」比报一个错标诚实。
    """
    assert wr.infer_business_tags("Gen-TAS: A Generative AI-Aided Framework") == []
    assert wr.infer_business_tags("silicon photonics cpo module") == []
    # 同一批缩写，凑够两个就能定类
    assert wr.infer_business_tags("XGS-PON OLT 部署") == ["接入"]


@pytest.mark.parametrize("text", [
    # `SI`（信号完整性）撞上 silicon：曾让「硅光 CPO」被误判成「公共」
    "silicon photonics cpo module",
    "high purity silicon substrate growth",
    # `EM`（电迁移）撞上 system
    "system level thermal analysis",
    # `PI`（电源完整性）撞上 pipeline
    "pipeline architecture for inference",
])
def test_short_ascii_keywords_do_not_match_inside_english_words(text):
    """关键词表是按中文语料写的，跑 arXiv 英文摘要时短缩写会误伤整词。

    这是修 `SI`∈silicon 时发现的真 bug：中文语料碰不到，英文摘要遍地都是。
    """
    assert wr.infer_business_tags(text) == []


def test_short_ascii_keywords_match_across_separators():
    """整词匹配不能紧到把 HBM3 / 800G / TSN交换机 这类真实写法漏掉。"""
    assert "数通" in wr.infer_business_tags("HBM3 与 CXL 内存扩展")
    assert "接入" in wr.infer_business_tags("TSN交换机与门控调度")
    assert "接入" in wr.infer_business_tags("XGS-PON OLT 部署")
    assert "数通" in wr.infer_business_tags("DPU-based smartnic")


def test_infer_business_tags_ranks_by_hit_count():
    """多个类目都命中时，按命中关键词数量排序，取最贴近的。"""
    # 传送 3 个（硅光/CPO/PAM4），数通 2 个（DPU/集合通信）
    assert wr.infer_business_tags("硅光 CPO PAM4 模块与 DPU 集合通信") == ["传送", "数通"]


def test_infer_business_tags_limits_to_three():
    tags = wr.infer_business_tags(
        "硅光 CPO PAM4 相干光 DWDM DPU RoCEv2 集合通信 PON OLT TSN 光刻 EUV ALD")
    assert len(tags) == 3
    assert len(set(tags)) == 3


def test_infer_business_tags_empty_without_match():
    """什么都没命中要返回空列表，由调用方决定兜底 —— 不在这里偷偷给默认值。"""
    assert wr.infer_business_tags("今天天气不错，适合出门散步") == []
    assert wr.infer_business_tags("") == []


def test_business_tag_keywords_cover_exactly_the_five_tags():
    """关键词表的键必须与 BUSINESS_TAGS 一一对应（不然排序会 KeyError）。"""
    assert set(wr.BUSINESS_TAG_KEYWORDS) == set(wr.BUSINESS_TAGS)
    assert all(words for words in wr.BUSINESS_TAG_KEYWORDS.values())


def test_scoring_prompt_carries_the_business_tag_standard(monkeypatch, tmp_path):
    """判定标准必须真的进到 system prompt —— 否则模型还是只能看名字猜。"""
    seen: list[str] = []

    def fake_call(cfg, system, user, max_retries=3, **kw):
        seen.append(system)
        return json.dumps({"items": [{"idx": 0, "score": 8, "semiconductor": True,
                                      "title_cn": "译名", "domain": wr.DOMAINS[0],
                                      "business_tags": ["数通"], "reason": "r"}]},
                          ensure_ascii=False)

    monkeypatch.setattr(af, "_call_model", fake_call)
    wr.score_candidates([_cand("a1", "标题")], [_cfg()],
                        prompts=dict(wr.load_prompts(tmp_path / "none.json")),
                        cache=wr.WeeklyCache(tmp_path / "c.json"), workers=1)

    assert seen, "打分阶段没有调用模型"
    sys_prompt = seen[0]
    # 五个标签的场景定义都要在（光看名字模型分不清数通/传送/接入）
    for kw in ("数据中心", "硅光", "PON", "FPGA", "光刻"):
        assert kw in sys_prompt, kw
    assert "业务领域判定标准" in sys_prompt


def test_scoring_falls_back_to_keywords_when_model_omits_tags(monkeypatch, tmp_path):
    """模型没给 business_tags 时，用关键词兜底 —— 绝不能静默退化成「公共」。

    这是修复的核心断言：标题明明是光刻工艺，旧代码会给「公共」。
    """
    def fake_call(cfg, system, user, max_retries=3, **kw):
        return json.dumps({"items": [{"idx": 0, "score": 8, "semiconductor": True,
                                      "title_cn": "译名", "domain": wr.DOMAINS[0],
                                      "reason": "r"}]}, ensure_ascii=False)   # 漏了 business_tags

    monkeypatch.setattr(af, "_call_model", fake_call)
    got, _ = wr.score_candidates(
        [_cand("a1", "EUV 光刻与原子层沉积 ALD 工艺中的 FinFET 器件")], [_cfg()],
        prompts=dict(wr.load_prompts(tmp_path / "none.json")),
        cache=wr.WeeklyCache(tmp_path / "c.json"), workers=1)
    assert got["a1"]["business_tags"] == ["芯片硬件"]


def test_scoring_falls_back_when_model_returns_bogus_tags(monkeypatch, tmp_path):
    """模型自造标签（不在五类里）等同漏答，走同样的兜底路径。"""
    def fake_call(cfg, system, user, max_retries=3, **kw):
        return json.dumps({"items": [{"idx": 0, "score": 8, "semiconductor": True,
                                      "title_cn": "译名", "domain": wr.DOMAINS[0],
                                      "business_tags": ["光通信", "半导体"], "reason": "r"}]},
                          ensure_ascii=False)

    monkeypatch.setattr(af, "_call_model", fake_call)
    got, _ = wr.score_candidates(
        [_cand("a1", "XGS-PON OLT 与 TSN 时间敏感网络")], [_cfg()],
        prompts=dict(wr.load_prompts(tmp_path / "none.json")),
        cache=wr.WeeklyCache(tmp_path / "c.json"), workers=1)
    assert got["a1"]["business_tags"] == ["接入"]


def test_scoring_prefers_valid_model_tags_over_keywords(monkeypatch, tmp_path):
    """模型给了合法标签就听模型的 —— 它读过正文，比关键词匹配更准。"""
    def fake_call(cfg, system, user, max_retries=3, **kw):
        return json.dumps({"items": [{"idx": 0, "score": 8, "semiconductor": True,
                                      "title_cn": "译名", "domain": wr.DOMAINS[0],
                                      "business_tags": ["传送", "公共"], "reason": "r"}]},
                          ensure_ascii=False)

    monkeypatch.setattr(af, "_call_model", fake_call)
    # 标题命中「光刻」（芯片硬件），但模型说是「传送」—— 以模型为准
    got, _ = wr.score_candidates(
        [_cand("a1", "EUV 光刻工艺综述")], [_cfg()],
        prompts=dict(wr.load_prompts(tmp_path / "none.json")),
        cache=wr.WeeklyCache(tmp_path / "c.json"), workers=1)
    assert got["a1"]["business_tags"] == ["传送", "公共"]


def test_scoring_public_is_last_resort_only(monkeypatch, tmp_path):
    """关键词也全军覆没时才落到「公共」（跨领域通用），且确实只在这一种情况下发生。"""
    def fake_call(cfg, system, user, max_retries=3, **kw):
        return json.dumps({"items": [{"idx": 0, "score": 5, "semiconductor": True,
                                      "title_cn": "译名", "domain": wr.DOMAINS[0],
                                      "reason": "r"}]}, ensure_ascii=False)

    monkeypatch.setattr(af, "_call_model", fake_call)
    got, _ = wr.score_candidates(
        [_cand("a1", "行业周度观察与随笔", text="本周的一些零散想法。")], [_cfg()],
        prompts=dict(wr.load_prompts(tmp_path / "none.json")),
        cache=wr.WeeklyCache(tmp_path / "c.json"), workers=1)
    assert got["a1"]["business_tags"] == ["公共"]


def test_llm_json_retries_on_missing_field_then_returns_good_data(monkeypatch):
    """语法对但漏字段 → 带话重试；重试给了合格答案就用它，不走关键词。"""
    replies = iter([
        json.dumps({"items": [{"score": 8, "title_cn": "t", "domain": wr.DOMAINS[0],
                               "reason": "r"}]}, ensure_ascii=False),      # 漏 business_tags
        json.dumps({"items": [{"score": 8, "title_cn": "t", "domain": wr.DOMAINS[0],
                               "business_tags": ["传送"], "reason": "r"}]}, ensure_ascii=False),
    ])
    prompts: list[str] = []

    def fake_call(cfg, system, user, max_retries=3, **kw):
        prompts.append(system)
        return next(replies)

    monkeypatch.setattr(af, "_call_model", fake_call)
    data = wr.llm_json(_cfg(), "SYS", "USER",
                       validate=lambda d: None if wr.valid_business_tags(d["items"][0]) else "缺标签")
    assert data["items"][0]["business_tags"] == ["传送"]
    assert len(prompts) == 2
    assert "缺标签" in prompts[1]        # 重试提示里带上了具体原因


def test_llm_json_returns_incomplete_data_instead_of_raising(monkeypatch):
    """重试后仍不合格 → 返回那份数据交给调用方兜底，别把整篇丢掉。"""
    def fake_call(cfg, system, user, max_retries=3, **kw):
        return json.dumps({"items": [{"score": 7, "reason": "r"}]}, ensure_ascii=False)

    monkeypatch.setattr(af, "_call_model", fake_call)
    data = wr.llm_json(_cfg(), "SYS", "USER",
                       validate=lambda d: None if wr.valid_business_tags(d["items"][0]) else "缺标签")
    assert data["items"][0]["score"] == 7      # 数据还在


def test_scoring_retry_beats_keyword_fallback(monkeypatch, tmp_path):
    """模型第一次漏标签、重试补上 → 用模型答案，**不**用关键词兜底。

    标题命中「光刻」（芯片硬件），但模型重试后说是「接入」—— 听模型的，
    它读的是正文，关键词表只看了标题。
    """
    replies = iter([
        json.dumps({"items": [{"idx": 0, "score": 8, "semiconductor": True, "title_cn": "译名",
                               "domain": wr.DOMAINS[0], "reason": "r"}]}, ensure_ascii=False),
        json.dumps({"items": [{"idx": 0, "score": 8, "semiconductor": True, "title_cn": "译名",
                               "domain": wr.DOMAINS[0], "business_tags": ["接入"],
                               "reason": "r"}]}, ensure_ascii=False),
    ])
    monkeypatch.setattr(af, "_call_model", lambda *a, **kw: next(replies))
    got, _ = wr.score_candidates(
        [_cand("a1", "EUV 光刻与原子层沉积 ALD 工艺")], [_cfg()],
        prompts=dict(wr.load_prompts(tmp_path / "none.json")),
        cache=wr.WeeklyCache(tmp_path / "c.json"), workers=1)
    assert got["a1"]["business_tags"] == ["接入"]


def test_scoring_keeps_article_when_model_never_gives_tags(monkeypatch, tmp_path):
    """重试后还是不给标签 → 关键词兜底，且**不丢这篇**（打分结果照样产出）。"""
    calls = []

    def fake_call(cfg, system, user, max_retries=3, **kw):
        calls.append(1)
        return json.dumps({"items": [{"idx": 0, "score": 6, "semiconductor": True, "title_cn": "译名",
                                      "domain": wr.DOMAINS[0], "reason": "r"}]}, ensure_ascii=False)

    monkeypatch.setattr(af, "_call_model", fake_call)
    got, errors = wr.score_candidates(
        [_cand("a1", "XGS-PON OLT 与 TSN 时间敏感网络")], [_cfg()],
        prompts=dict(wr.load_prompts(tmp_path / "none.json")),
        cache=wr.WeeklyCache(tmp_path / "c.json"), workers=1)
    assert not errors, errors
    assert got["a1"]["business_tags"] == ["接入"]     # 关键词兜底生效
    assert got["a1"]["score"] == 6                   # 这篇没被丢掉
    assert len(calls) == 2                           # 重试过一次


def test_scoring_sends_enough_context_to_classify(monkeypatch, tmp_path):
    """打分阶段必须给足正文 —— 领域信号大量落在前 700 字之后。

    实测 arXiv 语料：摘要中位数 1474 字，30% 的领域关键词在 700 字之后，
    56 篇里 10 篇因此判不出领域。窗口被改窄会静默丢分类准确度，所以钉住。
    """
    seen: list[str] = []

    def fake_call(cfg, system, user, max_retries=3, **kw):
        seen.append(user)
        return json.dumps({"items": [{"idx": 0, "score": 8, "semiconductor": True,
                                      "title_cn": "译名", "domain": wr.DOMAINS[0],
                                      "business_tags": ["公共"], "reason": "r"}]},
                          ensure_ascii=False)

    monkeypatch.setattr(af, "_call_model", fake_call)
    # 领域关键词只在第 1500 字附近出现
    body = "开场白。" * 300 + "本文提出一种硅光 CPO 封装方案。" + "后文。" * 300
    wr.score_candidates([_cand("a1", "标题", text=body)], [_cfg()],
                        prompts=dict(wr.load_prompts(tmp_path / "none.json")),
                        cache=wr.WeeklyCache(tmp_path / "c.json"), workers=1)

    assert seen, "打分阶段没有调用模型"
    assert "硅光 CPO 封装方案" in seen[0], "正文被截断，模型看不到第 1500 字的领域信号"
    assert wr.SCORING_TEXT_CHARS >= 2000


# ── 打分批处理（2026-09 提速：每批 N 篇 + 可配置并发）────────────────
#
# ⚠️ 批处理对**结果型断言几乎不敏感** —— 把实现悄悄改回逐篇，「每篇都拿到自己的
# 记录」这类断言照样全绿。所以下面一律断言**请求的次数与形状**（录制桩），
# 每条都做过变异验证。

_OK = json.dumps({"ok": True}, ensure_ascii=False)


def _recording(monkeypatch, reply):
    """装一个录制桩，记下每次调用的 (system, user, model 名)。

    ``reply`` 传字符串则固定返回，传函数则按 ``(system, user)`` 现算。
    """
    calls: list[dict] = []

    def fake_call(cfg, system, user, max_retries=3, **kw):
        calls.append({"system": system, "user": user, "model": cfg.name})
        return reply(system, user) if callable(reply) else reply

    monkeypatch.setattr(af, "_call_model", fake_call)
    return calls


def _positions(user: str) -> list[int]:
    """user message 里的【第 N 篇】编号 —— 断言批的切法与编号用。"""
    return [int(m) for m in re.findall(r"【第 (\d+) 篇】", user)]


def _echo_by_pos(user: str, *, tags: tuple = ("数通",)) -> str:
    """按编号回显，每篇带一个能区分身份的 title_cn（译名0 / 译名1 …）。"""
    return json.dumps({"items": [
        {"idx": i, "score": 8.0 - i * 0.1, "semiconductor": True,
         "title_cn": f"译名{i}", "domain": wr.DOMAINS[0],
         "business_tags": list(tags), "reason": "r"}
        for i in _positions(user)]}, ensure_ascii=False)


def _echo_by_title(user: str) -> str:
    """按输入里各篇的**标题**回显 —— 与编号/批大小无关，用于等价性对比。"""
    items = []
    for i, block in enumerate(user.split("【第 ")[1:]):
        m = re.search(r"标题: (.+)", block)
        items.append({"idx": i, "score": 8.0, "semiconductor": True,
                      "title_cn": (m.group(1).strip() if m else "?"),
                      "domain": wr.DOMAINS[0], "business_tags": ["数通"], "reason": "r"})
    return json.dumps({"items": items}, ensure_ascii=False)


def _cands(n: int, *, ts0: int = 1754400000) -> list[dict]:
    return [_cand(f"a{i}", f"文章{i}", ts=ts0 + i) for i in range(n)]


def _score(cands, tmp_path, *, batch=8, workers=1, cache=None, on_progress=None):
    return wr.score_candidates(
        cands, [_cfg()],
        prompts=dict(wr.load_prompts(tmp_path / "none.json")),
        cache=cache or wr.WeeklyCache(tmp_path / "c.json"),
        workers=workers, batch_size=batch, on_progress=on_progress)


def test_scoring_sends_one_request_per_batch(monkeypatch, tmp_path):
    """9 篇 + 每批 4 → **恰好 3 次请求**，每批编号从 0 连续。"""
    calls = _recording(monkeypatch, lambda s, u: _echo_by_pos(u))
    _score(_cands(9), tmp_path, batch=4)

    assert len(calls) == 3, f"期望 3 次请求（9 篇 / 每批 4），实际 {len(calls)}"
    assert [_positions(c["user"]) for c in calls] == [
        [0, 1, 2, 3], [0, 1, 2, 3], [0],
    ], "批的切法或编号不对"


def test_scoring_batch_maps_idx_back_to_articles(monkeypatch, tmp_path):
    """**只看编号、不看数组顺序** —— 模型打乱顺序也不能错位。"""
    def reply(system, user):
        rows = [{"idx": i, "score": 5.0 + i, "semiconductor": True, "title_cn": f"译名{i}",
                 "domain": wr.DOMAINS[0], "business_tags": ["数通"], "reason": "r"}
                for i in _positions(user)]
        return json.dumps({"items": list(reversed(rows))}, ensure_ascii=False)

    _recording(monkeypatch, reply)
    got, _ = _score(_cands(4), tmp_path, batch=4)
    assert [got[f"a{i}"]["title_cn"] for i in range(4)] == ["译名0", "译名1", "译名2", "译名3"]
    assert [got[f"a{i}"]["score"] for i in range(4)] == [5.0, 6.0, 7.0, 8.0]


def test_scoring_ignores_extra_and_invalid_idx(monkeypatch, tmp_path):
    """越界 / 非法 / 重复编号都不能让解析炸掉或产生多余篇目。"""
    def reply(system, user):
        return json.dumps({"items": [
            {"idx": 0, "score": 7, "semiconductor": True, "title_cn": "第一次",
             "domain": wr.DOMAINS[0], "business_tags": ["数通"], "reason": "r"},
            {"idx": 99, "score": 9, "semiconductor": True, "title_cn": "越界",
             "domain": wr.DOMAINS[0], "business_tags": ["数通"], "reason": "r"},
            {"idx": "abc", "score": 9, "title_cn": "非法", "business_tags": ["数通"]},
            {"idx": True, "score": 9, "title_cn": "布尔", "business_tags": ["数通"]},
            {"idx": 0, "score": 6, "semiconductor": True, "title_cn": "第二次",
             "domain": wr.DOMAINS[0], "business_tags": ["数通"], "reason": "r"},
        ]}, ensure_ascii=False)

    _recording(monkeypatch, reply)
    got, _ = _score(_cands(1), tmp_path, batch=1)
    assert len(got) == 1, "越界/非法编号不该凭空多出篇目"
    assert got["a0"]["title_cn"] == "第二次", "重复编号应当后者覆盖（与 ai_filter 一致）"
    assert got["a0"]["score"] == 6


def test_scoring_partial_batch_only_reasks_missing(monkeypatch, tmp_path):
    """漏答时**只补那几篇**（重新编号），绝不整批重发。"""
    def reply(system, user):
        if "补充打分" in user:
            return _echo_by_pos(user)         # 补漏轮正常作答
        pos = _positions(user)[:1]            # 首轮只答第 0 篇
        return json.dumps({"items": [
            {"idx": i, "score": 8, "semiconductor": True, "title_cn": f"译名{i}",
             "domain": wr.DOMAINS[0], "business_tags": ["数通"], "reason": "r"}
            for i in pos]}, ensure_ascii=False)

    calls = _recording(monkeypatch, reply)
    got, errors = _score(_cands(4), tmp_path, batch=4)

    assert len(got) == 4 and not [e for e in errors if "没有给出" in e], errors
    assert len(calls) == 2, f"补漏应当只多一次请求，实际 {len(calls)} 次"
    assert _positions(calls[1]["user"]) == [0, 1, 2], "补漏轮必须只含漏掉的三篇且重新编号"


def test_scoring_missing_item_is_not_cached(monkeypatch, tmp_path):
    """**漏答的篇目绝不写缓存** —— 否则一次网络抖动会把文章永久拉黑。"""
    def reply(system, user):
        # 按**标题**跳过「文章1」：补漏轮会重新编号，按位置跳会被绕过
        rows = []
        for i, block in enumerate(user.split("【第 ")[1:]):
            if "标题: 文章1" in block:
                continue
            rows.append({"idx": i, "score": 8, "semiconductor": True, "title_cn": f"译名{i}",
                         "domain": wr.DOMAINS[0], "business_tags": ["数通"], "reason": "r"})
        return json.dumps({"items": rows}, ensure_ascii=False)

    _recording(monkeypatch, reply)
    cache = wr.WeeklyCache(tmp_path / "c.json")
    got, errors = _score(_cands(3), tmp_path, batch=3, cache=cache)

    assert "a1" not in got
    assert cache.get("scores", wr.cache_key("scoring", None, "a1")) is None, "漏答被写进了缓存"
    assert cache.get("scores", wr.cache_key("scoring", None, "a0")) is not None
    assert any("没有给出" in e for e in errors), errors


def test_scoring_empty_items_reply_is_not_cached(monkeypatch, tmp_path):
    """`{"items":[]}` / 无关对象**不算记录** —— 这是改造前的一个隐性 bug。

    旧代码 `_pick_item` 会把整个信封当记录返回，于是产出 `score=0.0` 的一条
    **并写进缓存**；批处理会把它放大成「模型漏答 N 篇 = N 条永久 0 分」。
    """
    cache = wr.WeeklyCache(tmp_path / "c.json")
    for payload in ('{"items": []}', '{"foo": 1}'):
        _recording(monkeypatch, payload)
        got, errors = _score(_cands(2), tmp_path, batch=2, cache=cache)
        assert got == {}, f"{payload} 被当成了有效记录"
        assert len(errors) == 2, errors


def test_scoring_batch_transport_failure_writes_nothing(monkeypatch, tmp_path):
    """传输失败：该批零缓存、逐篇记 error，**不降级**（拿 N 倍请求打坏端点只会更糟）。"""
    def reply(system, user):
        if len(_positions(user)) > 1:
            raise af.ModelCallError("模型「m1」调用失败: key 无效")
        return _echo_by_pos(user)

    calls = _recording(monkeypatch, reply)
    cache = wr.WeeklyCache(tmp_path / "c.json")
    got, errors = _score(_cands(3), tmp_path, batch=3, cache=cache)

    assert got == {}
    assert len(errors) == 3, "该批三篇都要记一笔"
    assert len(calls) == 1, "传输失败**不许**降级逐篇再打三次"
    assert cache.get("scores", wr.cache_key("scoring", None, "a0")) is None


def test_scoring_batch_parse_failure_degrades_to_single(monkeypatch, tmp_path):
    """整批解析不出来 → 降级逐篇（输出被截断是最常见的成因，逐篇把预算还给每篇）。"""
    def reply(system, user):
        if len(_positions(user)) > 1:
            return "抱歉，我无法完成这个请求。"          # 整批不可用
        return _echo_by_pos(user)

    calls = _recording(monkeypatch, reply)
    got, errors = _score(_cands(3), tmp_path, batch=3)

    assert len(got) == 3, errors
    # 整批 2 次（含 llm_json 的带原因重发）+ 降级逐篇 3 次
    assert len(calls) == 2 + 3, f"期望整批 2 次 + 逐篇 3 次，实际 {len(calls)} 次"
    assert [len(_positions(c["user"])) for c in calls] == [3, 3, 1, 1, 1]


def test_scoring_bad_tags_reasks_only_that_item(monkeypatch, tmp_path):
    """个别篇标签非法 → 只补那一篇，**其余不重发**（重发会让好答案冒被改写的风险）。"""
    def reply(system, user):
        pos = _positions(user)
        rows = []
        for i in pos:
            tags = ["自造词"] if len(pos) > 1 and i == 1 else ["数通"]
            rows.append({"idx": i, "score": 8, "semiconductor": True, "title_cn": f"译名{i}",
                         "domain": wr.DOMAINS[0], "business_tags": tags, "reason": "r"})
        return json.dumps({"items": rows}, ensure_ascii=False)

    calls = _recording(monkeypatch, reply)
    got, errors = _score(_cands(4), tmp_path, batch=4, )

    assert len(calls) == 2, f"应当只补一轮，实际 {len(calls)} 次请求"
    assert _positions(calls[1]["user"]) == [0], "补漏轮只该含标签非法的那一篇"
    assert got["a1"]["business_tags"] == ["接入"] or got["a1"]["business_tags"] == ["公共"] \
        or got["a1"]["business_tags"], "标签兜底没生效"
    assert got["a1"]["business_tags"] != ["自造词"], "自造词被原样留下"
    assert got["a0"]["business_tags"] == ["数通"], "其余篇目的标签被改动了"


def test_scoring_progress_counts_articles_not_batches(monkeypatch, tmp_path):
    """进度回调的语义是**篇数**（前端进度条按它算），不是批数。"""
    _recording(monkeypatch, lambda s, u: _echo_by_pos(u))
    seen: list[tuple[int, int]] = []
    _score(_cands(9), tmp_path, batch=4, on_progress=lambda d, t: seen.append((d, t)))
    assert seen == [(4, 9), (8, 9), (9, 9)], seen


def test_scoring_batch_size_one_accepts_reply_without_idx(monkeypatch, tmp_path):
    """batch=1 时必须接受「不带 idx」的老写法 —— 与改造前的 _pick_item 等价。"""
    _recording(monkeypatch, json.dumps({"items": [
        {"score": 7, "semiconductor": True, "title_cn": "译名",
         "domain": wr.DOMAINS[0], "business_tags": ["数通"], "reason": "r"}]},
        ensure_ascii=False))
    got, _ = _score(_cands(1), tmp_path, batch=1)
    assert got["a0"]["score"] == 7 and got["a0"]["title_cn"] == "译名"


def test_scoring_batch_size_one_equals_legacy(monkeypatch, tmp_path):
    """批大小不能改变**结果**：1 与 8 跑同一份语料，逐字段相等。"""
    _recording(monkeypatch, lambda s, u: _echo_by_title(u))
    one, _ = _score(_cands(5), tmp_path, batch=1,
                    cache=wr.WeeklyCache(tmp_path / "c1.json"))
    many, _ = _score(_cands(5), tmp_path, batch=8,
                     cache=wr.WeeklyCache(tmp_path / "c8.json"))
    assert one == many


def test_scoring_batches_rotate_models_by_batch(monkeypatch, tmp_path):
    """一批只发给**一个**模型；多模型时按批序号轮询（不是按篇）。"""
    models = [ModelConfig(id="m1", name="A", base_url="http://x", api_key="k",
                          model="x", enabled=True, format="openai"),
              ModelConfig(id="m2", name="B", base_url="http://x", api_key="k",
                          model="x", enabled=True, format="openai")]
    calls: list[str] = []

    def fake_call(cfg, system, user, max_retries=3, **kw):
        calls.append(cfg.name)
        return _echo_by_pos(user)

    monkeypatch.setattr(af, "_call_model", fake_call)
    got, _ = wr.score_candidates(
        _cands(5), models, prompts=dict(wr.load_prompts(tmp_path / "none.json")),
        cache=wr.WeeklyCache(tmp_path / "c.json"), workers=1, batch_size=1)

    assert len(got) == 5
    assert calls == ["A", "B", "A", "B", "A"], calls


def test_scoring_chunks_after_cache_filtering(monkeypatch, tmp_path):
    """切块要在**缓存过滤之后** —— 命中缓存的篇目不该白占批位。"""
    cache = wr.WeeklyCache(tmp_path / "c.json")
    cands = _cands(8)
    for it in cands[:2]:
        cache.put("scores", wr.cache_key("scoring", None, it["key"]),
                  {"score": 9, "semiconductor": True, "title_cn": "命中",
                   "domain": wr.DOMAINS[0], "business_tags": ["数通"], "reason": "cached"})

    calls = _recording(monkeypatch, lambda s, u: _echo_by_pos(u))
    got, _ = _score(cands, tmp_path, batch=4, cache=cache)

    assert len(got) == 8
    assert len(calls) == 2, f"8 篇里 2 篇命中，剩 6 篇应当只切 2 批，实际 {len(calls)}"
    assert "文章0" not in calls[0]["user"] and "文章1" not in calls[0]["user"]
    for it in cands[:2]:
        assert got[it["key"]]["title_cn"] == "命中", "缓存命中的篇目被覆盖了"


def test_scoring_batch_truncates_each_article_not_the_whole_message(monkeypatch, tmp_path):
    """**每篇各自**截断到 2000 字 —— 对整条 message 截断会让第 2 篇之后全丢。"""
    calls = _recording(monkeypatch, lambda s, u: _echo_by_pos(u))
    long = "开场。" * 600 + "关键信号甲。"      # 关键词落在 ~1800 字
    other = "引子。" * 600 + "关键信号乙。"
    _score([_cand("a1", "甲", text=long), _cand("a2", "乙", text=other)],
           tmp_path, batch=2)

    user = calls[0]["user"]
    assert "关键信号甲。" in user, "第一篇正文被截断了"
    assert "关键信号乙。" in user, "后面那篇的正文被整条 message 截断吃掉了"
    assert len(user) < 2 * wr.SCORING_TEXT_CHARS + 800


def test_scoring_batch_1based_numbering_is_corrected(monkeypatch, tmp_path):
    """整批用 1-based 编号时左移一位 —— 但有守卫，不做启发式猜测。"""
    def reply(system, user):
        return json.dumps({"items": [
            {"idx": i + 1, "score": 8, "semiconductor": True, "title_cn": f"译名{i}",
             "domain": wr.DOMAINS[0], "business_tags": ["数通"], "reason": "r"}
            for i in _positions(user)]}, ensure_ascii=False)

    _recording(monkeypatch, reply)
    got, errors = _score(_cands(3), tmp_path, batch=3)
    assert [got[f"a{i}"]["title_cn"] for i in range(3)] == ["译名0", "译名1", "译名2"]
    assert not errors, errors


def test_scoring_1based_guard_does_not_fire_on_partial_answers(monkeypatch, tmp_path):
    """守卫条件：只有「0 缺席**且**编号恰好是 1..n」才纠偏。

    只答了 {1,2} 的残缺回复不能被当成 1-based 整体左移 —— 那会把第 1 篇的答案
    错按到第 0 篇头上。
    """
    def reply(system, user):
        return json.dumps({"items": [
            {"idx": i, "score": 8, "semiconductor": True, "title_cn": f"译名{i}",
             "domain": wr.DOMAINS[0], "business_tags": ["数通"], "reason": "r"}
            for i in _positions(user) if i != 0]}, ensure_ascii=False)

    _recording(monkeypatch, reply)
    got, _ = _score(_cands(3), tmp_path, batch=3)
    assert "a0" not in got or got["a0"]["title_cn"] != "译名1", "残缺回复被错误地左移了"


# ── 核心洞察的 JSON 包装事故（2026-09 实跑第 1 期）────────────────────
#
# 故障链：传输层强制 response_format=json_object → 模型只能回 {"content": "…"}
#         → _clean_intro 不解析 JSON，整包原样进报告
#         → JSON 里转义的 \n 是**字面反斜杠+n**，所以分段全失效、①–⑤ 挤成一行
#         → html.escape 把引号编成 &quot;，模板 | safe 输出后就是用户看到的那串
#
# 所以这里的 fixture 不手抄，而是**按故障本身的构造过程生成**，改哪一环都会跟着变。


def _model_wrapped_intro() -> str:
    """模型在 json_object 模式下**只能**回出来的形状（\\n 是 JSON 转义，不是真换行）。"""
    return json.dumps(
        {"content": "本期（2026-09-06 至 2026-09-12）精选 10 篇。\n① 看点一。\n② 看点二。"},
        ensure_ascii=False,
    )


def _incident_snapshot_intro() -> str:
    """那期 ``data/report.json`` 里真正存下来的东西 —— _clean_intro 已经跑过一遍。"""
    return html.escape(_model_wrapped_intro()).replace("\n", "<br>\n")


def test_intro_call_turns_off_json_mode(monkeypatch, tmp_path):
    """协议约束必须与「要的是成稿文字」这个语义一致 —— 这是根因所在。

    json_object 模式下模型**只能**回包装对象，`_clean_intro` 再怎么修都只是兜底。
    """
    seen: dict = {}

    def fake_call(cfg, system, user, max_retries=3, **kw):
        seen.update(kw)
        return "本期精选 3 篇。\n① 看点一。"

    monkeypatch.setattr(af, "_call_model", fake_call)
    wr.generate_intro([_cand("a1", "文章")], {}, {}, [_cfg()],
                      prompts=dict(wr.load_prompts(tmp_path / "none.json")),
                      from_date="2026-09-01", to_date="2026-09-07")
    assert seen.get("json_mode") is False, "核心洞察阶段必须关掉 json_object"


def test_clean_intro_unwraps_json_wrapper():
    """万一还是被包了一层（换了模型、协议层回退），也绝不能让它进报告。"""
    out = wr._clean_intro(_model_wrapped_intro())
    assert "content" not in out, "包装对象的字段名漏进了报告正文"
    assert "{" not in out
    assert "<br>" in out, "字面量 \\n 没有被还原成换行，① ② 会挤成一行"
    assert "① 看点一。" in out


def test_clean_intro_unwraps_wrapper_with_bare_newlines():
    """`json.loads` 解不开的包装也要能抠出来 —— 正则兜底那条路。

    两种成因分开钉：裸换行（非法 JSON，但换行本来就是**真的**）与
    转义换行 + 语法错误（换行是**字面反斜杠+n**，必须手动还原，否则整段不分行）。
    """
    # ① 正文里带裸换行 → JSON 非法，换行本身是真的
    out = wr._clean_intro('{"content": "第一行\n第二行"}')
    assert "content" not in out
    assert "第一行<br>" in out

    # ② 转义换行 + 尾逗号 → JSON 非法，但换行是字面量，不还原就永远分不了行
    out2 = wr._clean_intro('{"content": "甲\\n乙",}')
    assert "content" not in out2
    assert "甲<br>" in out2, "字面量 \\n 没有被还原成真换行"


def test_clean_intro_leaves_plain_text_alone():
    """解包只认「已知正文字段」那一种形状 —— 正文长得像 JSON 也不能被吃掉。"""
    assert "正常正文" in wr._clean_intro("这是 {不是 JSON} 的一段正常正文。")
    # 合法 JSON、但没有正文字段：不认识就原样留着，不做猜测
    assert "标题" in wr._clean_intro('{"标题": "正文"}')


def test_repair_saved_intro_fixes_the_incident_snapshot():
    """那期快照存的是**已经转义过**的坏数据，json.loads 再也解不开，得先还原实体。"""
    snapshot = _incident_snapshot_intro()
    assert "&quot;content&quot;" in snapshot, "没复现出那批数据的形状，这个测试就白写了"
    fixed = wr.repair_saved_intro(snapshot)
    assert "content" not in fixed
    assert "<br>" in fixed
    assert "① 看点一。" in fixed


def test_repair_saved_intro_keeps_normal_intro():
    """正常引言（哪怕是带实体的）必须原样返回 —— 修复不能把好数据改坏。"""
    normal = "本期精选 10 篇。&amp;<br>\n① 看点一。"
    assert wr.repair_saved_intro(normal) == normal


def test_load_saved_context_repairs_intro(tmp_path):
    """往期报告点一次「重新渲染」就该恢复 —— 不必重新花钱生成。"""
    d = tmp_path / "第1期_2026-09-12"
    (d / "data").mkdir(parents=True)
    (d / "data" / "report.json").write_text(
        json.dumps({"issue": {"num": 1}, "intro": _incident_snapshot_intro()},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    ctx = wr.load_saved_context(d)
    assert ctx is not None
    assert "content" not in ctx["intro"]
    assert "<br>" in ctx["intro"]


def test_intro_prompt_never_mentions_json():
    """拆掉陷阱：intro 的固定输出约束里**不能**出现 "json"。

    DeepSeek 要求 json_object 模式时 prompt 必须含 "json" —— 早先那句「不要 JSON」
    恰好满足了它，于是模型被迫包装。根因虽已关掉，但这行字留着就是下一颗雷。
    """
    assert "json" not in wr.build_prompt("intro").lower()


# ── 补正文（2026-09 实跑暴露：九成文章没有正文）──────────────────────
#
# 用户的库实测：379 篇里只有 38 篇（10%）带 body_text —— 那份缓存只有跑过
# 「AI 内容筛选」或「导出正文」才会有。于是打分与解读阶段模型只看得到标题，
# 明明有实测数据的文章被判成「文中未给出量化数据」。


def test_needs_body_only_for_wechat_without_body():
    """只给「公众号 + 有链接 + 正文过短」的候选补正文。

    外部条目的摘要本身就是可判定内容（arXiv 摘要信息量足够），不该联网抓。
    """
    short = _cand("a1", "标题", text="几十字的摘要")
    long = _cand("a2", "标题", text="正文。" * 200)
    ext = _cand("a3", "论文", kind="arXiv", text="摘要")
    nolink = _cand("a4", "标题", text="摘要")
    nolink["url"] = ""
    assert wr.needs_body(short) is True
    assert wr.needs_body(long) is False
    assert wr.needs_body(ext) is False
    assert wr.needs_body(nolink) is False
    assert wr.BODY_MIN_CHARS == 200


def test_fetch_missing_bodies_fills_and_isolates_failures(monkeypatch):
    """抓到正文；单篇失败只记一笔，不影响其余篇目。"""
    from mp_harvest.core import article_reader

    def fake_fetch(url, *, cred=None, timeout=25.0):
        if "bad" in url:
            raise RuntimeError("网络不通")
        if "empty" in url:
            return {"content_found": False}
        return {"content_found": True, "body_text": f"{url} 的正文" * 20,
                "body_html": "<p>x</p>"}

    monkeypatch.setattr(article_reader, "fetch_and_parse_article", fake_fetch)
    cands = [
        _cand("a1", "甲", text="短摘要"),
        _cand("a2", "乙", text="短摘要"),
        _cand("a3", "丙", text="短摘要"),
    ]
    cands[1]["url"] = "https://mp.weixin.qq.com/s/bad"
    cands[2]["url"] = "https://mp.weixin.qq.com/s/empty"

    got, errors = wr.fetch_missing_bodies(cands, cred_for=lambda _aid: {}, workers=1)
    assert set(got) == {"a1"}
    assert got["a1"][0].startswith("https://example.com/a1")
    assert len(errors) == 2 and any("网络不通" in e for e in errors)
    assert any("环境校验" in e for e in errors)


def test_generate_issue_fetches_body_before_scoring(monkeypatch, tmp_path):
    """**先补正文再打分** —— 顺序错了整个改动就白做（模型还是只看标题）。"""
    from mp_harvest.core import article_reader

    monkeypatch.setattr(article_reader, "fetch_and_parse_article",
                        lambda url, **kw: {"content_found": True,
                                           "body_text": "实测数据：能效提升 66%。" * 20,
                                           "body_html": "<p>x</p>"})
    seen: list[str] = []

    def fake_call(cfg, system, user, max_retries=3, **kw):
        seen.append(user)
        return _fake_scoring_reply(user)

    monkeypatch.setattr(af, "_call_model", fake_call)
    saved: list[list[dict]] = []
    tpl = _tpl(tmp_path, "{{ issue.num }}")
    wr.generate_issue(
        candidates=[_cand("a1", "有数据的文章", text="55 字的摘要")],
        models=[_cfg()], prompts=wr.load_prompts(tmp_path / "none.json"),
        cache=wr.WeeklyCache(tmp_path / "c.json"), out_dir=tmp_path / "out",
        issue_num=1, from_date="a", to_date="b", selected_count=1,
        template_path=tpl, workers=1,
        cred_for=lambda _aid: {}, save_bodies=saved.append,
    )

    assert seen, "打分阶段没有调用模型"
    assert "实测数据：能效提升 66%" in seen[0], "打分时还没有正文（补正文顺序错了）"
    assert saved and saved[0][0]["body_text"].startswith("实测数据"), "正文没有写回缓存"
    assert saved[0][0]["identity"], "写回要靠 identity，缺了就对不上号"


def test_generate_issue_can_skip_body_fetch(monkeypatch, tmp_path):
    """关掉补正文时**一次网络请求都不发** —— 勾选框得真的管用。"""
    from mp_harvest.core import article_reader

    monkeypatch.setattr(article_reader, "fetch_and_parse_article",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该发请求")))
    seen: list[str] = []
    monkeypatch.setattr(af, "_call_model", lambda cfg, s, u, **k: (seen.append(u),
                                                                   _fake_scoring_reply(u))[1])
    tpl = _tpl(tmp_path, "{{ issue.num }}")
    res = wr.generate_issue(
        candidates=[_cand("a1", "只有标题", text="短摘要")],
        models=[_cfg()], prompts=wr.load_prompts(tmp_path / "none.json"),
        cache=wr.WeeklyCache(tmp_path / "c.json"), out_dir=tmp_path / "out",
        issue_num=1, from_date="a", to_date="b", selected_count=1,
        template_path=tpl, workers=1, fetch_bodies=False,
        cred_for=lambda _aid: {},
    )
    assert res["ok"] is True
    assert "短摘要" in seen[0], "关掉之后应当仍用摘要"


def test_scoring_prompt_separates_tech_disclosure_from_promotion():
    """打分标准必须分清「技术披露」与「厂商宣传」（2026-09 用户要求）。

    起因：报告里混进了 PCIM 展会报道《从电网到算力：东芝的功率半导体新版图》——
    通篇讲技术参数，但内容是「某公司展示了/布局了」。只靠「纯市场新闻不相关」
    那句挡不住，模型会觉得它讲的就是技术。

    ⚠️ 尺度很关键（用户中途纠正过一次）：**厂商自己发布的论文/技术说明算技术内容，
    不因来源是厂商而扣分** —— 半导体行业的一手信息本来就多来自厂商。要挡的只是
    「罗列产品线、展台、发布会」那种宣传稿。
    """
    s = wr.build_prompt("scoring")
    assert "厂商宣传" in s and "展会报道" in s and "企业软文" in s
    assert "不因来源是厂商而扣分" in s, "把厂商自证也一并否掉了（用户明确说不可接受）"
    assert "技术披露" in s
    # 判据落在「有没有可复现的方法/实验设计」，而不是「题材是不是技术」
    assert "读不到可复现的方法或实验设计" in s


def test_output_schema_ties_promotion_to_the_semiconductor_flag():
    """「判成市场内容」必须落到 `semiconductor=false` 上。

    实测过：只让模型给低分是不够的 —— 它会给出 `semiconductor: true` + 3.0 分，
    而**入选与否看的是那个字段**，低分照样进报告，只是排在最后。
    """
    s = wr.build_prompt("scoring")
    assert "厂商宣传稿" in s and "一律 false" in s
    assert "只给低分不够" in s
    # 实测过的漏洞：模型会在理由里写「…但整体仍偏展台与产品线报道」，然后照样给 true。
    # 必须把这句话堵死，否则规则只在「单篇送审」时管用、一批八篇就手软。
    assert "就必须给 false" in s and "不许" in s


# ── 候选要不要尊重 AI 筛选结果（2026-09）──────────────────────────
#
# 用户实际遇到的问题：窗口内 37 篇候选里有 **33 篇是他早就筛掉的**，
# 周报把它们重新打分（花钱）还放进了报告 —— 那次筛选等于白做。


def _cand_with_verdict(key: str, ts: int, verdict):
    """造一条**文章缓存行**（带 AI 最终判定）。

    字段名是 ``keep``（``state.merge_article_verdicts`` 写的那个），
    不是候选里的 ``verdict`` —— 后者是 normalize 之后才有的。
    """
    c = _cand(key, f"文章{key}", ts=ts)
    c["keep"] = verdict
    return c


def test_only_kept_skips_filtered_but_keeps_unjudged():
    """只排除**明确判过「过滤掉」**的；未判定（None）照收。

    「未判定」≠「被否掉」—— 刚拉来还没来得及筛的文章不该被静默丢掉。
    """
    rows = [
        (_cand_with_verdict("done_ok", 2000, True), "号A", "a1"),
        (_cand_with_verdict("dropped", 1900, False), "号A", "a1"),
        (_cand_with_verdict("never", 1800, None), "号A", "a1"),
    ]
    loose = wr.collect_candidates(wechat_rows=rows, start_ts=1000, end_ts=3000)
    assert len(loose) == 3, "不筛选时三篇都该在"

    kept = wr.collect_candidates(wechat_rows=rows, start_ts=1000, end_ts=3000, only_kept=True)
    assert {c["key"] for c in kept} == {"wechat:mid:done_ok", "wechat:mid:never"}, \
        "只该去掉被明确否掉的那篇"


def test_only_kept_applies_to_external_items_too():
    """外部条目的判定是读缓存合并进来的，同样要生效（否则只有公众号侧被尊重）。"""
    items = [
        {**_erow("arxiv:1", "过的", 2000), "keep": True},
        {**_erow("arxiv:2", "否的", 1900), "keep": False},
        {**_erow("arxiv:3", "没判过", 1800)},
    ]
    assert len(wr.collect_candidates(external_items=items, start_ts=1000, end_ts=3000)) == 3
    kept = wr.collect_candidates(external_items=items, start_ts=1000, end_ts=3000, only_kept=True)
    assert {c["key"] for c in kept} == {"ext:arxiv:1", "ext:arxiv:3"}


def test_candidate_carries_its_verdict():
    """判定要跟着候选走到 collect_candidates 里 —— 否则上面那条规则无从执行。"""
    got = wr.collect_candidates(
        wechat_rows=[(_cand("a1", "甲"), "号A", "acct-A")], start_ts=0, end_ts=0)
    assert "verdict" in got[0]

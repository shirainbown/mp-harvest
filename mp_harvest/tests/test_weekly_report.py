"""周报生成（core/weekly_report.py）单元测试。

重点钉住两件用户明确关心的事：
1. **模板可以随便改** —— 改什么输出就是什么；写错变量名要报出来而不是静默空白。
2. **提示词可以随便改** —— 改哪段只有哪段缓存失效，改回来还能命中。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core import weekly_report as wr  # noqa: E402
from mp_harvest.core.ai_filter import ModelConfig  # noqa: E402


def _cfg(name: str = "m1") -> ModelConfig:
    return ModelConfig(id="m1", name=name, base_url="http://127.0.0.1:1",
                       api_key="k", model="x", enabled=True, format="openai")


def _cand(key: str, title: str, *, ts: int = 1754400000, kind: str = "公众号",
          text: str = "正文内容足够长以便被当作真实候选处理，包含技术细节。") -> dict:
    return {
        "key": key, "kind": kind, "title": title, "title_cn": "", "arxiv_id": "",
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
    w = [(_wrow("mid:1", "窗口内", 2000), "号A"), (_wrow("mid:2", "窗口外", 100), "号A")]
    e = [_erow("arxiv:9", "外部", 1500)]
    got = wr.collect_candidates(wechat_rows=w, external_items=e, start_ts=1000, end_ts=3000)
    assert {c["key"] for c in got} == {"wechat:mid:1", "ext:arxiv:9"}
    # 按发布时间倒序
    assert [c["key"] for c in got] == ["wechat:mid:1", "ext:arxiv:9"]
    # 同一篇重复出现只留一条
    dup = wr.collect_candidates(wechat_rows=[(_wrow("mid:1", "A", 10), "x"),
                                             (_wrow("mid:1", "B", 10), "y")])
    assert len(dup) == 1


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


def _stub_model(monkeypatch, *, n_candidates: int = 3):
    from mp_harvest.core import ai_filter as af

    def fake_call(cfg, system, user, max_retries=3, **kw):
        if '"score"' in system:
            return json.dumps({"items": [{
                "idx": 0, "score": 8.5, "semiconductor": True, "title_cn": "译名",
                "domain": wr.DOMAINS[0], "business_tags": ["数通", "公共"],
                "reason": "关键数据支撑的入选理由"}]}, ensure_ascii=False)
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


def test_builtin_template_links_to_local_archive():
    """归档过正文时，概览表与深度解读卡片都要给出「本地存档」入口。"""
    h = wr.render_report(_full_ctx())["html"]
    assert h.count('href="articles/a1.html"') == 2     # 精选：概览表 + 深度解读卡片
    assert h.count('href="articles/a3.html"') == 1     # 其他入选只有板块三表格
    # 没有归档文件的文章不应出现空链接
    assert 'href=""' not in h

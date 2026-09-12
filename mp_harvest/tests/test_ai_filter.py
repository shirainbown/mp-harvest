"""AI 过滤引擎纯逻辑测试（不访问真实网络）。"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core import ai_filter as af  # noqa: E402
from mp_harvest.core.ai_filter import (  # noqa: E402
    _call_model,
    DEFAULT_CONTENT_PRINCIPLES,
    DEFAULT_CONTENT_PROMPT,
    DEFAULT_PRINCIPLES,
    DEFAULT_PROMPT,
    FIXED_OUTPUT_REQUIREMENTS,
    ModelConfig,
    _model_label,
    _build_anthropic_payload,
    _build_openai_payload,
    _build_payload,
    _endpoint,
    _parse_content,
    article_key,
    build_system_prompt,
    default_content_principles_path,
    default_principles_path,
    distribute_batches,
    judge_articles,
    load_content_principles,
    load_models,
    load_principles,
    save_content_principles,
    parse_model_output,
    save_models,
    save_principles,
)


def _cfg(name: str = "test", base: str = "http://127.0.0.1:1") -> ModelConfig:
    return ModelConfig(
        id=name,
        name=name,
        base_url=base,
        api_key="sk-test",
        model="m",
        enabled=True,
    )


def _art(key: str, title: str = "t") -> dict:
    return {"identity": key, "link": f"https://mp.weixin.qq.com/s/{key}", "title": title}


def test_model_config_roundtrip():
    m = ModelConfig(
        id="a", name="DeepSeek", base_url="https://api.deepseek.com/v1",
        api_key="k", model="deepseek-chat", enabled=False,
    )
    m2 = ModelConfig.from_dict(m.to_dict())
    assert m2 == m


def test_model_config_format_roundtrip():
    m = ModelConfig(
        id="c", name="Claude", base_url="https://api.anthropic.com",
        api_key="sk-ant", model="claude-sonnet-4-20250514", format="anthropic",
    )
    m2 = ModelConfig.from_dict(m.to_dict())
    assert m2.format == "anthropic"
    # 非法 format 回退 openai
    bad = ModelConfig.from_dict({"format": "cohere"})
    assert bad.format == "openai"


def test_model_label():
    m = ModelConfig(
        id="a", name="DeepSeek", base_url="", api_key="", model="deepseek-chat"
    )
    assert _model_label(m) == "DeepSeek (deepseek-chat)"
    # 名称为空 → 直接显示模型 ID
    m2 = ModelConfig(id="b", name="", base_url="", api_key="", model="deepseek-reasoner")
    assert _model_label(m2) == "deepseek-reasoner"
    # 名称与模型相同 → 不重复
    m3 = ModelConfig(id="c", name="gpt-4o", base_url="", api_key="", model="gpt-4o")
    assert _model_label(m3) == "gpt-4o"


def test_anthropic_endpoint_payload_parse():
    cfg = ModelConfig(
        id="c", name="Claude", base_url="https://api.anthropic.com",
        api_key="sk-ant", model="claude-sonnet-4-20250514", format="anthropic",
    )
    # base_url 不带 /v1 → 补 /v1/messages；带 /v1 → 直接 /messages
    assert _endpoint(cfg) == "https://api.anthropic.com/v1/messages"
    cfg2 = ModelConfig(**{**cfg.to_dict(), "base_url": "https://api.anthropic.com/v1"})
    assert _endpoint(cfg2) == "https://api.anthropic.com/v1/messages"

    payload = _build_anthropic_payload(cfg, "系统提示", "用户内容")
    assert payload["max_tokens"] == 4096
    assert payload["system"] == "系统提示"
    assert payload["messages"] == [{"role": "user", "content": "用户内容"}]
    assert "response_format" not in payload

    # 响应解析：content 块数组
    data = {"content": [{"type": "text", "text": "{\"items\":[]}"}]}
    assert _parse_content(cfg, data) == '{"items":[]}'
    assert _parse_content(cfg, {"content": []}) == ""
    # OpenAI 响应解析保持兼容
    oai = ModelConfig(
        id="o", name="O", base_url="https://x/v1", api_key="k",
        model="m", format="openai",
    )
    assert _parse_content(oai, {"choices": [{"message": {"content": "hi"}}]}) == "hi"


def test_openai_payload_json_mode_can_be_turned_off():
    """要**成稿文字**的阶段必须能关掉 json_object（2026-09 周报核心洞察事故的根因）。

    开着的时候模型只能合规地回 ``{"content": "…"}``，调用方按纯文本处理就会把整包
    放进最终产物。默认**仍然开着** —— 既有调用点的行为一个字都不能变。
    """
    cfg = ModelConfig(id="o", name="O", base_url="https://x/v1", api_key="k",
                      model="m", format="openai")
    assert _build_openai_payload(cfg, "s", "u")["response_format"] == {"type": "json_object"}
    assert "response_format" not in _build_openai_payload(cfg, "s", "u", json_mode=False)
    # 走 _build_payload 这条真实分发路径也要生效（_call_model 用的是它）
    assert "response_format" not in _build_payload(cfg, "s", "u", json_mode=False)
    assert _build_payload(cfg, "s", "u")["response_format"] == {"type": "json_object"}


def test_save_load_models():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "ai_models.json"
        save_models(p, [_cfg("a"), _cfg("b")])
        loaded = load_models(p)
        assert [m.name for m in loaded] == ["a", "b"]


def test_load_models_missing_returns_default():
    with tempfile.TemporaryDirectory() as td:
        loaded = load_models(Path(td) / "nope.json")
        assert len(loaded) >= 1
        assert loaded[0].name == "模型"
        assert loaded[0].base_url == ""  # 不自带厂商，地址由用户填写


def test_load_models_empty_list_stays_empty():
    """删除全部模型后保存空列表，重启不应再冒默认模板。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "ai_models.json"
        save_models(p, [])
        assert load_models(p) == []


def test_parse_model_output_with_fence():
    text = '```json\n{"items":[{"idx":0,"title":"x","keep":true,"category":"fpga","relevance_score":9,"technical_depth":6,"confidence":"high","reason":"前沿FPGA架构分析"}]}\n```'
    items = parse_model_output(text)
    assert len(items) == 1
    assert items[0]["idx"] == 0
    assert items[0]["keep"] is True


def test_parse_model_output_invalid_raises():
    try:
        parse_model_output("不是JSON")
        assert False, "应抛出 ValueError"
    except ValueError:
        pass
    try:
        parse_model_output('{"items": "oops"}')
        assert False, "应抛出 ValueError"
    except ValueError:
        pass


def test_distribute_batches_round_robin():
    batches = distribute_batches(10, 4, 2)
    assert batches == [(0, 4, 0), (4, 8, 1), (8, 10, 0)]
    assert distribute_batches(0, 4, 2) == []
    assert distribute_batches(3, 5, 1) == [(0, 3, 0)]


def test_article_key():
    assert article_key({"identity": "i", "link": "u"}) == "i"
    assert article_key({"link": "u", "title": "t"}) == "u"
    assert article_key({"title": "t"}) == "t"


def test_judge_articles_cache_hit_no_network():
    arts = [_art("id1"), _art("id2")]
    cache = {
        "id1": {"keep": True, "category": "fpga", "reason": "命中"},
        "id2": {"keep": False, "category": "other", "reason": "命中"},
    }
    with tempfile.TemporaryDirectory() as td:
        cp = Path(td) / "cache.json"
        cp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        # base_url 指向不可达地址，若误发请求会立刻失败；缓存命中时不应请求
        res = judge_articles(
            arts,
            [_cfg("bad")],
            cache_path=cp,
            batch_size=2,
            workers=1,
            max_retries=1,
        )
    assert res["ok"] is True
    assert res["cached"] == 2
    assert res["judged"] == 0
    assert [a["title"] for a in res["kept"]] == ["t"]
    assert len(res["dropped"]) == 1
    assert res["kept"][0]["reason"] == "命中"


def test_judge_articles_no_enabled_models():
    m = _cfg("disabled")
    m.enabled = False
    try:
        judge_articles([_art("id1")], [m])
        assert False, "应抛出 ValueError"
    except ValueError:
        pass


def test_judge_articles_model_failure_fallback():
    arts = [_art("id1"), _art("id2")]
    with tempfile.TemporaryDirectory() as td:
        cp = Path(td) / "cache.json"
        res = judge_articles(
            arts,
            [_cfg("bad")],
            cache_path=cp,
            batch_size=2,
            workers=1,
            max_retries=1,
        )
    assert res["ok"] is False
    assert res["errors"]
    assert len(res["dropped"]) == 2
    assert all(a.get("keep") is False for a in res["dropped"])
    assert all("模型" in a.get("reason", "") for a in res["dropped"])
    # 关键：模型调用失败的兜底判定**只影响本轮展示，绝不写入持久缓存**。
    # 写入就会把文章永久拉黑 —— 之后网络恢复也直接命中缓存、一次请求都不发
    # （2026-09 修复，原断言锁死了这个 bug）。
    with tempfile.TemporaryDirectory() as td2:
        cp2 = Path(td2) / "cache.json"
        judge_articles(
            arts,
            [_cfg("bad")],
            cache_path=cp2,
            batch_size=2,
            workers=1,
            max_retries=1,
        )
        data = json.loads(cp2.read_text(encoding="utf-8"))
        assert data.get("entries") == {}, f"失败兜底不应入缓存：{data}"



def test_build_system_prompt():
    # 默认 prompt = 原则 + 固定输出要求
    assert FIXED_OUTPUT_REQUIREMENTS in DEFAULT_PROMPT
    assert DEFAULT_PRINCIPLES in DEFAULT_PROMPT
    # 用户自定义原则会被包裹，固定格式要求仍保留
    custom = "只保留与 FPGA 验证相关的前沿文章"
    full = build_system_prompt(custom)
    assert custom in full
    assert "idx 必须与输入列表中的序号一一对应" in full
    assert "严格 JSON" in full
    # 空原则回退到默认原则
    assert DEFAULT_PRINCIPLES in build_system_prompt("  ")


def test_principles_persistence():
    with tempfile.TemporaryDirectory() as td:
        path = default_principles_path(Path(td))
        assert path.name == "ai_principles.txt"
        # 未保存时返回默认原则
        assert load_principles(path) == DEFAULT_PRINCIPLES
        custom = "自定义筛选原则：只保留 AI EDA 内容"
        save_principles(path, custom)
        assert load_principles(path) == custom
        # 空内容回退默认
        save_principles(path, "   ")
        assert load_principles(path) == DEFAULT_PRINCIPLES


def test_content_principles_persistence():
    with tempfile.TemporaryDirectory() as td:
        path = default_content_principles_path(Path(td))
        assert path.name == "ai_content_principles.txt"
        # 未保存时返回默认内容原则
        assert load_content_principles(path) == DEFAULT_CONTENT_PRINCIPLES
        custom = "自定义内容原则：正文必须有代码或数据"
        save_content_principles(path, custom)
        assert load_content_principles(path) == custom
        # 空内容回退默认
        save_content_principles(path, "   ")
        assert load_content_principles(path) == DEFAULT_CONTENT_PRINCIPLES


def test_content_prompt_builds_from_content_principles():
    assert FIXED_OUTPUT_REQUIREMENTS in DEFAULT_CONTENT_PROMPT
    assert DEFAULT_CONTENT_PRINCIPLES in DEFAULT_CONTENT_PROMPT


def test_test_connection_retries_without_response_format():
    """DeepSeek 等模型对 json_object 要求 prompt 含 "json"：首次 400 应去掉
    response_format 重试成功（2026-08-09 真机：max_retries=1 会把兜底掐掉）。"""
    import io
    import urllib.error

    from mp_harvest.core import ai_filter

    calls: list[dict] = []

    class _FakeResp:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._data

    original = ai_filter._urlopen

    def fake_urlopen(req, timeout=180):
        payload = json.loads(req.data.decode("utf-8"))
        calls.append(payload)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "Bad Request",
                None,
                io.BytesIO(
                    b'{"error":{"message":"Prompt must contain the word \'json\' '
                    b"in some form to use response_format json_object.}}"
                ),
            )
        return _FakeResp(b'{"choices":[{"message":{"content":"OK"}}]}')

    ai_filter._urlopen = fake_urlopen
    try:
        ok, msg = ai_filter.test_connection(_cfg(base="https://api.deepseek.com"))
    finally:
        ai_filter._urlopen = original

    assert ok is True
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]


def test_fetch_models_ok_and_errors():
    """/models 列表解析 + 401 报错 + anthropic 不支持（2026-08-09 新增）。"""
    import io
    import urllib.error

    from mp_harvest.core import ai_filter

    original = ai_filter._urlopen

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return (
                b'{"object":"list","data":[{"id":"deepseek-chat"},'
                b'{"id":"deepseek-reasoner"}]}'
            )

    def fake_urlopen(req, timeout=180):
        if "bad" in req.full_url:
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", None, io.BytesIO(b"{}")
            )
        return _FakeResp()

    ai_filter._urlopen = fake_urlopen
    try:
        ok, ids = ai_filter.fetch_models(_cfg(base="https://api.deepseek.com"))
        assert ok is True
        assert ids == ["deepseek-chat", "deepseek-reasoner"]

        ok2, msg2 = ai_filter.fetch_models(_cfg(base="https://bad.example.com"))
        assert ok2 is False
        assert "401" in msg2

        ok3, msg3 = ai_filter.fetch_models(
            ModelConfig(
                id="c", name="c", base_url="https://api.anthropic.com",
                api_key="k", model="m", format="anthropic",
            )
        )
        assert ok3 is False
        assert "Anthropic" in msg3
    finally:
        ai_filter._urlopen = original


def test_judge_articles_on_batch_callback():
    """每完成一批回调 on_batch（2026-08-09：供服务层实时推送判定结果）。"""
    from mp_harvest.core import ai_filter

    arts = [_art("id1"), _art("id2")]
    batches: list[tuple[int, str | None]] = []
    original = ai_filter._call_model

    def fake_call(cfg, system_prompt, user_content, max_retries=3):
        rows = json.loads(user_content)
        return json.dumps(
            {
                "items": [
                    {"idx": r["idx"], "keep": True, "reason": "ok", "relevance_score": 3}
                    for r in rows
                ]
            }
        )

    ai_filter._call_model = fake_call
    try:
        res = judge_articles(
            arts,
            [_cfg("m")],
            batch_size=1,
            workers=2,
            on_batch=lambda rows, err: batches.append((len(rows), err)),
        )
    finally:
        ai_filter._call_model = original

    assert res["judged"] == 2
    assert sorted(n for n, _ in batches) == [1, 1]
    assert all(err is None for _, err in batches)


def test_judge_articles_content_field_truncates_and_sends_content():
    """内容筛选阶段：输入携带截断后的正文内容（2026-08-16 新增）。"""
    import json as _json

    from mp_harvest.core import ai_filter

    arts = [{"identity": "id1", "link": "u", "title": "t", "body_text": "ABCDEFGHIJ"}]
    captured: list[str] = []
    original = ai_filter._call_model

    def fake_call(cfg, system_prompt, user_content, max_retries=3):
        captured.append(user_content)
        return '{"items":[{"idx":0,"keep":true}]}'

    ai_filter._call_model = fake_call
    try:
        res = judge_articles(
            arts,
            [_cfg("m")],
            content_field="body_text",
            max_content_chars=4,
            batch_size=1,
            workers=1,
        )
    finally:
        ai_filter._call_model = original

    assert res["judged"] == 1
    user = _json.loads(captured[0])
    assert user[0]["content"] == "ABCD"
    assert "title" in user[0]


def test_load_verdicts_is_side_effect_free():
    """``load_verdicts`` 是给只读展示端点用的，**绝不能写盘**。

    ``_load_cache`` 遇到旧格式会复制一个 ``.bak-`` 备份 —— 那是判定流程该做的事。
    展示端点只是读一下渲染理由，要是也走那条路，用户每打开一次列表就在数据目录里
    多堆一个备份文件。这条测试就是钉死这个区别。
    """
    from mp_harvest.core import ai_filter

    # 旧格式（无 __version__、无前缀）—— 正是会触发 _backup_file 的那种
    legacy = Path(tempfile.mkdtemp()) / "ai_filter_cache.json"
    legacy.write_text(
        json.dumps({"id1": {"keep": True, "reason": "相关", "model": "m"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    before = sorted(p.name for p in legacy.parent.iterdir())

    got = ai_filter.load_verdicts(legacy, prefix="")
    assert got["id1"]["keep"] is True
    assert got["id1"]["reason"] == "相关"

    after = sorted(p.name for p in legacy.parent.iterdir())
    assert before == after, f"load_verdicts 不该产生任何新文件，却有：{set(after) - set(before)}"


def test_legacy_unprefixed_fields_get_the_prefix():
    """**最老那一代**：字段无前缀（``keep``），迁移时要补上当前前缀。

    用户机上 2026-08-16 的 ``.bak-`` 备份就是这个形状。
    """
    from mp_harvest.core import ai_filter

    d = Path(tempfile.mkdtemp())
    f = d / "ai_filter_cache.json"
    f.write_text(
        json.dumps({"id1": {"keep": True, "reason": "相关", "model": "m"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    got = ai_filter.load_verdicts(f, prefix="title_")
    assert got["id1"]["title_keep"] is True
    assert got["id1"]["title_reason"] == "相关"


def test_mid_generation_prefixed_fields_are_readable():
    """**中间那一代**：扁平格式，但字段**已经带前缀**（``title_keep``）。

    这是真出事的那一代：只认无前缀字段的话 ``"keep" in v`` 为假 → 整行被丢 →
    整份缓存读成空的。用户机上两个缓存文件（1193 + 127 条）当时正是这个形状，
    后果是重跑筛选对每篇都重新调模型（白花钱），且外部来源的判定一律合并不进来。

    读不出来**不会报错**，只会安安静静当没有 —— 所以必须专门钉一条。
    """
    from mp_harvest.core import ai_filter

    d = Path(tempfile.mkdtemp())
    f = d / "ai_filter_cache.json"
    f.write_text(
        json.dumps({
            "id1": {"title_keep": False, "title_reason": "财经动态，无技术细节"},
            "id2": {"title_keep": True, "title_reason": "含工艺参数"},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    got = ai_filter.load_verdicts(f, prefix="title_")
    assert len(got) == 2, f"中间代整份被丢了，只读到 {got}"
    assert got["id1"]["title_keep"] is False
    assert got["id1"]["title_reason"] == "财经动态，无技术细节"
    assert got["id2"]["title_keep"] is True


def test_load_cache_and_load_verdicts_agree():
    """主流程（``_load_cache``）与展示端点（``load_verdicts``）必须读到同一份内容。

    两处曾各写一份迁移逻辑 —— 漂移的代价是「展示端点说没判定过、主流程却命中了
    缓存」这种自相矛盾的输出，且两边看上去都正常。
    """
    from mp_harvest.core import ai_filter

    d = Path(tempfile.mkdtemp())
    f = d / "ai_content_filter_cache.json"
    f.write_text(
        json.dumps({"id1": {"content_keep": True, "content_reason": "含实测数据"}},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    entries, migrated = ai_filter._load_cache(f, "content_")
    assert entries == ai_filter.load_verdicts(f, prefix="content_")
    assert entries["id1"]["content_keep"] is True
    # 迁移过就要落盘 —— 否则每轮都重迁一遍（调用方靠这个标记决定写回）
    assert migrated is True


def test_mid_generation_cache_survives_a_round_trip():
    """迁移一次之后就落到 ``__version__`` 格式，第二轮认出作者、不再迁移、不再备份。

    少了这层，每跑一轮筛选都会往数据目录里多堆一个 ``.bak-`` 备份。
    """
    from mp_harvest.core import ai_filter

    d = Path(tempfile.mkdtemp())
    f = d / "ai_filter_cache.json"
    f.write_text(json.dumps({"id1": {"title_keep": True, "title_reason": "好"}},
                            ensure_ascii=False), encoding="utf-8")

    entries, migrated = ai_filter._load_cache(f, "title_")
    assert migrated is True
    f.write_text(json.dumps({"__version__": ai_filter._CACHE_VERSION, "entries": entries},
                            ensure_ascii=False), encoding="utf-8")

    again, migrated2 = ai_filter._load_cache(f, "title_")
    assert again == entries
    assert migrated2 is False, "已是当前格式，不该再判为需要迁移"


def test_load_verdicts_reads_v2_and_tolerates_garbage():
    from mp_harvest.core import ai_filter

    d = Path(tempfile.mkdtemp())
    v2 = d / "c.json"
    v2.write_text(
        json.dumps({"__version__": 2, "entries": {"k": {"title_keep": False}}}),
        encoding="utf-8",
    )
    assert ai_filter.load_verdicts(v2, prefix="title_")["k"]["title_keep"] is False

    assert ai_filter.load_verdicts(d / "不存在.json") == {}
    bad = d / "bad.json"
    bad.write_text("[[[", encoding="utf-8")
    assert ai_filter.load_verdicts(bad) == {}
    arr = d / "arr.json"
    arr.write_text("[1,2,3]", encoding="utf-8")
    assert ai_filter.load_verdicts(arr) == {}


# ── 执行日志埋点（2026-09）────────────────────────────────────────
#
# 「用户能看到 AI 返回了什么、筛选结果如何」是这次需求的**核心诉求**，所以下面
# 断言的是**事情真的被记下来了**，而不是接口形状。打包版没有控制台，这里是
# 用户唯一的观察窗口。


def _isolated_log(monkeypatch, tmp_path):
    """把执行日志指向临时库 —— core 测试不走 server 那套隔离夹具。"""
    from mp_harvest.core import event_log as el

    store = el.EventLog(tmp_path / "events.db")
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: store)
    return store


def test_call_model_logs_raw_reply(monkeypatch, tmp_path):
    """每次模型调用都要留下**原始返回** —— 排查「模型为什么这么判」全靠它。"""
    store = _isolated_log(monkeypatch, tmp_path)
    monkeypatch.setattr(af, "_post_chat", lambda cfg, payload, timeout=180: "模型的原话")

    assert _call_model(_cfg("m"), "系统提示", "用户内容") == "模型的原话"

    rows = store.list(kind="ai.reply")
    assert len(rows) == 1, "模型返回没有留痕"
    d = rows[0]["data"]
    assert d["reply"] == "模型的原话"
    assert d["model"] == "m" and d["elapsed_ms"] >= 0
    assert d["prompt_chars"] == len("系统提示") and d["input_chars"] == len("用户内容")


def test_log_uses_model_label_when_name_is_empty(monkeypatch, tmp_path):
    """模型没起名时日志要显示**模型 ID** —— 否则就是「模型「模型」返回…」。

    实跑时正是这样：用户配了 deepseek 但没填名称，日志里全是「模型「模型」」，
    等于什么都没说。
    """
    store = _isolated_log(monkeypatch, tmp_path)
    monkeypatch.setattr(af, "_post_chat", lambda cfg, payload, timeout=180: "ok")
    unnamed = ModelConfig(id="x", name="", base_url="https://api.deepseek.com",
                          api_key="k", model="deepseek-v4-flash", format="openai")
    _call_model(unnamed, "S", "U")

    row = store.list(kind="ai.reply")[0]
    assert "deepseek-v4-flash" in row["message"], row["message"]
    assert row["data"]["model"] == "deepseek-v4-flash"
    assert row["data"]["model_id"] == "deepseek-v4-flash"


def test_call_model_logs_http_error_body(monkeypatch, tmp_path):
    """HTTP 错误响应体原先读出来就**直接丢掉**了 —— 它恰恰是排查「模型为什么
    不可用」的唯一线索（key 无效？模型名写错？额度耗尽？都在这个 body 里）。"""
    store = _isolated_log(monkeypatch, tmp_path)

    def boom(cfg, payload, timeout=180):
        raise urllib.error.HTTPError(
            "https://x/v1/chat/completions", 404, "Not Found", {},
            io.BytesIO(b'{"error":{"message":"model not found"}}'),
        )

    monkeypatch.setattr(af, "_post_chat", boom)
    with pytest.raises(RuntimeError):
        _call_model(_cfg("m"), "S", "U", max_retries=1)

    errs = store.list(kind="ai.error")
    assert errs, "模型调用失败没有留痕"
    # 每次尝试各记一条，最后再记一条「重试耗尽」—— 404 那条不是最新的
    with_status = [e for e in errs if e["data"]["status"] == 404]
    assert with_status, f"HTTP 状态码与响应体都没留下：{[e['data'] for e in errs]}"
    assert "model not found" in with_status[0]["data"]["reply"]
    assert any("失败" in e["message"] for e in errs), "重试耗尽也没留痕"


def test_judge_articles_logs_batch_summary(monkeypatch, tmp_path):
    """每批筛选的通过/过滤要留痕 —— 直接对应「用户的一些动作：筛选的结果」。"""
    store = _isolated_log(monkeypatch, tmp_path)
    monkeypatch.setattr(af, "_post_chat", lambda cfg, payload, timeout=180: json.dumps({
        "items": [
            {"idx": 0, "keep": True, "category": "fpga", "relevance_score": 8,
             "technical_depth": 7, "confidence": "high", "reason": "相关"},
            {"idx": 1, "keep": False, "category": "other", "relevance_score": 1,
             "technical_depth": 1, "confidence": "high", "reason": "无关"},
        ]}))

    with tempfile.TemporaryDirectory() as td:
        judge_articles([_art("id1"), _art("id2")], [_cfg("m")], prompt="P",
                       cache_path=Path(td) / "c.json", batch_size=2, workers=1)

    verdicts = store.list(kind="ai.verdict")
    assert verdicts, "筛选批次没有留痕"
    assert "通过 1" in verdicts[0]["message"] and "过滤 1" in verdicts[0]["message"]


def test_logging_failure_does_not_break_the_call(monkeypatch, tmp_path):
    """日志坏了也必须照常返回 —— 埋点在主流程里，绝不能反过来把调用拖垮。"""
    from mp_harvest.core import event_log as el

    class Exploding:
        def write(self, **kw):
            raise RuntimeError("日志库坏了")

    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: Exploding())
    monkeypatch.setattr(af, "_post_chat", lambda cfg, payload, timeout=180: "照常返回")
    assert _call_model(_cfg("m"), "S", "U") == "照常返回"

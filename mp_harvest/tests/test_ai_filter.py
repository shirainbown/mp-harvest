"""AI 过滤引擎纯逻辑测试（不访问真实网络）。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.ai_filter import (  # noqa: E402
    DEFAULT_CONTENT_PRINCIPLES,
    DEFAULT_CONTENT_PROMPT,
    DEFAULT_PRINCIPLES,
    DEFAULT_PROMPT,
    FIXED_OUTPUT_REQUIREMENTS,
    ModelConfig,
    _model_label,
    _build_anthropic_payload,
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
    # 缓存应已落盘（fallback 判定也写入）
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
        assert "id1" in data and "id2" in data


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

    original = ai_filter.urllib.request.urlopen

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

    ai_filter.urllib.request.urlopen = fake_urlopen
    try:
        ok, msg = ai_filter.test_connection(_cfg(base="https://api.deepseek.com"))
    finally:
        ai_filter.urllib.request.urlopen = original

    assert ok is True
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]


def test_fetch_models_ok_and_errors():
    """/models 列表解析 + 401 报错 + anthropic 不支持（2026-08-09 新增）。"""
    import io
    import urllib.error

    from mp_harvest.core import ai_filter

    original = ai_filter.urllib.request.urlopen

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

    ai_filter.urllib.request.urlopen = fake_urlopen
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
        ai_filter.urllib.request.urlopen = original


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

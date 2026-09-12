"""周报生成：选题打分 → 深度解读 → 按模板渲染 → 归档原文。

对应原先手工跑的 `gen_weekly_17.py` 那套脚本，但数据源换成 MP 自己的库
（公众号缓存 + 「其他来源」外部目录），模型走应用里已配置的 AI 模型。

**模板是唯一真相**（2026-09 重做）：旧脚本用正则抠 `<div class="article-card">` 那块，
卡片 HTML 在模板与 Python 里各写一份 —— 用户改模板里的卡片，输出不会变。
现在改用 Jinja2，循环与条件都写在模板里，改什么输出就是什么。
配套三重安全网：未定义变量会被**列名警告**（不是静默空白）、语法错误带行号、
预览模式用缓存数据渲染不花 AI 调用。

**AI 提示词可人工编辑**：四段（打分/深度解读/核心洞察/其他摘要）都能改，
默认值就是旧脚本里的原文。但**输出 JSON 格式由代码固定拼接、不可编辑**
（`FIXED_OUTPUT`）—— 否则用户改标准时删掉「只输出 JSON」，解析就崩了。
这与 `ai_filter` 的「原则 + FIXED_OUTPUT_REQUIREMENTS」是同一套设计。

**缓存随提示词自动失效**：缓存键 = `sha256(该段完整提示词)[:8] + ":" + 文章键`。
改哪段只有哪段失效，改回来还能命中旧缓存 —— 比手工版本号精确。
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import html as html_mod
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

# ── 领域与业务标签（与旧脚本一致，模板注释里也有同一份规范）──────────

DOMAINS = [
    "AI芯片架构与推理优化",
    "FPGA/可编程计算与架构",
    "芯片互联与存储架构",
    "处理器安全与可信架构",
    "半导体制造与先进封装",
]

BUSINESS_TAGS = ["公共", "数通", "传送", "接入", "芯片硬件"]

TAG_COLORS = {
    "公共": "#27ae60",
    "数通": "#e74c3c",
    "传送": "#9b59b6",
    "接入": "#3498db",
    "芯片硬件": "#f39c12",
}


# ── 四段可编辑提示词（默认值 = 旧脚本原文）──────────────────────────

DEFAULT_SCORING = """你是芯片与半导体产业技术分析师。请逐篇评估以下文章/论文，重点考察硬件/架构/工艺层面的实质创新，并按五类之一归档领域。

评分维度参考：
- 硬件/架构/工艺层面的实质创新性（这是主要权重）
- 是否有可验证的量化结果或工程实现
- 与半导体/芯片硬件技术的相关性（纯软件、纯市场新闻不相关）
- 业务领域归属（公共 / 数通 / 传送 / 接入 / 芯片硬件）

入选理由要具体：写明关键数据或创新点，不要写「值得一读」这类空话。"""

DEFAULT_DETAIL = """你是芯片与半导体领域技术专家。请深度解读以下文章/论文：

- 关键技术创新：100-150 字，分号分隔多个要点，讲清楚「做了什么、怎么做的、相比已有方案强在哪」
- 数据与实验结果：列出文中关键量化指标，格式「数值: 含义；数值: 含义」；
  没有量化数据时写「文中未给出量化数据，以定性分析为主」，不要编造数字
- 详细摘要：200-300 字，按「背景 → 方法 → 结果 → 意义」组织"""

DEFAULT_INTRO = """你是芯片与半导体产业技术战略专家。基于本期精选文章的标题、领域与创新点，撰写一段 200-300 字的中文「核心洞察与摘要」作为周报开篇。

要求：
- 开头点明本期的时间范围与精选篇数，以及覆盖了哪些技术方向
- 随后用 ① ② ③ ④ ⑤ 分方向列出 3-5 条看点，每条点出代表性成果与关键数据
- 每条看点单独一行，点与点之间不要空行
- 不要使用 Markdown 加粗（**），标题与正文都写纯文本"""

DEFAULT_BRIEF = """请为以下每篇文章/论文写一句 50-100 字的中文摘要，突出方法创新与关键量化数据。

要求：
- 一句话讲清楚做了什么、关键结果是什么
- 有量化数据就写进去；没有就不要编
- 不要写「本文研究了」这类填充语，直接说内容"""

_PROMPT_DEFAULTS: dict[str, str] = {
    "scoring": DEFAULT_SCORING,
    "detail": DEFAULT_DETAIL,
    "intro": DEFAULT_INTRO,
    "brief": DEFAULT_BRIEF,
}

PROMPT_KEYS = tuple(_PROMPT_DEFAULTS)
PROMPT_LABELS = {
    "scoring": "选题打分",
    "detail": "深度解读",
    "intro": "核心洞察",
    "brief": "其他入选摘要",
}

# 代码固定的输出约束：**不可编辑**，永远拼在用户文本之后。
# 与 ai_filter.FIXED_OUTPUT_REQUIREMENTS 同一用意 —— 用户改标准不会把解析改崩。
FIXED_OUTPUT: dict[str, str] = {
    "scoring": """【输出格式（软件固定，不可更改）】
只输出严格 JSON 对象（不要 Markdown 代码块、不要任何多余文字）：
{"items":[{"idx":0,"score":8.5,"semiconductor":true,"title_cn":"中文标题","domain":"五选一领域","business_tags":["公共"],"reason":"30-60字入选理由"}]}
要求：
- idx 必须与输入编号一一对应，不能漏项、不能改序；
- score 为 1-10 浮点，可一位小数；
- semiconductor 为 true/false，纯软件/纯市场新闻为 false；
- domain 必须严格取自这五个之一：AI芯片架构与推理优化 / FPGA/可编程计算与架构 / 芯片互联与存储架构 / 处理器安全与可信架构 / 半导体制造与先进封装；
- business_tags 取自 ["公共","数通","传送","接入","芯片硬件"]，1-3 个；
- title_cn：英文标题给准确中文译名，中文标题原样返回。""",
    "detail": """【输出格式（软件固定，不可更改）】
只输出严格 JSON 对象（不要 Markdown 代码块、不要任何多余文字）：
{"key_innovation":"100-150字，分号分隔多个要点","data_results":"数值: 含义；数值: 含义","summary":"200-300字"}""",
    # 核心洞察要的是成稿文字而不是 JSON —— 交给模板前由代码清洗成 HTML
    "intro": """【输出格式（软件固定，不可更改）】
只输出这段中文正文本身，不要 JSON、不要 Markdown、不要任何解释性前后缀。""",
    "brief": """【输出格式（软件固定，不可更改）】
只输出严格 JSON 对象（不要 Markdown 代码块、不要任何多余文字）：
{"items":[{"idx":0,"brief":"50-100字中文摘要"}]}
要求：idx 必须与输入编号一一对应，不能漏项、不能改序。""",
}


def build_prompt(key: str, text: str | None = None) -> str:
    """完整提示词 = 用户可编辑标准 + 软件固定输出约束。"""
    body = (text if text is not None else _PROMPT_DEFAULTS.get(key, "")).strip()
    if not body:
        body = _PROMPT_DEFAULTS.get(key, "")
    return f"{body}\n\n{FIXED_OUTPUT.get(key, '')}".strip()


def prompt_fingerprint(key: str, text: str | None = None) -> str:
    """提示词指纹：缓存键的前缀。改一个字就换一个键 —— 该阶段缓存自动失效。"""
    return hashlib.sha256(build_prompt(key, text).encode("utf-8")).hexdigest()[:8]


# ── 提示词持久化 ──────────────────────────────────────────────────


def load_prompts(path: str | Path) -> dict[str, str]:
    """读用户自定义提示词；缺失/损坏的键回落到内置默认（绝不抛异常）。"""
    out = dict(_PROMPT_DEFAULTS)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return out
    if not isinstance(raw, dict):
        return out
    for k in PROMPT_KEYS:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v
    return out


def save_prompts(path: str | Path, prompts: dict[str, str]) -> bool:
    """原子写；只保留认识的键。"""
    p = Path(path)
    keep = {k: str(prompts.get(k) or "") for k in PROMPT_KEYS}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception:  # noqa: BLE001
        return False


def prompts_payload(path: str | Path) -> dict[str, dict[str, str]]:
    """给前端：每段的当前文本 + 内置默认（供「恢复默认」）。"""
    current = load_prompts(path)
    return {
        k: {"text": current.get(k, ""), "default": _PROMPT_DEFAULTS[k], "label": PROMPT_LABELS[k]}
        for k in PROMPT_KEYS
    }


# ── 模型调用（严格 JSON）──────────────────────────────────────────


def _extract_json(text: str) -> Any:
    """从模型回复里抠 JSON：剥代码围栏，容忍前置废话。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S).strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass
    # 容错：从第一个 { / [ 取到最后一个 } / ]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i, j = s.find(open_ch), s.rfind(close_ch)
        if i != -1 and j > i:
            try:
                return json.loads(s[i : j + 1])
            except Exception:  # noqa: BLE001
                continue
    raise ValueError("模型回复里找不到可解析的 JSON")


def llm_json(
    cfg: Any,
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int = 6000,
    timeout: float = 300,
    retries: int = 1,
) -> Any:
    """调用模型并解析 JSON；解析失败时补一句「只输出 JSON」重试一次。

    传输层复用 ``ai_filter._call_model``（它自带的 HTTP 重试/限流退避照旧生效），
    这里只负责「拿到的文本 → 结构化数据」这一段。
    """
    from mp_harvest.core import ai_filter as ai_mod

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        prompt = system_prompt
        if attempt:
            prompt = prompt + "\n\n【重要】上一次回复无法解析。请只输出 JSON 本身，不要任何解释或代码块。"
        text = ai_mod._call_model(cfg, prompt, user_content, max_tokens=max_tokens, timeout=timeout)
        try:
            return _extract_json(text)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"模型「{getattr(cfg, 'name', '')}」返回的内容无法解析为 JSON：{last_err}")


def _map_parallel(
    items: list[dict[str, Any]],
    models: list[Any],
    work: Callable[[dict[str, Any], Any], Any],
    *,
    workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    on_result: Callable[[dict[str, Any], Any], None] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """并发逐篇调用；模型轮询分配（多模型时天然负载均衡）。

    返回 ``({key: 结果}, [错误信息])``。单篇失败**不影响其余篇目** ——
    计进 errors 后继续，调用方按需上报。
    """
    enabled = [m for m in models if getattr(m, "enabled", True)] or list(models)
    if not enabled:
        raise RuntimeError("没有可用的 AI 模型，请先到「AI 模型」页配置并启用")
    results: dict[str, Any] = {}
    errors: list[str] = []
    done = 0
    total = len(items)

    def _guarded(it: dict[str, Any], model: Any) -> Any:
        if check_cancelled:
            check_cancelled()
        return work(it, model)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futures = {
            ex.submit(_guarded, it, enabled[i % len(enabled)]): it for i, it in enumerate(items)
        }
        for fut in concurrent.futures.as_completed(futures):
            it = futures[fut]
            try:
                val = fut.result()
                results[it["key"]] = val
                if on_result:
                    on_result(it, val)
            except Exception as exc:  # noqa: BLE001
                # 取消要原样抛出（TaskCancelled），其余按单篇失败处理
                if exc.__class__.__name__ == "TaskCancelled":
                    raise
                errors.append(f"{str(it.get('title') or '')[:40]}：{exc}")
            done += 1
            if on_progress:
                on_progress(done, total)
    return results, errors


# ── 候选收集 ──────────────────────────────────────────────────────


def normalize_wechat(row: dict[str, Any], *, source_name: str = "") -> dict[str, Any] | None:
    """公众号缓存行 → 候选。缺标题的丢掉。"""
    title = str(row.get("title") or "").strip()
    link = str(row.get("link") or "").strip()
    if not title and not link:
        return None
    ts = int(row.get("publish_ts") or 0)
    return {
        # 键必须有兜底：真实缓存行都带 identity，但缺了它又缺 link 时会算出
        # "wechat:" 这种空键，多篇候选会撞成同一条（2026-09 修复）
        "key": f"wechat:{row.get('identity') or link or _title_fingerprint(title)}",
        "kind": "公众号",
        "title": title or "(无标题)",
        "source": source_name or str(row.get("account") or ""),
        "date": _date_of(ts, str(row.get("publish_at") or "")),
        "publish_ts": ts,
        "publish_at": str(row.get("publish_at") or ""),
        "url": link,
        "text": str(row.get("body_text") or row.get("digest") or "").strip(),
        "body_html": str(row.get("body_html") or ""),
        "body_text": str(row.get("body_text") or ""),
    }


def normalize_external(item: dict[str, Any]) -> dict[str, Any] | None:
    """外部来源条目 → 候选。正文优先用本地正文文件，退回中文摘要/英文摘要。"""
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    if not title and not url:
        return None
    ts = int(item.get("publish_ts") or 0)
    authors = item.get("authors") or []
    first_author = str(authors[0]) if authors else ""
    body = read_external_body(item)
    return {
        "key": f"ext:{item.get('item_key') or url or _title_fingerprint(title)}",
        "kind": "arXiv" if item.get("arxiv_id") else "外部",
        "arxiv_id": str(item.get("arxiv_id") or ""),
        "title": title or "(无标题)",
        "title_cn": str(item.get("title_cn") or ""),
        "source": ("arXiv · " + first_author) if first_author else str(item.get("source_name") or ""),
        "date": _date_of(ts, str(item.get("dir_date") or "")),
        "publish_ts": ts,
        "publish_at": str(item.get("dir_date") or ""),
        "url": url,
        "text": body,
        "body_html": "",
        "body_text": body,
    }


def read_external_body(item: dict[str, Any]) -> str:
    """外部条目的正文：本地正文文件 → summary_cn → abstract。

    正文文件是 MP 自己写的自包含 HTML（内容是摘要），所以按 HTML 取文本；
    取不到就退回数据库里的摘要字段。
    """
    body_path = str(item.get("body_path") or "")
    if body_path:
        try:
            from mp_harvest.core.article_reader import _html_to_text

            text = _html_to_text(Path(body_path).read_text(encoding="utf-8", errors="ignore"))
            if text.strip():
                return text.strip()
        except Exception:  # noqa: BLE001
            pass
    return str(item.get("summary_cn") or item.get("abstract") or "").strip()


def _title_fingerprint(title: str) -> str:
    """候选键的兜底：标题摘要（identity/链接都缺时不至于让多篇撞成一条）。"""
    return "t" + hashlib.sha1(str(title or "").encode("utf-8", "ignore")).hexdigest()[:12]


def _date_of(ts: int, fallback: str) -> str:
    if ts:
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(fallback or ""))
    return m.group(1) if m else ""


def collect_candidates(
    *,
    wechat_rows: Iterable[tuple[dict[str, Any], str]] = (),
    external_items: Iterable[dict[str, Any]] = (),
    start_ts: int = 0,
    end_ts: int = 0,
) -> list[dict[str, Any]]:
    """按日期窗口收集候选并去重（同 key 只留一条）。

    ``wechat_rows`` 是 ``(文章行, 公众号名)`` 的序列 —— 由调用方从
    ``server.state`` 取（core 不能 import server）；外部条目同理。
    """
    out: dict[str, dict[str, Any]] = {}
    for row, name in wechat_rows:
        c = normalize_wechat(row, source_name=name)
        if c and _in_window(c["publish_ts"], start_ts, end_ts):
            out.setdefault(c["key"], c)
    for item in external_items:
        c = normalize_external(item)
        if c and _in_window(c["publish_ts"], start_ts, end_ts):
            out.setdefault(c["key"], c)
    return sorted(out.values(), key=lambda c: c["publish_ts"], reverse=True)


def _in_window(ts: int, start_ts: int, end_ts: int) -> bool:
    if start_ts and ts and ts < start_ts:
        return False
    if end_ts and ts and ts > end_ts:
        return False
    return True


# ── 缓存 ──────────────────────────────────────────────────────────


def default_cache_path() -> Path:
    from mp_harvest.infra.platform import paths

    return paths.data_dir() / "weekly" / "cache.json"


class WeeklyCache:
    """打分/解读结果的持久缓存（容错：任何故障都不阻断生成）。

    键 = ``提示词指纹 + ":" + 文章键``，所以改提示词只有对应阶段失效。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {"scores": {}, "details": {}, "briefs": {}}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for stage in ("scores", "details", "briefs"):
                    if isinstance(raw.get(stage), dict):
                        self._data[stage] = raw[stage]
        except Exception:  # noqa: BLE001
            pass

    def get(self, stage: str, key: str) -> Any:
        with self._lock:
            return self._data.get(stage, {}).get(key)

    def put(self, stage: str, key: str, value: Any) -> None:
        with self._lock:
            self._data.setdefault(stage, {})[key] = value
            self._flush()

    def prune_stage(self, stage: str, keep_prefix: str) -> int:
        """丢掉某阶段里不属于当前提示词指纹的旧条目。

        用户每改一次提示词就换一个指纹，旧条目再也不会被命中 —— 不清的话
        缓存文件会随改动次数无限膨胀。返回清掉的条数。
        """
        with self._lock:
            rows = self._data.get(stage) or {}
            stale = [k for k in rows if not k.startswith(f"{keep_prefix}:")]
            for k in stale:
                rows.pop(k, None)
            if stale:
                self._flush()
            return len(stale)

    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception:  # noqa: BLE001
            pass


def cache_key(stage: str, prompt_text: str | None, article_key: str) -> str:
    return f"{prompt_fingerprint(stage, prompt_text)}:{article_key}"


# ── 五个阶段 ──────────────────────────────────────────────────────


def score_candidates(
    items: list[dict[str, Any]],
    models: list[Any],
    *,
    prompts: dict[str, str],
    cache: WeeklyCache,
    workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """逐篇打分：评分 / 半导体相关性 / 中文译名 / 领域 / 业务标签 / 入选理由。"""
    system = build_prompt("scoring", prompts.get("scoring"))
    pending = []
    cached: dict[str, Any] = {}
    for it in items:
        k = cache_key("scoring", prompts.get("scoring"), it["key"])
        hit = cache.get("scores", k)
        if hit is not None:
            cached[it["key"]] = hit
        else:
            pending.append((it, k))

    def work(it: dict[str, Any], model: Any) -> dict[str, Any]:
        user = (
            f"类型:{it['kind']} 来源:{it['source']} 日期:{it['date']}\n"
            f"标题: {it['title']}\n正文/摘要: {it['text'][:700] or '（无正文）'}"
        )
        data = llm_json(model, system, user, max_tokens=4000)
        # 兼容「包一层 items」与「直接给单对象」两种写法
        rec: dict[str, Any] = {}
        if isinstance(data, dict) and isinstance(data.get("items"), list) and data["items"]:
            rec = data["items"][0]
        elif isinstance(data, dict):
            rec = data
        score = _as_float(rec.get("score"), 0.0)
        tags = [t for t in (rec.get("business_tags") or []) if t in BUSINESS_TAGS][:3] or ["公共"]
        domain = str(rec.get("domain") or "")
        return {
            "score": score,
            "semiconductor": _as_bool(rec.get("semiconductor"), True),
            "title_cn": str(rec.get("title_cn") or it["title"]),
            "domain": domain if domain in DOMAINS else DOMAINS[0],
            "business_tags": tags,
            "reason": str(rec.get("reason") or "")[:140],
        }

    fresh, errors = _map_parallel(
        [it for it, _ in pending],
        models,
        work,
        workers=workers,
        on_progress=on_progress,
        check_cancelled=check_cancelled,
        on_result=lambda it, val: cache.put(
            "scores", cache_key("scoring", prompts.get("scoring"), it["key"]), val
        ),
    )
    merged = {**cached, **fresh}
    return merged, errors


def analyze_selected(
    selected: list[dict[str, Any]],
    models: list[Any],
    *,
    prompts: dict[str, str],
    cache: WeeklyCache,
    workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Top N 逐篇深度解读：关键技术创新 / 数据与实验结果 / 详细摘要。"""
    system = build_prompt("detail", prompts.get("detail"))
    pending = []
    cached: dict[str, Any] = {}
    for it in selected:
        k = cache_key("detail", prompts.get("detail"), it["key"])
        hit = cache.get("details", k)
        if hit is not None:
            cached[it["key"]] = hit
        else:
            pending.append((it, k))

    def work(it: dict[str, Any], model: Any) -> dict[str, Any]:
        user = (
            f"标题: {it['title']}\n来源: {it['source']}（{it['kind']}）\n\n"
            f"正文/摘要:\n{it['text'][:6000] or '（无正文，请基于标题做谨慎推断并说明依据有限）'}"
        )
        data = llm_json(model, system, user, max_tokens=6000)
        if not isinstance(data, dict):
            raise ValueError("解读结果不是 JSON 对象")
        return {
            "key_innovation": str(data.get("key_innovation") or ""),
            "data_results": str(data.get("data_results") or ""),
            "summary": str(data.get("summary") or ""),
        }

    fresh, errors = _map_parallel(
        [it for it, _ in pending],
        models,
        work,
        workers=workers,
        on_progress=on_progress,
        check_cancelled=check_cancelled,
        on_result=lambda it, val: cache.put(
            "details", cache_key("detail", prompts.get("detail"), it["key"]), val
        ),
    )
    return {**cached, **fresh}, errors


def brief_others(
    others: list[dict[str, Any]],
    models: list[Any],
    *,
    prompts: dict[str, str],
    cache: WeeklyCache,
    workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """其余入选文章的一句话摘要（50-100 字）。"""
    system = build_prompt("brief", prompts.get("brief"))
    pending = []
    cached: dict[str, str] = {}
    for it in others:
        k = cache_key("brief", prompts.get("brief"), it["key"])
        hit = cache.get("briefs", k)
        if isinstance(hit, str) and hit.strip():
            cached[it["key"]] = hit
        else:
            pending.append((it, k))

    def work(it: dict[str, Any], model: Any) -> str:
        user = f"标题: {it['title']}\n正文/摘要: {it['text'][:600] or '（无正文）'}"
        data = llm_json(model, system, user, max_tokens=2000)
        rec: dict[str, Any] = {}
        if isinstance(data, dict) and isinstance(data.get("items"), list) and data["items"]:
            rec = data["items"][0]
        elif isinstance(data, dict):
            rec = data
        out = str(rec.get("brief") or "").strip()
        if not out:
            raise ValueError("摘要为空")
        return out

    fresh, errors = _map_parallel(
        [it for it, _ in pending],
        models,
        work,
        workers=workers,
        on_progress=on_progress,
        check_cancelled=check_cancelled,
        on_result=lambda it, val: cache.put(
            "briefs", cache_key("brief", prompts.get("brief"), it["key"]), val
        ),
    )
    return {**cached, **fresh}, errors


def generate_intro(
    selected: list[dict[str, Any]],
    scores: dict[str, Any],
    details: dict[str, Any],
    models: list[Any],
    *,
    prompts: dict[str, str],
    from_date: str,
    to_date: str,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """本期核心洞察（① ② ③ 编号式）。失败时退回规则拼接的兜底文案。"""
    enabled = [m for m in models if getattr(m, "enabled", True)] or list(models)
    lines = []
    for k, it in enumerate(selected, 1):
        s = scores.get(it["key"]) or {}
        d = details.get(it["key"]) or {}
        lines.append(
            f"{k}. [{s.get('domain', '')}] {s.get('title_cn') or it['title']}："
            f"{str(d.get('key_innovation') or '')[:60]}"
        )
    if on_progress:
        on_progress("生成核心洞察…")
    try:
        if not enabled:
            raise RuntimeError("没有可用的 AI 模型")
        from mp_harvest.core import ai_filter as ai_mod

        system = build_prompt("intro", prompts.get("intro"))
        user = (
            f"本期时间范围：{from_date} 至 {to_date}\n"
            f"精选 {len(selected)} 篇，清单如下：\n" + "\n".join(lines)
        )
        # 核心洞察要的是**成稿文字**，不是 JSON —— 走 llm_json 会永远解析失败、
        # 永远落到兜底文案（2026-09 由测试抓出）。这里直接取文本。
        text = ai_mod._call_model(enabled[0], system, user, max_tokens=4000, timeout=300)
        cleaned = _clean_intro(text)
        return cleaned or _fallback_intro(selected, scores, from_date, to_date)
    except Exception:  # noqa: BLE001
        return _fallback_intro(selected, scores, from_date, to_date)


def _fallback_intro(
    selected: list[dict[str, Any]], scores: dict[str, Any], from_date: str, to_date: str
) -> str:
    """模型不可用时的兜底：按领域拼接，至少不是空白。"""
    doms = sorted({(scores.get(it["key"]) or {}).get("domain", "") for it in selected} - {""})
    return (
        f"本期（{from_date} 至 {to_date}）精选 {len(selected)} 篇，"
        f"覆盖{'、'.join(doms)}等方向。"
    )


def _clean_intro(text: Any) -> str:
    """核心洞察清洗：去 Markdown 加粗、折叠多余空行、\\n → <br>（模板里 | safe）。"""
    s = str(text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s, flags=re.S).strip()
    s = s.replace("**", "").replace("\r\n", "\n")
    s = re.sub(r"\n\s*\n+", "\n", s)
    return html_mod.escape(s).replace("\n", "<br>\n")


# ── 渲染 ──────────────────────────────────────────────────────────



def _make_undefined(sink: set[str]):
    import jinja2

    class _Undefined(jinja2.Undefined):
        """记录所有「模板引用了但上下文没给」的名字。

        Jinja 默认把未定义变量渲染成空串 —— 用户把 `{{ARTICLE_NO}}` 敲成
        `{{ARTILCE_NO}}` 时页面只是默默少一块，很难发现。这里收集起来在渲染完成后
        作为警告返回，让模板改动有明确的反馈。
        """

        def _note(self) -> str:
            name = str(self._undefined_name or "")
            if name:
                sink.add(name)
            return ""

        def __str__(self) -> str:  # type: ignore[override]
            return self._note()

        def __iter__(self):  # type: ignore[override]
            self._note()
            return iter(())

        def __bool__(self) -> bool:
            self._note()
            return False

        def __len__(self) -> int:  # type: ignore[override]
            self._note()
            return 0

    return _Undefined


def _make_env(template_dir: Path):
    import jinja2

    sink: set[str] = set()
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_dir)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=_make_undefined(sink),
    )
    env.filters["tag_spans"] = _tag_spans
    env.filters["nl2br"] = _nl2br
    return env, sink


def _tag_spans(tags: Any) -> Any:
    """业务标签 → 彩色 span（颜色规范见模块常量；模板不用手写内联样式）。"""
    from markupsafe import Markup

    if isinstance(tags, str):
        tags = [tags]
    parts = []
    for t in tags or []:
        name = str(t)
        color = TAG_COLORS.get(name)
        if not color:
            continue
        parts.append(
            f'<span style="background-color:{color}; color:#ffffff; font-size:11px; '
            f'font-weight:bold; padding:1px 6px; line-height:1.5;">{html_mod.escape(name)}</span>'
        )
    return Markup("&nbsp;".join(parts))


def _nl2br(text: Any) -> Any:
    from markupsafe import Markup

    s = html_mod.escape(str(text or ""))
    return Markup(s.replace("\r\n", "\n").replace("\n", "<br>\n"))


BUILTIN_TEMPLATE_NAME = "weekly.html"


def resolve_template_dir() -> Path:
    """定位内置模板目录（兼容 PyInstaller 各布局）。

    自带一份探测，而不是复用 ``article_reader._resolve_template_dir``：
    那个函数以 ``article.html`` 为哨兵、又是私有函数，周报模板与它只是「恰好
    同目录」—— 一旦哪天挪了位置或改了哨兵，周报会以一句 ImportError 崩掉。
    """
    import sys

    here = Path(__file__).resolve().parent
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(here)
    for r in roots:
        for cand in (r / "templates", r / "mp_harvest" / "core" / "templates"):
            if (cand / BUILTIN_TEMPLATE_NAME).is_file():
                return cand
    return here / "templates"


def render_report(
    context: dict[str, Any],
    *,
    template_path: str | Path | None = None,
) -> dict[str, Any]:
    """按模板渲染周报。

    ``template_path`` 为空用内置模板（``core/templates/weekly.html``）；
    否则加载用户任意路径的模板文件。

    返回 ``{ok, html, missing, error}``：``missing`` 是模板引用了但上下文没提供的
    变量名（拼写错误会在这里暴露，而不是静默渲染成空白）。
    """
    path = str(template_path or "").strip()
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            return {"ok": False, "html": "", "missing": [], "error": f"模板文件不存在：{p}"}
        tdir, tname = p.parent, p.name
    else:
        tdir, tname = resolve_template_dir(), BUILTIN_TEMPLATE_NAME
        if not (Path(tdir) / tname).is_file():
            return {
                "ok": False, "html": "", "missing": [],
                "error": f"内置模板缺失：{Path(tdir) / tname}",
            }

    try:
        env, sink = _make_env(Path(tdir))
        tpl = env.get_template(tname)
        html_out = tpl.render(**context)
    except Exception as exc:  # noqa: BLE001
        err = f"{exc.__class__.__name__}: {exc}"
        lineno = getattr(exc, "lineno", None)
        if lineno:
            err = f"模板第 {lineno} 行：{getattr(exc, 'message', exc)}"
        return {"ok": False, "html": "", "missing": [], "error": err}

    return {"ok": True, "html": html_out, "missing": sorted(sink), "error": ""}


# ── 上下文组装 ────────────────────────────────────────────────────


def build_context(
    *,
    issue_num: int,
    from_date: str,
    to_date: str,
    selected: list[dict[str, Any]],
    others: list[dict[str, Any]],
    scores: dict[str, Any],
    details: dict[str, Any],
    briefs: dict[str, str],
    intro: str,
    total: int,
    wechat_count: int,
    arxiv_count: int,
    archive_rel: dict[str, str] | None = None,
    org: dict[str, str] | None = None,
    title: str = "逻辑芯片行业洞察快报",
) -> dict[str, Any]:
    """组装模板上下文。字段名即模板契约，改动需同步更新内置模板的说明注释。"""
    rel = archive_rel or {}
    org = org or {}

    def _article(it: dict[str, Any], no: int) -> dict[str, Any]:
        s = scores.get(it["key"]) or {}
        d = details.get(it["key"]) or {}
        title_cn = s.get("title_cn") or it.get("title_cn") or it["title"]
        return {
            "no": no,
            "title": it["title"],
            "title_cn": title_cn,
            "title_en": it["title"] if it["title"] != title_cn else "",
            "source": it["source"],
            "kind": it["kind"],
            "date": it["date"],
            "url": it["url"],
            "domain": s.get("domain", ""),
            "business_tags": s.get("business_tags") or [],
            "reason": s.get("reason", ""),
            "key_innovation": d.get("key_innovation", ""),
            "data_results": d.get("data_results", ""),
            "summary": d.get("summary", ""),
            "brief": briefs.get(it["key"], ""),
            "score": s.get("score", 0),
            "local_file": rel.get(it["key"], ""),
        }

    sel = [_article(it, i) for i, it in enumerate(selected, 1)]
    oth = [_article(it, i) for i, it in enumerate(others, len(selected) + 1)]
    return {
        "issue": {
            "num": issue_num,
            "from_date": from_date,
            "to_date": to_date,
            "generated_at": time.strftime("%Y-%m-%d %H:%M"),
            "title": title,
        },
        "stats": {
            "wechat": wechat_count,
            "arxiv": arxiv_count,
            "total": total,
            "selected_count": len(sel),
            "other_count": len(oth),
        },
        "intro": intro,
        "selected": sel,
        "others": oth,
        "tags": dict(TAG_COLORS),
        "org_name": org.get("name", ""),
        "org_email": org.get("email", ""),
        "archive_url": org.get("archive_url", ""),
    }


# ── 小工具 ────────────────────────────────────────────────────────


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return default


def _as_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y"):
            return True
        if s in ("false", "0", "no", "n", ""):
            return False
    if v is None:
        return default
    try:
        return bool(v)
    except Exception:  # noqa: BLE001
        return default


def suggest_issue_number(out_dir: str | Path, default: int = 1) -> int:
    """扫描输出目录里已有的「第N期_…」子目录，返回最大期号 + 1。"""
    root = Path(str(out_dir)).expanduser()
    best = 0
    try:
        if root.is_dir():
            for p in root.iterdir():
                m = re.match(r"第(\d+)期", p.name)
                if m and p.is_dir():
                    best = max(best, int(m.group(1)))
    except Exception:  # noqa: BLE001
        pass
    return best + 1 if best else default


def issue_dirname(issue_num: int, to_date: str) -> str:
    """归档目录名：``第17期_2026-09-07``。"""
    return f"第{int(issue_num)}期_{to_date}"


# ── 归档 ──────────────────────────────────────────────────────────


def _text_to_html(text: str) -> str:
    """纯文本 → 段落 HTML。

    外部来源的条目只有摘要文本，没有正文片段；直接交给 ``render_article_html``
    会落进它的 ``<pre>`` 兜底分支（长段落不折行、观感差）。按空行切段落更接近
    公众号文章的排版，归档页看起来才一致。
    """
    out = []
    for block in re.split(r"\n\s*\n+", str(text or "").strip()):
        block = block.strip()
        if block:
            out.append(f"<p>{html_mod.escape(block).replace(chr(10), '<br>')}</p>")
    return "".join(out)


def archive_article(
    candidate: dict[str, Any],
    articles_dir: Path,
    *,
    meta: dict[str, Any],
    download_images: bool = False,
) -> Path:
    """把一篇候选的原文写进归档目录，返回文件路径。

    公众号与外部来源通吃：``render_article_html`` 只认
    ``title/link/publish_at/body_html/body_text`` 五个键。
    """
    from mp_harvest.core.article_reader import (
        _article_content_hash,
        safe_export_filename,
        write_article_export,
    )

    title = str(meta.get("title_cn") or candidate["title"])
    link = candidate.get("url") or ""
    arxiv_id = str(candidate.get("arxiv_id") or "")
    if arxiv_id:
        # arXiv 条目按编号命名，与「其他来源」目录里 {arxiv_id}.pdf 的习惯一致
        stem = re.sub(r'[\\/:*?"<>|\s]+', "_", arxiv_id)
        fname = f"{stem}.html"
    else:
        fname = safe_export_filename(
            title,
            ext="html",
            date=candidate.get("date", ""),
            account=candidate.get("source", ""),
            content_hash=_article_content_hash(link=link, body_text=candidate.get("body_text", "")),
        )
    art = {
        "title": title,
        "link": link,
        "publish_at": str(candidate.get("publish_at") or candidate.get("date") or ""),
        "account": candidate.get("source", ""),
        "body_html": str(candidate.get("body_html") or "")
        or _text_to_html(str(candidate.get("body_text") or "")),
        "body_text": str(candidate.get("body_text") or ""),
    }
    return write_article_export(
        articles_dir / fname,
        art,
        account=candidate.get("source", ""),
        download_images=download_images,
        assets_dir=articles_dir.parent / "assets",
        assets_rel="../assets",
    )


def issue_root(out_dir: str | Path, issue_num: int, to_date: str) -> Path:
    return Path(str(out_dir)).expanduser() / issue_dirname(issue_num, to_date)


def archive_articles(
    candidates: list[dict[str, Any]],
    root: Path,
    *,
    titles: dict[str, str] | None = None,
    reasons: dict[str, str] | None = None,
    download_images: bool = False,
    on_progress: Callable[[str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """把候选原文逐篇写进 ``<root>/articles/``。单篇失败不阻断整期。

    **必须在渲染周报之前调用** —— 周报里要带指向本地全文的链接（``local_file``），
    而文件名要等归档才知道。

    返回 ``{rel_map, index_rows, dir, archived, errors}``：
    - ``rel_map`` 的路径**相对 root**（供周报正文链接）
    - ``index_rows`` 的 ``file`` **相对 articles/**（供目录页链接，它就住在那一层）
    """
    articles_dir = root / "articles"
    titles = titles or {}
    reasons = reasons or {}
    errors: list[str] = []
    rel_map: dict[str, str] = {}
    index_rows: list[dict[str, Any]] = []
    try:
        articles_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return {"rel_map": {}, "index_rows": [], "dir": articles_dir,
                "archived": 0, "errors": [f"无法创建归档目录：{exc}"]}

    for it in candidates:
        if check_cancelled:
            check_cancelled()
        title_cn = titles.get(it["key"]) or it.get("title_cn") or it["title"]
        meta = {"title_cn": title_cn}
        try:
            path = archive_article(it, articles_dir, meta=meta, download_images=download_images)
            rel_map[it["key"]] = path.relative_to(root).as_posix()
            index_rows.append(
                {
                    "title": title_cn,
                    "publish_at": it.get("publish_at") or it.get("date") or "",
                    "publish_ts": int(it.get("publish_ts") or 0),
                    "account": it.get("source", ""),
                    # 目录页就在 articles/ 里，链接必须相对它自己 ——
                    # 写成相对 root 的 articles/xxx.html 会 404（2026-09 修复）
                    "file": path.relative_to(articles_dir).as_posix(),
                    "link": it.get("url", ""),
                    "keep": True,
                    "reason": reasons.get(it["key"], ""),
                }
            )
            if on_progress:
                on_progress(f"归档 {str(title_cn)[:24]}")
        except Exception as exc:  # noqa: BLE001
            # 无链接 / 正文取不到：记一笔继续，周报正文不依赖这一步
            errors.append(f"{str(it.get('title'))[:40]}：{exc}")

    return {
        "rel_map": rel_map,
        "index_rows": index_rows,
        "dir": articles_dir,
        "archived": len(rel_map),
        "errors": errors,
    }


def write_report_outputs(
    root: Path,
    *,
    report_html: str,
    context: dict[str, Any],
    scores: dict[str, Any],
    details: dict[str, Any],
    briefs: dict[str, str],
    index_rows: list[dict[str, Any]],
    issue_num: int,
    report_title: str = "逻辑芯片行业洞察快报",
) -> dict[str, Any]:
    """写周报正文 + 数据快照 + 归档目录页。"""
    from mp_harvest.core.article_reader import _render_index_page

    errors: list[str] = []
    data_dir = root / "data"
    report_path = root / f"{report_title}_第{int(issue_num)}期.html"
    try:
        root.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_html, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "report_path": str(report_path), "errors": [f"周报写入失败：{exc}"]}

    # 目录页：直接调 _render_index_page（不走 _write_index_from_records ——
    # 那条路从 SQLite 取行会把 publish_at 置空，日期列全是空白）
    try:
        (root / "articles" / "index.html").write_text(
            _render_index_page(index_rows, account_name=f"第{int(issue_num)}期 · 文章归档"),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"归档目录页生成失败：{exc}")

    # 数据快照：改模板后可直接拿 report.json 重渲染，不必重跑 AI
    try:
        (data_dir / "report.json").write_text(
            json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (data_dir / "scores.json").write_text(
            json.dumps({"scores": scores, "details": details, "briefs": briefs},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"数据快照写入失败：{exc}")

    return {"ok": True, "report_path": str(report_path), "errors": errors}


def load_saved_context(issue_dir: str | Path) -> dict[str, Any] | None:
    """读回某期归档的渲染上下文（供「改模板后重渲染」，不调 AI）。"""
    p = Path(str(issue_dir)).expanduser() / "data" / "report.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


# ── 整期编排 ──────────────────────────────────────────────────────


def generate_issue(
    *,
    candidates: list[dict[str, Any]],
    models: list[Any],
    prompts: dict[str, str],
    cache: WeeklyCache,
    out_dir: str | Path,
    issue_num: int,
    from_date: str,
    to_date: str,
    selected_count: int = 15,
    org: dict[str, str] | None = None,
    report_title: str = "逻辑芯片行业洞察快报",
    template_path: str | Path | None = None,
    download_images: bool = False,
    workers: int = 4,
    on_stage: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """跑完整一期：打分 → 选题 → 归档 → 解读 → 渲染 → 落盘。

    **先归档后渲染**：周报里要带指向本地全文的链接，文件名得先落地才知道。
    """
    stage = on_stage or (lambda _m: None)
    n = max(1, int(selected_count))

    stage(f"打分 {len(candidates)} 篇候选…")
    scores, score_errors = score_candidates(
        candidates, models, prompts=prompts, cache=cache, workers=workers,
        on_progress=on_progress, check_cancelled=check_cancelled,
    )

    valid = [c for c in candidates if (scores.get(c["key"]) or {}).get("semiconductor", True)]
    valid.sort(key=lambda c: (scores.get(c["key"]) or {}).get("score", 0), reverse=True)
    if not valid:
        return {"ok": False, "error": "没有通过相关性筛选的文章（半导体相关的都为空）"}
    selected, others = valid[:n], valid[n:]

    root = issue_root(out_dir, issue_num, to_date)
    stage(f"归档原文 {len(valid)} 篇…")
    art = archive_articles(
        valid, root,
        titles={c["key"]: (scores.get(c["key"]) or {}).get("title_cn") or c["title"] for c in valid},
        reasons={c["key"]: (scores.get(c["key"]) or {}).get("reason", "") for c in valid},
        download_images=download_images,
        on_progress=None, check_cancelled=check_cancelled,
    )

    stage(f"深度解读 Top{len(selected)}…")
    details, detail_errors = analyze_selected(
        selected, models, prompts=prompts, cache=cache, workers=workers,
        on_progress=on_progress, check_cancelled=check_cancelled,
    )
    stage(f"概括其他入选 {len(others)} 篇…")
    briefs, brief_errors = brief_others(
        others, models, prompts=prompts, cache=cache, workers=workers,
        on_progress=on_progress, check_cancelled=check_cancelled,
    )

    stage("生成核心洞察…")
    intro = generate_intro(
        selected, scores, details, models, prompts=prompts,
        from_date=from_date, to_date=to_date, on_progress=None,
    )

    context = build_context(
        issue_num=issue_num, from_date=from_date, to_date=to_date,
        selected=selected, others=others, scores=scores, details=details,
        briefs=briefs, intro=intro, total=len(valid),
        wechat_count=sum(1 for c in valid if c["kind"] == "公众号"),
        arxiv_count=sum(1 for c in valid if c["kind"] == "arXiv"),
        archive_rel=art["rel_map"], org=org, title=report_title,
    )

    stage("渲染周报…")
    rendered = render_report(context, template_path=template_path)
    if not rendered["ok"]:
        return {
            "ok": False,
            "error": rendered["error"],
            "issue_dir": str(root),
            # 数据快照照写：模板坏了但 AI 的钱已经花了，重渲染时不该再花一遍
            "context": context,
        }

    stage("写入归档…")
    out = write_report_outputs(
        root, report_html=rendered["html"], context=context,
        scores=scores, details=details, briefs=briefs,
        index_rows=art["index_rows"], issue_num=issue_num, report_title=report_title,
    )

    errors = [*score_errors, *detail_errors, *brief_errors, *art["errors"], *out["errors"]]
    return {
        "ok": bool(out["ok"]),
        "issue_dir": str(root),
        "report_path": out.get("report_path", ""),
        "articles_dir": str(art["dir"]),
        "selected": len(selected),
        "others": len(others),
        "total": len(valid),
        "dropped": len(candidates) - len(valid),
        "archived": art["archived"],
        "failed": len(errors),
        "errors": errors[:20],
        # 模板引用了但没提供的变量 —— 改模板时最容易犯的错，显式报出来
        "missing_vars": rendered.get("missing", []),
    }


__all__ = [
    "BUSINESS_TAGS",
    "archive_articles",
    "archive_article",
    "generate_issue",
    "issue_root",
    "load_saved_context",
    "write_report_outputs",
    "TAG_COLORS",
    "DOMAINS",
    "PROMPT_KEYS",
    "PROMPT_LABELS",
    "WeeklyCache",
    "analyze_selected",
    "brief_others",
    "build_context",
    "build_prompt",
    "cache_key",
    "collect_candidates",
    "generate_intro",
    "issue_dirname",
    "llm_json",
    "load_prompts",
    "normalize_external",
    "normalize_wechat",
    "prompt_fingerprint",
    "prompts_payload",
    "read_external_body",
    "render_report",
    "resolve_template_dir",
    "save_prompts",
    "score_candidates",
    "suggest_issue_number",
]

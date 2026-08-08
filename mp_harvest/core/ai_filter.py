"""AI 标题过滤引擎：多模型（OpenAI 兼容 chat/completions）并行判定。

纯逻辑模块，不依赖 GUI；API 风格参考本地项目 tools/judge_titles.py：
- 配置从本地 JSON 文件读取（api_key 不写入仓库，data/*.json 已 gitignore）；
- 判定结果按 article_key 缓存到本地 JSON，命中直接复用不调 API；
- 支持多模型轮询分块并发、单模型失败自动降级到其他模型。

Prompt 设计：用户只编辑「筛选原则」（DEFAULT_PRINCIPLES / data/ai_principles.txt），
输出格式与判定要求（FIXED_OUTPUT_REQUIREMENTS）由软件固定，`build_system_prompt`
负责拼接完整 system prompt——保证无论用户怎么改原则，模型返回都是可解析的严格 JSON。

接口格式：每个模型可配 `format`（openai | anthropic）：
- openai    → POST {base_url}/chat/completions（Authorization: Bearer）
- anthropic → POST {base_url}/v1/messages（x-api-key + anthropic-version）

模型调用失败重试策略：429/5xx 退避重试；HTTP 400 且包含
response_format 提示时去掉该字段重试一次（兼容不支持 JSON mode 的模型）。
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from mp_harvest.infra.platform.paths import data_dir

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

DEFAULT_PRINCIPLES = """你是一位半导体与计算机体系结构领域的资深专家，负责为微信公众号文章标题做价值筛选。
目标：从大量文章中选出最前沿、有技术增量、有深入分析的知识型内容，只保留以下四类主题：
1. FPGA：架构、设计方法、时序/约束、验证、高速接口、可重构计算、工具链
2. 芯片设计：微架构、RTL/Verilog/SystemVerilog、SoC、验证（UVM等）、DFT、后端/物理设计、EDA
3. AI 辅助芯片设计：AI/大模型应用于芯片设计流程（RTL 生成、智能验证、AI EDA、布局布线优化、存算一体架构设计）
4. 芯片工艺：制程、光刻/EUV、先进封装/Chiplet、器件、材料、晶圆制造

【硬性排除，命中任意一条直接 drop】
- 招聘类：招聘、校招、社招、内推、实习、加入我们
- 广告/营销类：广告、推广、促销、优惠、折扣、免费领取、报名、抽奖、资料包
- 课程/培训类：课程、培训、训练营、公开课、研修班、付费教程
- 纯 AI 模型介绍类：文章重心是介绍/评测某个 AI 模型、Agent、训练框架或 AI 产品
- 财经/商业动态类：股价、市值、融资、收购、签约、投产、破产、产能、涨价、裁员
- 噱头/标题党类：制造焦虑或吸引点击的泛泛标题
- 基础科普/入门教程/FAQ 类：什么是X、一文读懂X、扫盲、入门、手把手、常见问题
- 经验分享/调试复盘类：个人踩坑、故障排查、案例复盘（AI 辅助芯片设计方法论除外）

【保留标准】不属于任何硬性排除，且 relevance_score >= 8，且 technical_depth >= 4。
判断依据只有标题时请保守判断（宁缺毋滥），信息不足一律 drop。"""

FIXED_OUTPUT_REQUIREMENTS = """【输出格式（必须严格遵守，软件固定，不可更改）】
只输出严格 JSON，不要 Markdown 代码块，不要任何多余文字：
{"items":[{"idx":0,"title":"原文标题","category":"fpga|chip_design|ai_chip_design|chip_process|other","keep":true,"relevance_score":1-10,"technical_depth":1-10,"confidence":"high|medium|low","reason":"20-60字具体中文理由"}]}
要求：
- idx 必须与输入列表中的序号一一对应，不能漏项、不能改序；
- keep 必须是 true/false 布尔值，软件以此区分「通过」与「过滤掉」列表；
- reason 必须具体：drop 写明命中哪类排除；keep 写明所属保留类型与可能技术内容。"""


def build_system_prompt(principles: str | None = None) -> str:
    """拼接完整 system prompt = 用户可编辑原则 + 软件固定输出要求。"""
    p = (principles or "").strip() or DEFAULT_PRINCIPLES
    return f"{p}\n\n{FIXED_OUTPUT_REQUIREMENTS}"


DEFAULT_PROMPT = build_system_prompt(DEFAULT_PRINCIPLES)


def default_principles_path(root_dir: str | Path | None = None) -> Path:
    """默认解析到 data_dir()；显式传 root_dir 时兼容旧布局 root/data/。"""
    base = (Path(root_dir) / "data") if root_dir else data_dir()
    return base / "ai_principles.txt"


def save_principles(path: str | Path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text or DEFAULT_PRINCIPLES, encoding="utf-8")


def load_principles(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        return DEFAULT_PRINCIPLES
    try:
        text = p.read_text(encoding="utf-8").strip()
    except Exception:
        return DEFAULT_PRINCIPLES
    return text or DEFAULT_PRINCIPLES


@dataclass
class ModelConfig:
    id: str
    name: str
    base_url: str
    api_key: str
    model: str
    enabled: bool = True
    format: str = "openai"  # "openai" | "anthropic"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelConfig":
        fmt = str(data.get("format") or "openai").strip().lower()
        if fmt not in ("openai", "anthropic"):
            fmt = "openai"
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or "未命名模型"),
            base_url=str(data.get("base_url") or "").rstrip("/"),
            api_key=str(data.get("api_key") or ""),
            model=str(data.get("model") or DEFAULT_MODEL),
            enabled=bool(data.get("enabled", True)),
            format=fmt,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "enabled": self.enabled,
            "format": self.format,
        }


def default_models() -> list[ModelConfig]:
    return [
        ModelConfig(
            id="model-default",
            name="模型",
            base_url="",
            api_key="",
            model="",
            enabled=True,
            format="openai",
        )
    ]


def load_models(path: str | Path) -> list[ModelConfig]:
    p = Path(path)
    if not p.exists():
        return default_models()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default_models()
    if not isinstance(data, list):
        return default_models()
    out: list[ModelConfig] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            out.append(ModelConfig.from_dict(item))
        except Exception:
            continue
    return out


def save_models(path: str | Path, models: list[ModelConfig]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps([m.to_dict() for m in models], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _model_label(m: ModelConfig) -> str:
    """模型展示名：优先「名称 (模型ID)」，让用户一眼看清实际调用的是哪个模型。"""
    name = (m.name or "").strip()
    model = (m.model or "").strip()
    if model and name and model != name:
        return f"{name} ({model})"
    if model:
        return model
    if name:
        return name
    return "未知模型"


def article_key(art: dict[str, Any]) -> str:
    return str(
        art.get("identity")
        or art.get("link")
        or art.get("title")
        or ""
    )


# ── 模型调用 ────────────────────────────────────────────────────────


def _endpoint(cfg: ModelConfig) -> str:
    base = cfg.base_url.rstrip("/")
    if cfg.format == "anthropic":
        return base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    return base + "/chat/completions"


def _build_openai_payload(
    cfg: ModelConfig, system_prompt: str, user_content: str
) -> dict[str, Any]:
    return {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }


def _build_anthropic_payload(
    cfg: ModelConfig, system_prompt: str, user_content: str
) -> dict[str, Any]:
    return {
        "model": cfg.model,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
        "temperature": 0.1,
    }


def _build_payload(cfg: ModelConfig, system_prompt: str, user_content: str) -> dict[str, Any]:
    if cfg.format == "anthropic":
        return _build_anthropic_payload(cfg, system_prompt, user_content)
    return _build_openai_payload(cfg, system_prompt, user_content)


def _post_chat(cfg: ModelConfig, payload: dict[str, Any]) -> str:
    url = _endpoint(cfg)
    headers = {"Content-Type": "application/json"}
    if cfg.format == "anthropic":
        headers["x-api-key"] = cfg.api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = "Bearer " + cfg.api_key
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _parse_content(cfg, data)


def _parse_content(cfg: ModelConfig, data: dict[str, Any]) -> str:
    """从 OpenAI / Anthropic 响应体中提取文本内容（纯函数，便于测试）。"""
    if cfg.format == "anthropic":
        content = data.get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text") or "")
        return ""
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def _call_model(
    cfg: ModelConfig,
    system_prompt: str,
    user_content: str,
    max_retries: int = 3,
) -> str:
    payload = _build_payload(cfg, system_prompt, user_content)
    is_openai = cfg.format != "anthropic"
    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return _post_chat(cfg, payload)
        except urllib.error.HTTPError as e:
            last_err = e
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore") or ""
            except Exception:
                pass
            # 模型不支持 response_format：去掉该字段再试一次（仅 OpenAI 格式）
            if (
                is_openai
                and e.code == 400
                and payload.get("response_format")
                and ("response_format" in body or "json" in body.lower())
                and attempt == 1
            ):
                payload.pop("response_format", None)
                continue
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(5 * attempt)
                continue
        except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(3 * attempt)
                continue
    raise RuntimeError(f"模型「{cfg.name}」调用失败: {last_err}")


def test_connection(cfg: ModelConfig) -> tuple[bool, str]:
    """发送 1 条最小消息测试模型连通性。返回 (ok, 说明)。"""
    if not cfg.api_key:
        return False, "未填写 API Key"
    if not cfg.base_url:
        return False, "未填写 Base URL"
    try:
        start = time.time()
        # 注意：不能用 max_retries=1——OpenAI 兼容的 json_object 模式遇到
        # 不支持/要求 prompt 含 "json" 的模型（如 DeepSeek）会先 400，
        # _call_model 需要第二次循环去掉 response_format 再试（2026-08-09 真机发现）。
        content = _call_model(
            cfg,
            "你是连通性测试助手，只回复 OK。",
            "请回复 OK",
            max_retries=2,
        )
        return True, f"连接成功（{time.time() - start:.1f}s）：{content[:40]!r}"
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败：{exc}"


def fetch_models(cfg: ModelConfig, timeout: int = 15) -> tuple[bool, str | list[str]]:
    """拉取 OpenAI 兼容 ``GET {base_url}/models`` 的可用模型列表。

    返回 ``(True, [model id, ...])`` 或 ``(False, 错误说明)``。
    Anthropic 格式不适用（无等价公开列表语义），直接返回提示。
    """
    if not cfg.api_key:
        return False, "未填写 API Key"
    if not cfg.base_url:
        return False, "未填写 Base URL"
    if cfg.format == "anthropic":
        return False, "Anthropic 接口不支持拉取模型列表，请手动填写模型名"
    url = cfg.base_url.rstrip("/") + "/models"
    headers = {
        "Authorization": "Bearer " + cfg.api_key,
        "Accept": "application/json",
    }
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return False, "接口返回格式异常（缺少 data 数组）"
        ids = [str(r.get("id") or "") for r in rows if isinstance(r, dict) and r.get("id")]
        if not ids:
            return False, "接口返回了空模型列表"
        return True, ids
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "HTTP 401：API Key 无效"
        if exc.code == 404:
            return False, "HTTP 404：该地址不支持 /models 列表接口，请手动填写模型名"
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败：{exc}"


def parse_model_output(text: str) -> list[dict[str, Any]]:
    content = (text or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.S)
    data = json.loads(content)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("模型输出缺少 items 数组")
    return items


def distribute_batches(
    article_count: int, batch_size: int, model_count: int
) -> list[tuple[int, int, int]]:
    """按 batch_size 分块，模型轮询分配：返回 [(start, end, model_index)]。"""
    if article_count <= 0 or batch_size <= 0 or model_count <= 0:
        return []
    out: list[tuple[int, int, int]] = []
    start = 0
    idx = 0
    while start < article_count:
        end = min(start + batch_size, article_count)
        out.append((start, end, idx % model_count))
        start = end
        idx += 1
    return out


_FALLBACK_VERDICT = {
    "keep": False,
    "category": "other",
    "relevance_score": 0,
    "technical_depth": 0,
    "confidence": "low",
    "reason": "模型未返回判定",
}


def _verdict_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {
        k: row.get(k)
        for k in (
            "keep",
            "category",
            "relevance_score",
            "technical_depth",
            "confidence",
            "reason",
            "at",
            "model",
        )
        if k in row
    }


# ── 判定主流程 ──────────────────────────────────────────────────────


def judge_articles(
    articles: list[dict[str, Any]],
    models: list[ModelConfig],
    *,
    prompt: str = DEFAULT_PROMPT,
    cache_path: str | Path | None = None,
    batch_size: int = 50,
    workers: int = 4,
    max_retries: int = 3,
    on_progress: Callable[[int, int], None] | None = None,
    on_batch: Callable[[list[dict[str, Any]], str | None], None] | None = None,
) -> dict[str, Any]:
    """用多个已启用模型并发判定标题。

    返回：
      {"ok": bool, "kept": [...], "dropped": [...], "errors": [...],
       "used_models": [...], "cached": int, "judged": int}
    判定结果（keep/category/relevance_score/technical_depth/confidence/
    reason/at）合并进每条文章条目。``on_batch(rows, err)`` 每完成一批回调一次
    （rows 已带判定字段），供服务层实时推送/刷新（2026-08-09）。
    """
    enabled = [m for m in models if getattr(m, "enabled", True)]
    if not enabled:
        raise ValueError("没有启用的 AI 模型，请在配置中启用至少一个")
    if not articles:
        return {
            "ok": True,
            "kept": [],
            "dropped": [],
            "errors": [],
            "used_models": [],
            "cached": 0,
            "judged": 0,
        }

    cache: dict[str, dict[str, Any]] = {}
    if cache_path is not None:
        cp = Path(cache_path)
        if cp.exists():
            try:
                raw = json.loads(cp.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cache = raw
            except Exception:
                cache = {}

    items: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    cache_lock = threading.Lock()
    for i, art in enumerate(articles):
        row = {
            "idx": i,
            "account": str(art.get("account") or ""),
            "title": str(art.get("title") or ""),
            "link": str(art.get("link") or ""),
            "publish_at": str(art.get("publish_at") or ""),
            "identity": str(art.get("identity") or ""),
            "_source": art,
        }
        key = article_key(art)
        if key and key in cache and not row["_source"].get("_ai_skip_cache"):
            verdict = dict(cache[key])
            row.update(verdict)
            row["_cached"] = True
            items.append(row)
        else:
            row["_cached"] = False
            pending.append(row)
            items.append(row)

    batches = distribute_batches(
        len(pending), max(1, int(batch_size)), len(enabled)
    )
    errors: list[str] = []
    used_models: list[str] = []
    done = 0
    total = len(pending)
    judged = 0

    def judge_batch(
        batch: tuple[int, int, int],
    ) -> tuple[list[dict[str, Any]], str | None]:
        start, end, model_idx = batch
        cfg = enabled[model_idx]
        batch_rows = pending[start:end]
        user = json.dumps(
            [
                {
                    "idx": r["idx"],
                    "account": r["account"],
                    "title": r["title"],
                }
                for r in batch_rows
            ],
            ensure_ascii=False,
        )
        try:
            content = _call_model(cfg, prompt, user, max_retries=max_retries)
            verdicts = parse_model_output(content)
        except Exception as exc:  # noqa: BLE001
            return batch_rows, f"{exc}"

        by_idx: dict[int, dict[str, Any]] = {}
        for v in verdicts:
            try:
                i = int(v.get("idx"))
            except (TypeError, ValueError):
                continue
            by_idx[i] = v
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for r in batch_rows:
            v = by_idx.get(r["idx"])
            if v is None:
                v = dict(_FALLBACK_VERDICT)
            verdict = {
                "keep": bool(v.get("keep", False)),
                "category": str(v.get("category", "other")),
                "relevance_score": int(v.get("relevance_score", 0) or 0),
                "technical_depth": int(v.get("technical_depth", 0) or 0),
                "confidence": str(v.get("confidence", "low")),
                "reason": str(v.get("reason", "")),
                "at": now,
                "model": cfg.model,
            }
            r.update(verdict)
            key = article_key(r["_source"])
            if key:
                with cache_lock:
                    cache[key] = verdict
        return batch_rows, None

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, int(workers))
    ) as ex:
        futures = {ex.submit(judge_batch, b): b for b in batches}
        for fut in concurrent.futures.as_completed(futures):
            rows, err = fut.result()
            judged += len(rows)
            done += len(rows)
            midx = futures[fut][2]
            if err:
                errors.append(err)
                # 失败批次统一按 drop 兜底
                now = time.strftime("%Y-%m-%d %H:%M:%S")
                for r in rows:
                    if "keep" not in r:
                        r.update(dict(_FALLBACK_VERDICT))
                        r["reason"] = "模型调用失败，按丢弃处理"
                        r["at"] = now
                        r["model"] = enabled[midx].model
                    key = article_key(r["_source"])
                    if key:
                        with cache_lock:
                            cache[key] = _verdict_fields(r)
            else:
                used_models.append(_model_label(enabled[midx]))
            if on_batch:
                on_batch(rows, err)
            if on_progress:
                on_progress(done, total)

    # 缓存落盘（仅当有新判定）
    if judged and cache_path is not None:
        try:
            cp = Path(cache_path)
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(
                json.dumps(cache, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception:
            pass

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for row in items:
        art = dict(row["_source"])
        for k, v in _verdict_fields(row).items():
            art[k] = v
        (kept if row.get("keep") else dropped).append(art)

    cached_n = sum(1 for r in items if r.get("_cached"))
    return {
        "ok": not errors,
        "kept": kept,
        "dropped": dropped,
        "errors": errors,
        "used_models": list(dict.fromkeys(used_models)),
        "cached": cached_n,
        "judged": judged,
    }

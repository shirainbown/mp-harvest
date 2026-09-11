"""REST 请求/响应 Pydantic 契约（设计稿 §7.1）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ── accounts ──────────────────────────────────────────────────────


class AccountCreateIn(BaseModel):
    # 名称可留空：core add_pending 默认「未命名公众号」（2026-08-09 用户反馈）
    name: str = ""
    url: str = Field(min_length=1)


class ImportItemIn(BaseModel):
    """单条导入项（前端 preview 返回后原样回传 confirm）。"""

    name: str = ""
    url: str = ""
    dup: bool = False


class ImportIn(BaseModel):
    """批量导入两段式：preview 解析去重（{text}）→ confirm 确认入库（{items}）。"""

    stage: Literal["preview", "confirm"] = "preview"
    text: str = ""
    items: list[ImportItemIn] | None = None


# ── history / articles ────────────────────────────────────────────


class HistoryFetchIn(BaseModel):
    account_id: str = Field(min_length=1)
    days: int = Field(default=7, ge=1, le=365)
    # 自定义日期范围（YYYY-MM-DD）；两者都提供时优先于 days（2026-08-23 新增）
    start_date: str = ""
    end_date: str = ""  # 缺省 = 今天


class HistoryFetchBatchIn(BaseModel):
    """批量拉取：一次为多个公众号创建聚合任务（2026-08-09 新增）。"""

    account_ids: list[str] = Field(min_length=1)
    days: int = Field(default=7, ge=1, le=365)
    start_date: str = ""
    end_date: str = ""


class SupplementIn(BaseModel):
    account_id: str | None = None
    url: str = Field(min_length=1)
    title: str = ""


class ExportHtmlIn(BaseModel):
    account_id: str | None = None
    ids: list[str] | None = None  # 文章 identity；空 = 该账号全部
    view: str = "all"  # ids 为空时按当前视图过滤（all/keep/drop/pending）
    stage: str = "final"  # 视图所属阶段：final / title / content（2026-08-16）
    out_dir: str | None = None  # 自定义导出目录（支持 ~ 展开）；留空用默认 data_dir/exports/...
    # 正文图片本地化；None = 读后端设置 export.download_images（默认 False，2026-09 重构 B7）
    download_images: bool | None = None
    # 时间筛选（与列表一致，2026-08-23）
    start_date: str = ""
    end_date: str = ""
    latest_fetch: bool = False


# ── ai ────────────────────────────────────────────────────────────


class AiFilterIn(BaseModel):
    account_id: str = ""  # 空 = 全部公众号
    # 并行判定控制（2026-08-09）：每批多少篇 / 同时提交几批；不传用 core 默认（30 / 4）
    batch_size: int | None = Field(default=None, ge=1, le=200)
    workers: int | None = Field(default=None, ge=1, le=16)
    # 时间筛选（2026-08-23）：只筛选该范围内的缓存文章
    start_date: str = ""
    end_date: str = ""
    latest_fetch: bool = False


class AiContentFilterIn(BaseModel):
    """内容筛选（第二阶段）：仅对当前 keep=True 的文章拉正文并判定。"""

    account_id: str = ""  # 空 = 全部公众号
    batch_size: int | None = Field(default=None, ge=1, le=200)
    workers: int | None = Field(default=None, ge=1, le=16)
    start_date: str = ""
    end_date: str = ""
    latest_fetch: bool = False


class AiModelIn(BaseModel):
    """与 core.ai_filter.ModelConfig 对齐。

    ``name`` 必须可选（2026-09 修复）：core 的 ``ModelConfig.name`` 默认空串，
    前端「+ 添加模型」只填 id/base_url/api_key/model/format。原先这里写成
    ``Field(min_length=1)`` 必填，导致整个数组 PUT 被 422 拒绝 —— 只要存在一张
    新建的空白卡片，**所有**模型的保存都会失败（改了的 Key 一个都存不下来）。
    """

    id: str = ""
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    enabled: bool = True
    format: str = "openai"  # openai | anthropic


class ModelFetchIn(BaseModel):
    """拉取模型列表所需的最小配置（不需要 model/name）。"""

    base_url: str = ""
    api_key: str = ""
    format: str = "openai"


class PrinciplesIn(BaseModel):
    text: str = ""


# ── settings ──────────────────────────────────────────────────────


class TestProxyIn(BaseModel):
    proxy: str = ""  # 形如 http://127.0.0.1:8088；空则读 settings


# ── update ────────────────────────────────────────────────────────


class UpdateDownloadIn(BaseModel):
    zip_url: str = Field(min_length=1)
    proxy: str = ""


# ── 通用响应 ──────────────────────────────────────────────────────


class TaskCreatedOut(BaseModel):
    task_id: str
    type: str


class OkOut(BaseModel):
    ok: bool = True
    message: str = ""


class ErrorOut(BaseModel):
    detail: str


ResponseDict = dict[str, Any]

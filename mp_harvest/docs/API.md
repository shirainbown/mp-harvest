# MP Harvest API 契约（服务层 ↔ 前端）

> 权威参考（设计稿 §7 + Epic D1 前端联调对齐结果）。实现见 `mp_harvest/server/routes/`，
> 请求/响应 Pydantic 模型见 `mp_harvest/server/schemas.py`。
>
> **通用约定**
> - 服务只监听 `127.0.0.1` 动态端口；所有 `/api/*` 与 `/ws` 校验启动 token
>   （query `?token=` 或 `Authorization: Bearer`），失败返回 `401 {"detail":...}`
>   （WS 以 close code 4401 拒绝）。
> - 错误响应统一为 `{"detail": "..."}` + 恰当 HTTP 状态码（400 参数 / 404 不存在 /
>   409 状态冲突 / 422 校验失败 / 500 内部错误）。
> - 耗时操作（>100ms）一律返回 `202 + {task_id, type}`，进度经 WebSocket 推送（§3.2）。

## 1. REST 端点全表

### 平台 / 壳

| 方法 | 路径 | 请求 | 响应 | core/infra 模块 |
|---|---|---|---|---|
| GET | `/api/platform` | — | `{os, ca_needs_admin, proxy_needs_admin, data_dir, engine, version}` | `infra.platform.Platform.info()`（新增） |

### 公众号 / 凭证

| 方法 | 路径 | 请求 | 响应 | core 模块 |
|---|---|---|---|---|
| GET | `/api/accounts` | — | **裸数组** `[Account]`（`Account={id,name,url,__biz?,expires_at,pending}`，见 §3 第 5 条） | `store.AccountStore.list_accounts` + `server.mappers.account_out` |
| POST | `/api/accounts` | `{name?, url}`（name 可留空，默认「未命名公众号」，2026-08-09） | `201` **裸对象** `Account`（附加 `mitm_message` 提示字段）；best-effort 确保 mitm 运行并广播 `mitm.status` | `store.add_pending` + `server.mappers.account_out` |
| POST | `/api/accounts/import` | 两段式：preview `{text}`；confirm `{stage:"confirm", items:[{name,url,dup}]}` | preview → `{items:[{name,url,dup}]}`（批内/已有去重标记）；confirm → `{imported, skipped}`（dup/无链接跳过） | `batch_import.parse_batch_lines / dedupe_by_name / split_fresh_duplicates`、`store.add_pending` |
| DELETE | `/api/accounts/{id}` | — | `{ok, id}`；不存在 404 | `store.delete` |
| GET | `/api/accounts/{id}/credential` | — | `{account_id, name, expires_at, credentials, json}`；无凭证 409，不存在 404 | `store.get`、`credentials.credentials_to_json` |
| POST | `/api/accounts/{id}/renew` | — | 续约（§5.4：重置为等待抓包）。`{ok, account_id, status:"awaiting"}`；缺 __biz/文章链接 400，不存在 404，mitm 启动失败 500 | `store.set_awaiting`、`capture_target.expected_biz`、`infra.mitm.MitmCaptureService.reset_capture_state` |

### MITM / CA

| 方法 | 路径 | 请求 | 响应 | core/infra 模块 |
|---|---|---|---|---|
| GET | `/api/mitm/status` | — | `{running, port}` | `infra.mitm.MitmCaptureService` |
| POST | `/api/mitm/start` | — | `{ok, message, running, port}`；启动失败 500（并广播 `mitm.status`） | `MitmCaptureService.start` |
| POST | `/api/mitm/stop` | — | 同上 | `MitmCaptureService.stop` |
| POST | `/api/ca/install` | — | `InstallResult.to_dict()`（结构化错误内嵌 `ok:false`，非 5xx） | `infra.platform.ca.install` |
| GET | `/api/ca/status` | — | `{installed, trusted, cert_path, needs_admin}`（`trusted` 为前端判定字段，与 `installed` 同值，2026-08-09 修复） | `infra.platform.ca.status/cert_path` |

### 历史 / 文章 / 导出

| 方法 | 路径 | 请求 | 响应 | core 模块 |
|---|---|---|---|---|
| POST | `/api/history/fetch` | `{account_id, days=7}` | `202 {task_id, type}`；无凭证 409 | `history_client.fetch_history_days` |
| GET | `/api/articles?account_id=&view=&order=` | query：`view=all/keep/drop`、`order=desc/asc` | **裸数组** `[Article]`（`Article={id,account_id,title,url,date,source,verdict,reason}`，见 §3 第 5 条）；非法 view/order 400 | `state` 文章缓存 + `sightings` + `server.mappers.article_out` |
| POST | `/api/articles/supplement` | `{account_id?, url, title?}` | `201` **裸对象** `Article`（`source=补`） | `sightings.SightingsStore.upsert` + `server.mappers.article_out` |
| GET | `/api/articles/export-list?account_id=&view=&format=` | query：`view=all/keep/drop`（只导出当前视图 §5.5）、`format=json/csv/tsv/md/links/title+links` | **纯文本**（`text/plain`，`Content-Disposition` 带 RFC5987 文件名）；非法格式/view 400，账号 404 | `history_export.render_export / default_export_filename`（fmt 映射 `md→markdown`、`title+links→title_links`） |
| POST | `/api/articles/export-html` | `{account_id?, ids?, view?, out_dir?}`：`ids` 非空导出指定篇；否则按 `view=all/keep/drop` 过滤当前账号；`out_dir` 指定目标目录（支持 `~` 展开，留空用默认 `data/exports/…`） | `202 {task_id, type, total}`；无文章/非法 view 400。任务完成后在 `out_dir` 生成逐篇 HTML + **titles_filtered 风格 `index.html` 说明页**（搜索/排序/判定/本地+原文链接） | `article_reader.batch_export_articles`（§6 正文只有 HTML） |

### AI

| 方法 | 路径 | 请求 | 响应 | core 模块 |
|---|---|---|---|---|
| POST | `/api/ai/filter` | `{account_id, batch_size?=50, workers?=4}` | `202 {task_id, type, total}`；无文章 400；batch_size 1–200 / workers 1–16 越界 422；每批完成 WS 推 `ai.batch` | `ai_filter.judge_articles`（并行判定控制 + 实时刷新，2026-08-09） |
| GET | `/api/ai/models` | — | `{models: [ModelConfig]}` | `ai_filter.load_models` |
| PUT | `/api/ai/models` | `[{id?, name, base_url?, api_key?, model?, enabled?, format?}]` | `{ok, count}` | `ai_filter.save_models` |
| POST | `/api/ai/models/test` | 单个 ModelConfig | `{ok, message}` | `ai_filter.test_connection` |
| POST | `/api/ai/models/fetch` | `{base_url, api_key, format}` | `{ok, models: [id...]}`（失败 `{ok:false, message}`） | `ai_filter.fetch_models`（OpenAI 兼容 `GET {base_url}/models`，2026-08-09 新增） |
| GET | `/api/ai/principles` | — | `{text, default}`（`default` = `ai_filter.DEFAULT_PRINCIPLES`，前端「恢复默认」） | `ai_filter.load_principles` |
| PUT | `/api/ai/principles` | `{text}` | `{ok}` | `ai_filter.save_principles` |

### 设置 / 更新 / 任务

| 方法 | 路径 | 请求 | 响应 | core/infra 模块 |
|---|---|---|---|---|
| GET | `/api/settings` | — | `{settings: {...}}` | `settings.load_settings` |
| PUT | `/api/settings` | 任意 JSON 对象 | `{ok}` | `settings.save_settings` |
| POST | `/api/settings/test-proxy` | `{proxy}` | `{ok, message}`；空/坏地址 400 | `settings`（TCP 连通测试） |
| GET | `/api/update/check` | — | `UpdateCheckResult.to_dict()`（含 `available/version/zip_url/notes`；失败结构化 `ok:false`） | `infra.platform.updater.check` |
| POST | `/api/update/download` | `{zip_url, proxy?}` | `202 {task_id, type}` | `infra.platform.updater.download` |
| POST | `/api/update/apply` | — | 应用已下载更新（取 `data/update/` 下最新 `.zip`）。**真实实现不返回**（退出→替换→重启）；未下载 409，更新包缺失等平台错误 500 | `infra.platform.updater.apply` |
| GET | `/api/tasks` | — | `{tasks: [...]}` | `server.tasks.registry` |
| GET | `/api/tasks/{id}` | — | 任务详情；404 | 同上 |
| POST | `/api/tasks/{id}/cancel` | — | `{ok}`；404 | 同上（分页/批次边界生效） |

## 2. WebSocket（`/ws?token=`）

连接仅收服务端广播（客户端发消息仅用于保活/断线检测）。消息形如 `{"type": ..., ...payload}`：

| type | payload | 触发 |
|---|---|---|
| `task.progress` | `{task_id, percent, message}` | 拉历史 / AI 筛选 / HTML 导出 / 更新下载中 |
| `task.done` | `{task_id, result}` | 任务完成 |
| `task.error` | `{task_id, error}` | 任务失败 |
| `credential.captured` | `{account_id, expires_at}` | MITM 截获凭证并绑定（前端开始 30min 本地倒计时） |
| `credential.expired` | `{account_id}` | 凭证到期兜底（前端主要靠本地倒计时） |
| `ai.batch` | `{account_id, articles:[{id, verdict, reason}]}` | AI 筛选每完成一批即推（2026-08-09，前端实时刷新判定） |
| `mitm.status` | `{running, port}` | 代理启停 / 添加账号 / 续约时状态变化 |
| `clipboard.credential` | `{name, url}` | 剪贴板目击凭证链接（前端弹 toast） |

## 3. 前端联调对齐备注（Epic D1，2026-08-08）

1. `POST /api/accounts/{id}/renew`、`GET /api/mitm/status`、`POST /api/update/apply`
   为前端假设新增端点（§7.1 未列出），已实现并补契约测试。
2. `POST /api/accounts/import` 响应形状对齐前端：preview → `{items:[{name,url,dup}]}`，
   confirm（`{stage:"confirm", items}`）→ `{imported, skipped}`。
3. `GET /api/articles/export-list` 接受 `account_id`/`view`/`format`，返回**纯文本**而非 JSON 信封。
4. `GET /api/ai/principles` 响应为 `{text, default}`。
5. **已对齐（Epic D 补一轮联调，2026-08-08）**：前端 store 对 `GET /api/accounts`、
   `POST /api/accounts`、`GET /api/articles`、`POST /api/articles/supplement` 按
   **裸数组/裸对象**解析，且字段名与 core 行格式不一致。已在 server 层新增
   `server/mappers.py` 做适配映射（不改 core / 不改 frontend），4 个端点响应
   与前端 `types.ts` 逐字段对齐：

   **账号（core 行 → Account）**：`id→id`、`name→name`、`article_url→url`、
   `biz|credentials.__biz→__biz`（空则省略）、`expires_at`（ISO 字符串）→
   epoch 秒（null 保持 null）、`status=='awaiting'→pending:bool`。
   `POST /api/accounts` 的 mitm 提示收敛为对象上的 `mitm_message` 附加字段。

   **文章（core 行 → Article）**：`identity→id`、`link→url`、`publish_ts→date`
   （本地 ISO 字符串，`Date.parse` 可解析；无 ts 时退 `seen_at`/`publish_at`）、
   `source`（`getmsg→G`、`manual→补`、`mitm/mitm_getmsg/sighting→M`）、
   `keep`（`True→keep`/`False→drop`/缺省→`null`）、`reason`（缺省空串）。
   `view/order` 过滤排序仍在映射前按 core 字段（`keep`/`publish_ts`）生效。

   契约测试 `tests/server/test_frontend_mapping.py` 覆盖逐字段形状；真实 core
   冒烟（`--no-window` + curl 带 token）4 端点全部验证通过。

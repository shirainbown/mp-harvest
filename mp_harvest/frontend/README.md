# MP Harvest Frontend

Vue 3 + Vite + TypeScript + Tailwind CSS + Pinia SPA（设计稿 §5/§7 实现，UI 1:1 还原 `../../index.html`）。

## 开发

```bash
npm install
npm run dev        # 默认走 vite proxy → http://127.0.0.1:8765（可用 VITE_API_TARGET 覆盖）
npm run build      # 产物 dist/（首屏 gzip ~107KB）
npm run type-check
```

## URL 参数

| 参数 | 说明 |
|---|---|
| `?token=xxx` | 后端启动 token，自动附加到所有 REST（query）与 WS（`/ws?token=`） |
| `?api=http://host:port` | 直连后端地址（绕过 vite proxy，可指向远程采集端） |
| `?mock=1` | 无后端演示模式：内置假数据（6 公众号 / 620 文章 / 2 模型），并模拟 §7.2 全部 WS 事件（抓包回写、任务进度、mitm 状态） |

示例：`http://localhost:5173/?mock=1` 可直接演示全部四页与交互。

## 与 §7 契约的对齐结论（2026-08-08 E4 已收口，见 docs/TEST_RECORD.md）

§7.1 之外的补充端点（前端已调用，mock 已实现，后端已对齐）：

| 端点 | 原因 |
|---|---|
| `GET /api/mitm/status` | §7.1 只有 start/stop，首屏需要查询代理当前状态 |
| `POST /api/accounts/{id}/renew` | §5.4「续约/一键续约」在 §7.1 无对应端点 |
| `POST /api/update/apply` | §5.8「重启以应用」在 §7.1 无对应端点 |

契约形状假设：

- `POST /api/accounts/import` 两段式：`{text}` → `{items:[{name,url,dup}]}`（预览）；`{stage:"confirm",items}` → `{imported,skipped}`（确认）。
- `GET /api/articles` 返回数组，元素字段 `{id,account_id,title,url,date(ISO),source:"M"|"G"|"补",verdict:"keep"|"drop"|null,reason}`。
- `GET /api/ai/principles` → `{text, default}`。
- `GET /api/ai/models` → `{models:[...]}`（前端解包）；`PUT /api/ai/models` 发**裸数组**（name 可缺省）。
- `GET /api/settings` → `{settings:{...}}`（前端解包 `mode`/`proxy` → `proxy_url`）；`PUT /api/settings` 存 `{mode, proxy}`；`POST /api/settings/test-proxy` 发 `{proxy}`。
- `GET /api/update/check` → `{ok, available, version?, current_version?, zip_url?, notes?, error?}`；`POST /api/update/download` 发 `{zip_url}`。
- `GET /api/articles/export-list` 返回**文本**（前端复制/下载）；生产环境也可改为后端直接写 exports/，前端仅需 toast。
- token 统一走 query（`?token=`），WS 为 `/ws?token=`（§3.5 允许 query 或 header）。

## 结构

```
src/
├── config.ts            # token / api base / mock 开关（URL query）
├── types.ts             # §7 契约 TS 类型
├── api/rest.ts          # fetch 封装：自动带 token、错误统一 toast、mock 转发
├── api/ws.ts            # WS 自动重连（退避），事件分派到 stores
├── stores/              # ui / accounts / articles / tasks / settings
├── composables/useTicker.ts  # 全局单一 1s interval 驱动所有倒计时
├── mock/index.ts        # ?mock=1 假后端（REST + WS 事件模拟）
├── components/          # §5.9 组件库：Button/Input/Seg/Badge/Toast/Modal/Drawer/Popover/Tooltip/EmptyState/Skeleton/ProgressInline/Switch
├── layout/              # Sidebar + UpdateModal（markdown-it 渲染更新说明）
└── views/               # Credential / History（>500 虚拟滚动）/ AiModels / Network
```

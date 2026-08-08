# 功能实现看板

> 认领规则：把 ⬜ 改为 🚧 + @负责人 + 日期；完成改 ✅ 并在 PROGRESS.md 记一笔。
> 图例：⬜ Todo ｜ 🚧 Doing ｜ ✅ Done ｜ ⚠️ Blocked

## Epic A · core 业务层平移（W1）

| ID | 任务 | 状态 | 负责人 | 备注 |
|---|---|---|---|---|
| A1 | store / capture_target / sightings / clipboard_watch 平移 | ✅ | Agent-1 08-08 | 逻辑零改动 |
| A2 | history_client / history_ranges / history_account_select 平移 | ✅ | Agent-1 08-08 | |
| A3 | ai_filter / batch_import / history_export 平移 | ✅ | Agent-1 08-08 | |
| A4 | article_reader 输出层改单一 HTML 模板（§6） | ✅ | Agent-1 08-08 | 已删 docx/md/txt/json 分支 |
| A5 | infra/mitm：mitm_capture / mitm_addon 平移 | ✅ | Agent-1 08-08 | CA 准备委托 platform 占位接口 |
| A6 | 旧测试平移适配并全绿（去 UI/docx 相关） | ✅ | Agent-1 08-08 | 79/79 run_tests.py |

## Epic B · server + shell + platform（W2）

| ID | 任务 | 状态 | 负责人 | 备注 |
|---|---|---|---|---|
| B1 | server/app.py 装配 + token 中间件 | ✅ | Agent-2 08-08 | §3.5 纯 ASGI 中间件，http+ws 全覆盖 |
| B2 | tasks.py 任务注册表 + ws.py 广播 | ✅ | Agent-2 08-08 | §3.2 ThreadPoolExecutor+取消+WS 推送 |
| B3 | routes 全端点（§7.1） | ✅ | Agent-2 08-08 | accounts/mitm/history/ai/export/settings/update/platform/tasks |
| B4 | infra/platform：base + mac + win（CA/代理/路径/打开/更新） | ✅ | Agent-2 08-08 | §4 paths.py 为本 agent 新建（core agent 未建） |
| B5 | shell/main.py：uvicorn 线程 + pywebview 窗口 | ✅ | Agent-2 08-08 | §3.2 线程模型，--dev/--no-window/--hidden-titlebar |
| B6 | server 契约测试（httpx TestClient） | ✅ | Agent-2 08-08 | 63 用例全绿，core 全 mock |

## Epic C · frontend SPA（W3）

| ID | 任务 | 状态 | 负责人 | 备注 |
|---|---|---|---|---|
| C1 | Vite+Vue3+TS+Tailwind 工程初始化，设计 Token 映射 | ✅ | Agent-3 08-08 | 1:1 还原 index.html |
| C2 | 凭证管理页 + MITM 面板 + 倒计时 + 批量导入 Drawer | ✅ | Agent-3 08-08 | §5.4 |
| C3 | 历史文章页（视图切换/虚拟滚动/AI 进度/导出入口） | ✅ | Agent-3 08-08 | §5.5 |
| C4 | AI 模型页 + 网络设置页 | ✅ | Agent-3 08-08 | §5.6/§5.7 |
| C5 | api/rest + ws 封装 + Pinia stores | ✅ | Agent-3 08-08 | §7 |
| C6 | 检查更新 Modal + Toast/Modal/Popover 组件库 | ✅ | Agent-3 08-08 | §5.8/§5.9 |

## Epic D · 集成与发布（W4/W5）

| ID | 任务 | 状态 | 负责人 | 备注 |
|---|---|---|---|---|
| D1 | 依赖合并 requirements.txt + 端到端冒烟 | ✅ | Agent-4 08-08 | 契约对齐+72 用例绿+双测试套绿+冒烟全通，见 PROGRESS W4 |
| D2 | shell 加载 frontend dist（生产模式） | ✅ | Agent-4 08-08 | GET / 返回 dist/index.html 已验证 |
| D1b | 信封/字段差异联调收口（server/mappers.py） | ✅ | Agent-5 08-08 | 4 端点裸数组/裸对象+逐字段对齐，78 用例绿+真实 core 冒烟通过，见 API.md §3.5 |
| D3 | 采集端分离 API（§11.5.4，可后置） | ⬜ | | |
| D4 | CI 双平台流水线 + mac 公证 + DMG | ⬜ | | M5 |
| D5 | 在线更新链路验证 | ⬜ | | M5 |

## Epic E · E2E 验证与缺陷修复（2026-08-08）

| ID | 任务 | 状态 | 负责人 | 备注 |
|---|---|---|---|---|
| E1 | dev 模式 CA 自动生成（mitm 可启动） | ✅ | 本机自动化 | ca_setup 用 mitmproxy CertStore 兜底，见 TEST_RECORD §1 |
| E2 | macOS 系统代理接线 + 按服务备份/恢复 | ✅ | 本机自动化 | mitm_capture 委托 platform.proxy；不再关掉用户已有代理 |
| E3 | mitm 二次启动 EADDRINUSE 修复 | ✅ | 本机自动化 | stop 显式停 server + start 端口探测 + SystemExit 捕获；3 轮复现全过 |
| E4 | 前端契约对齐（ai models / settings / update / download / name 默认） | ✅ | 本机自动化 | settings.ts + types.ts + mock 与服务端 API.md 一致 |
| E5 | M1 收尾：pywebview 真窗口验证 | ✅ | 本机自动化 | Cocoa 开窗 + 优雅退出，M1 ✅ |
| E6 | API E2E 50 项断言 + 测试/更新记录落盘 | ✅ | 本机自动化 | 见 TEST_RECORD.md / PROGRESS W4 补2 |
| E7 | M2 真机：凭证捕获→绑定→倒计时闭环缺失（credential_watcher） | ✅ | 2026-08-09 | bug 9，真机复测通过 |
| E8 | AI 模型测试 400 必失败（json_object 兜底被 max_retries=1 掐掉） | ✅ | 2026-08-09 | bug 10，真机 1.2s 通过 |
| E9 | AI 模型列表获取 + 下拉选择（fetch /models） | ✅ | 2026-08-09 | `POST /api/ai/models/fetch` + 前端下拉，deepseek 实测 |
| E10 | 断连根因排查（系统代理 8088 ↔ 本机助手） | ✅ | 2026-08-09 | 见 TEST_RECORD 事故根因分析；抓包短窗口约定 |
| E11 | AI 模型页 UX：组合框输入/选择 + 手动保存 + toast 时长 | ✅ | 2026-08-09 | 用户反馈收口：datalist 单控件、无自动保存、1.2s/2.5s |
| E12 | 并行判定控制（每批篇数 / 并发批数） | ✅ | 2026-08-09 | `/api/ai/filter` batch_size/workers + 历史页弹层输入 |
| E13 | AI 判定每批实时刷新（on_batch + WS ai.batch） | ✅ | 2026-08-09 | 默认每批 50；前端按 id 合并 verdict/reason |
| E14 | 公众号名称可留空（默认未命名公众号） | ✅ | 2026-08-09 | AccountCreateIn.name 放开 + 表单去掉必填校验 |
| E15 | 任意目录直接运行（run.py + 自动构建 + 相对资源路径） | ✅ | 2026-08-09 | /tmp 实测：页面/资源/数据目录正确；清理测试污染的垃圾目录 |
| E16 | 项目更名 MP Harvest（mp-harvest）+ 新图标 | ✅ | 2026-08-09 | 包/目录/品牌全量更名；icon 入侧边栏+favicon；手册截图重新生成 |
| E17 | 仓库公开 + macOS Release v2.0.0 | ✅ | 2026-08-09 | PyInstaller arm64 .app 打包 + 冒烟 + GitHub Release（未签名） |
| E18 | 应用内升级链路修复（macOS apply）+ v2.0.1 | ✅ | 2026-08-09 | .app 安装目录/管理员授权/开发模式保护；单测 5 条；Release v2.0.1 |
| E19 | CA 信任精确校验 + 抓包安全守卫（v2.0.2） | ✅ | 2026-08-09 | verify-cert 按证书校验；install 补显式信任设置；未信任拒绝切代理；Release v2.0.2 |
| E20 | 凭证过期广播 + 打开证书目录 + 更新文案 + README | ✅ | 2026-08-09 | sweep_expired；POST /api/ca/open；UpdateModal 文案；根 README |

## Epic C 遗留问题（不阻塞，联调期处理）

- ✅ ~~「打开证书文件」按钮暂为禁用占位~~ → **E20 已实现**：`POST /api/ca/open` 打开证书目录。
- ⬜ 导出完成 toast 未带「打开文件夹」按钮（§6.1），当前 toast 仅文本提示导出目录。
- ⬜ 列表导出浏览器环境为下载附件，生产 pywebview 场景可切换为后端写盘。
- ✅ ~~**与后端 §7 契约假设见 frontend/README.md**~~ → **E4 已收口**：AI 模型 GET/PUT、settings 解包与 proxy 字段、update check/download 字段全部对齐服务端（见 TEST_RECORD §5-8）
- ✅ ~~**D1 发现的信封/字段差异**~~ → **D1b 已收口**：新增 `server/mappers.py` 适配映射，`GET/POST /api/accounts`、`GET /api/articles`、`POST /api/articles/supplement` 4 端点返回裸数组/裸对象，字段与前端 types.ts 逐字段一致（不改 core/frontend）；契约测试 + 真实 core 冒烟通过（见 docs/API.md §3 第 5 条）。
- ✅ **server 侧已修复**：列表导出 fmt 映射 `md→markdown`（core render_export 的 key 是 markdown，fake mock 未暴露，真实 core 冒烟发现）。

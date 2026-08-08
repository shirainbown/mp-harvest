# 开发进度管理

> 每完成一个子任务立即更新本文件。状态：✅ 完成 / 🚧 进行中 / ⬜ 未开始 / ⚠️ 阻塞

## 里程碑总览（设计稿 §9）

| 里程碑 | 内容 | 状态 | 验收 |
|---|---|---|---|
| M1 骨架 | shell + FastAPI + pywebview 开窗加载 SPA；paths、token 校验 | ✅ | 双平台开窗显示页面，浏览器可直接调试；mac 真窗口（Cocoa）已验证开窗/退出，win 待打包验证 |
| M2 凭证闭环 | CA/代理适配 + mitm 平移 + 凭证管理页 + WS 推送 | ⬜ | 装CA→开代理→抓凭证→倒计时→续约 全通 |
| M3 历史与导出 | 历史拉取/目击/列表导出 + 历史文章页 + HTML 正文导出 | ⬜ | 90天500+篇流畅滚动；HTML 导出达标 |
| M4 AI 与设置 | AI 筛选/模型页/原则/网络设置/批量导入 | ⬜ | 多模型并发 + 缓存命中 + 旧测试全绿 |
| M5 发布 | 双平台 CI（mac 公证 DMG / win zip）+ 在线更新 | ⬜ | tag 发布后双端在线更新 |

## 当前迭代记录

### 2026-08-08 · 迭代 1（多 agent 并行）
- ✅ W1：core + infra/mitm 平移，article_reader 输出层改 HTML 模板，旧测试迁移（Agent-1，2026-08-08）
  - core/ 12 模块 + infra/mitm 2 模块平移，import 改 `mp_harvest.*` 绝对导入，数据目录统一 `infra.platform.paths.data_dir()`；
  - article_reader 输出层重写：仅 HTML（Jinja2 `core/templates/article.html`，白名单 sanitize + no-referrer + 跟踪参数剥离 + 批量 index.html + 可选图片本地化）；
  - 测试：剔除 UI（article_list_view）/ docx / updater 用例，export_formats 改 HTML 断言，新增 test_paths；`run_tests.py` **79/79 全绿**；
  - 产出 `requirements-core.txt`（requests/bs4/lxml/jinja2/pyperclip/cryptography/mitmproxy）。
- ✅ W2：server（routes/ws/tasks/schemas）+ shell + infra/platform + 契约测试（Agent-2）
  - **infra/platform**（§4）：`base.py` 统一接口 `ca.install/status/cert_path`、`proxy.enable/disable`、`paths.data_dir`、`shell_open`、`updater.check/download/apply`、`info()`，`get_platform()` 按 sys.platform 分派单例；`paths.py` 由本 agent 新建（开发=包内 data/，冻结=%APPDATA%/MP Harvest 或 ~/Library/Application Support/MP Harvest）；`ca_setup.py` 提供 mitm_capture 需要的 `PROXY_HOST/PROXY_PORT/prepare_mitm_confdir`（平移旧版）；`win.py`（certutil 用户存储无需管理员、HKCU+InternetSetOption、os.startfile、退出替换重启 bat，winreg/ctypes 全惰性 import，mac 可导入已验证）；`mac.py`（osascript `with administrator privileges` 弹授权装 CA、networksetup 遍历所有网络服务跳过 `*` 禁用项、`open`、.app 替换脚本）；失败全返回结构化结果/抛 PlatformError。
  - **server**（§3.2/§7）：`app.py` 纯 ASGI token 中间件（http+ws 全覆盖，query `token` 或 Bearer）；`tasks.py` 任务注册表（ThreadPoolExecutor、cancel_event、TaskCancelled、task.progress/done/error WS 广播）；`ws.py` 广播中心（lifespan 绑 loop，run_coroutine_threadsafe 线程安全）+ `broadcast_event(type,payload)` 供 mitm/剪贴板回调；`state.py` 进程内单例（store/sightings/mitm/文章缓存/last_days，core 全惰性 import）；routes 9 文件全端点，耗时操作一律 202+task_id，业务在 on_progress 分页边界 check_cancelled。
  - **shell/main.py**：uvicorn 后台线程（127.0.0.1 动态端口单 worker）→ token URL → pywebview 1180x760(min 960x640)；`--dev/--no-window/--hidden-titlebar`；关窗清理=停 mitm→关代理→executor shutdown→uvicorn 退出。
  - **测试**：`tests/server/` 63 用例全绿（happy+error+cancel+WS 推送），core/mitm/platform 全 mock（sys.modules 注入 fake）；`pytest mp_harvest/tests/server -q`。另做了真实 core 集成冒烟（--no-window + curl：platform/accounts/import/ca/settings/models 全通）。
  - core 就绪后已对齐全量真实签名：`ModelConfig(id,name,base_url,api_key,model,enabled,format)+from_dict/to_dict`、`default_export_filename(*,account_name,days,ext)`、`batch_export_articles`（HTML-only 无 fmt 参）、`settings.load_settings()`（内部走 data_dir）、导出 fmt `title+links→title_links` 映射。
  - 产出 `requirements-server.txt`。
- ✅ W3：frontend Vue3 SPA 四视图（Agent-3）— `npm run build` 成功（gzip：JS 102.7KB + CSS 4.7KB ≈ 107KB < 200KB），四视图真实浏览器验证通过（暗色/亮色 Token、倒计时、Drawer/Modal/Popover、>500 条虚拟滚动）；无后端演示：`npm run dev` 后访问 `http://localhost:5173/?mock=1`；契约假设见 frontend/README.md
- ✅ W4：契约对齐 + 依赖合并 + 端到端冒烟（Agent-4，2026-08-08）
  - **契约对齐**（以前端已实现假设为准，设计稿 §7.1 无冲突处全部对齐）：新增 `POST /api/accounts/{id}/renew`（语义参考旧 ui.renew_account：校验 __biz/url→确保 mitm 运行→reset_capture_state→store.set_awaiting）、`GET /api/mitm/status`→{running,port}、`POST /api/update/apply`（取 data/update 最新 .zip 委托 platform.updater.apply，未下载 409）；`GET /api/ai/principles`→{text,default}（default=core ai_filter.DEFAULT_PRINCIPLES）；`POST /api/accounts/import` 响应改前端形状（preview→{items:[{name,url,dup}]}，confirm→{imported,skipped}）；`GET /api/articles/export-list` 加 view 参数、改纯文本响应（RFC5987 中文文件名）。契约测试 63→**72 全绿**（新增/改写用例：renew×3、mitm.status、update.apply×3、principles default、export-list 文本/view、import 新形状）。
  - **真实 core 冒烟发现并修复**：导出 fmt 映射 `md→markdown`（core render_export 只认 markdown；此前 fake mock 未暴露）。未改 core。
  - **依赖合并**：`requirements.txt`（core+server 注释分组），uv 环境 dry-run 审计 13 包「Would make no changes」，全部模块 import 验证通过（venv 无 pip，用 `uv pip`）。
  - **端到端冒烟**（`--no-window` 真实 core + dist）：无 token /api/platform→401；带 token→能力矩阵 {os:mac,...}；GET /→SPA index.html；/api/mitm/status→{running:false,port:8088}；import preview/confirm 两段式→{imported:1,skipped:1}；export-list md→200 text/plain 带中文文件名；update/apply 未下载→409；WS 带 token 连接成功可收发、无 token 被拒（HTTP 403）。冒烟数据已清理，进程已杀。
  - 产出 `docs/API.md`（REST+WS 契约全表 + 联调备注）。发现的前端/服务端信封与字段命名差异记入 KANBAN「Epic C 遗留问题」。
- ✅ W4 补：信封/字段差异最终对齐（Agent-5，2026-08-08）— 新增 `server/mappers.py`（core 行→前端 Account/Article 适配映射，不改 core/frontend）；`GET/POST /api/accounts`、`GET /api/articles`、`POST /api/articles/supplement` 4 端点改裸数组/裸对象，字段与 types.ts 逐字段一致（`article_url→url`、`identity→id`、`link→url`、`publish_ts→date(ISO)`、`source→M/G/补`、`keep→verdict(keep/drop/null)`、ISO `expires_at`→epoch 秒、`status→pending`）；契约测试 72→**78 全绿**（新增 test_frontend_mapping.py 6 用例，改写 accounts/history/ai 相关断言）；`run_tests.py` 79/79 全绿；真实 core 冒烟（`--no-window`，8088 占位防误触系统代理）4 端点带 token 全部验证、无 token 401，数据已清理。
- ✅ W4 补2：E2E 验证轮 + 8 项缺陷修复（本机自动化，2026-08-08）— 详见 `docs/TEST_RECORD.md`：
  - **mitm 可启动**：dev 模式 CA 自动生成（`ca_setup` 用 mitmproxy CertStore 兜底）；macOS 系统代理接线（mitm_capture 委托 platform.proxy）+ 按服务备份/恢复（不再关掉用户已有 Clash 代理）；二次启动 EADDRINUSE 修复（stop 显式停 server + start 端口探测 + 捕获 SystemExit）——3 轮 start/stop 隔离复现全过。
  - **前端契约对齐**（settings.ts/types/mock）：AI 模型 GET 解包 + PUT 裸数组；settings GET 解包、save/test-proxy 字段 `proxy`；update `available/current_version/zip_url`、`ok:false→fail`；`AiModelIn.name` 放宽为默认（原型无名称输入）。
  - **M1 收尾**：pywebview 真窗口（Cocoa）开窗 + Ctrl+C 优雅退出验证通过，M1 ✅。
  - **验证**：core 79/79、server 78/78、前端 build/type-check ✅、浏览器四视图 ✅、API E2E 50 项断言全过（模拟文章服务故障复测后）。测试残留与系统代理已清理恢复（详见 TEST_RECORD 事故处置）。
  - **文档**：新增 `docs/TEST_RECORD.md`（bug 记录 + 假阴性/易踩坑项）与 `docs/USAGE_NOTES.md`（使用注意事项/系统代理管理/测试约定）；AGENTS.md §4.7 已约定后续必须同步更新。
- ⬜ W5：M5 打包/CI/在线更新

### 2026-08-09 · M2 真机验证 + AI 体验补全
- ✅ M2 凭证闭环修复：新增 `server/credential_watcher.py`（inbox→绑定→`credential.captured` WS），
  真机验证凭证捕获后自动绑定（30 分钟倒计时），详见 TEST_RECORD bug 9。
- ✅ AI 模型测试修复：`test_connection` 改 `max_retries=2` 恢复 json_object 400 兜底，
  真机 deepseek 测试 1.2s 通过，详见 TEST_RECORD bug 10。
- ✅ AI 模型列表获取：新增 `POST /api/ai/models/fetch`（OpenAI 兼容 `GET {base_url}/models`）+
  前端「获取列表」按钮 + 下拉选择（deepseek 实测返回 v4-flash/v4-pro）；core 81/81、
  server 86/86、前端 build ✅（gzip ≈ 108KB）。
- ✅ AI 模型页 UX 收口：模型输入改为「可输入可选的组合框」（datalist，不再双控件）；
  去掉失焦自动保存改手动「保存」按钮；toast 时长缩短（成功 1.2s/错误 2.5s）、删除不再双弹。
- ✅ 并行判定控制：`/api/ai/filter` 支持 `batch_size`（1–200）/ `workers`（1–16），
  历史页 AI 筛选弹层新增「每批篇数 / 并发批数」输入；server 88/88 全绿。
- ✅ AI 判定实时刷新：core `judge_articles` 新增 `on_batch` 回调，每批完成即
  合并缓存 + WS 推 `ai.batch{account_id, articles:[{id,verdict,reason}]}`，
  前端按 id 只更新 verdict/reason，行内实时变色（视图/计数联动）；默认每批 50。
- ✅ 公众号名称可留空：`AccountCreateIn.name` 放开（默认「未命名公众号」），
  前端表单不再强制填写；core 82/82、server 90/90、前端 build ✅。
- ✅ 任意目录直接运行：新增根目录 `run.py` 启动器（自动加 sys.path，不依赖 cwd）；
  shell 入口缺 `frontend/dist` 时自动构建前端；vite `base:'./'` 使资源路径相对化；
  修复 `test_paths` 模拟 Windows 时在 macOS cwd 里创建字面 `C:\Users\...` 目录的污染 bug
  （已清理仓库根垃圾目录）；从 /tmp 实测启动/页面/资源/数据目录全部正确。
- ✅ 使用手册：新增 `docs/USER_GUIDE.md`（320 行）——下载/依赖/安装/启动/抓包全流程/
  四页界面说明/代理安全/数据备份/FAQ；附 5 张演示数据截图（mock 模式，隐私安全），
  存于 `docs/screenshots/`，OCR 逐张核验无真实信息。
- ✅ 项目更名 **MP Harvest（mp-harvest）**：Python 包 `schinza` → `mp_harvest`、
  目录 `schinza/` → `mp_harvest/`、品牌文案 Schinza → MP Harvest（窗口标题/侧边栏/
  index.html title/APP_NAME/文档）；更新源改为环境变量 `MP_HARVEST_GITHUB_REPO`
  配置（未配置时检查更新返回提示，不再指向原作者仓库）；
  UI 侧边栏 + favicon 使用新图标（`frontend/public/icon.png`，200×200）；使用手册
  截图全部重新生成（OCR 核验新品牌、无隐私）；core 82/82、server 90/90、build ✅。
  内部保留 `.reference/schinza-win/` 旧参考目录与手册「致谢」中的原始项目名。
- ✅ 已上传 GitHub：仓库 **shirainbown/mp-harvest**（private，main，commit 8652076），
  更新源默认 `shirainbown/mp-harvest`（可用环境变量覆盖）；本地项目目录已建独立
  git 仓库（外层 Documents 仓库未触碰）；数据/依赖/构建产物均 gitignore 未上传。
- ✅ **M5 部分完成：macOS 发布版**：仓库已公开；PyInstaller 打包（arm64 .app，99MB）
  并修复冻结模式 `package_root()` 资源路径（兼容 PyInstaller 6 平铺/`_internal` 两种布局）；
  冒烟通过（页面/资源/API/冻结数据目录）；发布 GitHub Release **v2.0.0**：
  [MP-Harvest-mac-2.0.0.zip](https://github.com/shirainbown/mp-harvest/releases/tag/v2.0.0)
  并补发 **DMG 安装版** `MP-Harvest-mac-2.0.0.dmg`（拖入 Applications，59MB）；
  zip 供应用内自更新、dmg 供用户安装（未签名/未公证，Gatekeeper 需右键打开；
  Intel/公证/CI 留待后续）。
- ✅ **升级链路修复 + v2.0.1 发布**：`MacUpdater.apply` 安装目录改为 `.app` 包根
  的父目录（不再嵌套进 Contents/MacOS）；`/Applications` 不可写时升级脚本走
  osascript 管理员授权；开发模式禁止应用内升级；`open` 改为打开新应用；
  APP_VERSION → 2.0.1，打包版实测更新检查连通 GitHub，发布 v2.0.1（zip+dmg），
  应用内升级全链路就绪（详见 TEST_RECORD bug 11）。
- ✅ **CA 信任精确校验 + 抓包安全守卫（v2.0.2）**：`ca.status()` 改为按**证书 SHA-1
  指纹**查 trust-settings-export 的 trustList（不再按名字/issuer 匹配，开发/打包两套
  CA 不误判；verify-cert 含 CT 校验不稳定，弃用）；
  `install()` 按指纹更新/新增 trust-settings-import 条目真正写入显式信任设置；
  `enable_system_proxy()` 在 CA 未信任时拒绝切换系统代理并提示先安装——
  杜绝「一开抓包全机断网」（本机助手断连根因闭环，TEST_RECORD bug 12）。
  实测：开发 CA True / 打包版 CA 补信任后 True；core 91/91、server 90/90；
  发布 v2.0.2（重建替换资产）。
- ✅ 断连根因排查结论落盘：MP Harvest 抓包切换整机系统代理 → 本机助手（Codex）也被吸入
  mitm 中间人且 CA 未信任 → 表现为「正在重新连接」；约定真机抓包短窗口、开发用
  `set_system_proxy=False`（见 TEST_RECORD「事故根因分析」/ USAGE_NOTES §2）。

## 下一步（继承者从这里开始）

见 `docs/KANBAN.md` 的 Doing/Todo 列。

## 风险与待验证（设计稿 §11.5.6）

- ⬜ Mac 微信桌面是否走系统代理、信任用户 CA（M2 真机验证）
- ⬜ 凭证跨 IP 使用是否加速失效（必要时 Agent 增加拉取转发）
- ⬜ macOS 签名公证流水线（M5，需 $99/年 Apple 开发者账号）
- ⚠️ 测试约定：mitm 相关开发验证用 `set_system_proxy=False`，避免反复切换本机系统代理（2026-08-08 曾残留 8088 导致断网，见 TEST_RECORD）

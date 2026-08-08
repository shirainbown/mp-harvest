# MP Harvest 跨平台重构 · 技术路线与 UI 设计指导

> 版本：v2.0 重构设计稿 ｜ 日期：2026-08-08
> 目标读者：后续按本文档编写代码的开发者（即你自己）
> 输入：MP Harvest Windows 版 v1.7.7 架构文档（Python + CustomTkinter）

---

## 一、重构目标与非目标

### 1.1 目标

| # | 目标 | 说明 |
|---|---|---|
| G1 | 跨平台 | 同一套代码构建 Windows 与 macOS 双平台产物 |
| G2 | UI 兼容性好 | 界面基于 Web 技术（HTML/CSS/JS），两端渲染行为可预期、可调试 |
| G3 | 流畅性优先 | 60fps 交互、长列表不卡、后台任务不阻塞 UI、进度实时反馈 |
| G4 | 最大化复用 | 现有 Python 业务逻辑（mitmproxy 抓包、getmsg 拉取、AI 筛选、HTML 解析）尽量原样保留 |
| G5 | 导出收敛 | 文章正文导出**只保留 HTML** 一种格式，做好做精 |

### 1.2 非目标（本期不做）

- Linux 支持（架构上不封死，但不验证、不发布）
- 移动端
- 多人协作 / 云端同步（坚持「本机优先」原则）
- Word/Markdown/TXT 正文导出（列表导出多格式保留，正文仅 HTML）

---

## 二、技术路线选型

### 2.1 候选方案事实对比（2026-08 核实）

| 维度 | pywebview + FastAPI | Tauri 2 + Python sidecar | Electron + Python sidecar | PySide6 + QWebEngine | Flutter Desktop |
|---|---|---|---|---|---|
| Python 逻辑复用 | **100% 同进程** | sidecar 子进程（HTTP/stdio） | sidecar 子进程 | 100% 同进程 | 基本不可复用 |
| 渲染引擎 | WebView2 (Win) / WKWebView (mac) | WebView2 / WKWebView | 内置 Chromium 150 | 内置 Chromium | 自绘（非 Web） |
| 渲染一致性 | 两引擎有细微差异 | 同左 | **最好**（同一 Chromium） | 好 | 不适用 |
| 安装包体积 | ~40–80 MB（PyInstaller one-dir） | 壳 3–10 MB + Python sidecar | 150–250 MB 量级 | 大（QWebEngine 上百 MB） | 中 |
| 空闲内存 | 低-中 | 最低（40–80 MB 壳） | 150–400 MB | 中-高 | 中 |
| 工具链成熟度 | 6.2.1 持续更新，但**单一维护者**；打包公证需自建 | 官方 sidecar 文档、安装器、签名公证工具链最完整，v2.11.5 活跃 | electron-builder 最成熟、内置公证/自动更新 | Qt 官方维护 | 桌面稳定但 webview 插件维护差 |
| 迁移成本（从现状） | **最低**（业务层零改动，只重写 UI） | 中（需 Rust 壳 + 跨进程协议） | 中（Node 壳 + 跨进程协议） | 低（但 UI 仍需重写） | 极高（全量 Dart 重写） |
| 许可证 | BSD（pywebview）宽松 | Apache/MIT | MIT | LGPL 注意动态链接合规 | BSD |

### 2.2 决策：主路线 pywebview + FastAPI + Vue 3 SPA

**选定架构：Python 单进程（业务层 100% 复用）+ 内嵌 FastAPI/uvicorn 本地服务 + pywebview 原生窗口加载 Vue 3 SPA。**

理由（按权重排序）：

1. **迁移成本最低**：`store / history_client / ai_filter / article_reader / batch_import / sightings / settings` 等纯逻辑模块**一行不用改**，继续单元测试保护。重写的只有 `ui.py`、`ai_filter_dialog.py`、`article_list_view.py` 这三个 CustomTkinter 表现层。
2. **流畅性上限与 Tauri 相同**：两端用的渲染引擎完全一样（Win=WebView2 Chromium，mac=WKWebView），UI 流畅度取决于前端写法（虚拟滚动、CSS 动画、不阻塞主线程），不取决于壳。pywebview 不会有「Electron 级」的内存负担。
3. **通信无障碍**：同进程意味着不用设计跨进程序列化协议；前端与后端走 `http://127.0.0.1:<动态端口>` 的 REST + WebSocket，调试时可直接用浏览器打开同一地址，开发体验极佳。
4. **包体可控**：PyInstaller one-dir 产物 40–80 MB，对一个带 mitmproxy 的工具完全可接受。

**已知短板与对策**：

| 短板 | 对策 |
|---|---|
| pywebview 单一维护者 | 只用其稳定核心能力（开窗、加载 URL）；业务通信全部走 FastAPI HTTP/WS，**不依赖 js_api/evaluate_js 桥**（它有长度限制与线程坑），将来换壳成本极低 |
| 无官方打包/公证工具链 | GitHub Actions matrix 自建双平台流水线（见 §10），macOS 签名公证脚本是固定套路，一次写好长期复用 |
| 两 webview 引擎 CSS 细微差异 | 只用标准 CSS（flex/grid），不用实验特性；CI 产出两端截图人工核对 |

### 2.3 备选升级路线：Tauri 2 + Python sidecar

如果未来出现以下任一信号，可平滑升级到 Tauri 壳（业务层、FastAPI 服务、前端 SPA **三层全部原样保留**，只换壳）：

- 需要把安装包压到 20 MB 以内；
- 需要移动端（Tauri 2 支持 iOS/Android）；
- pywebview 维护停滞且出现阻断性 bug。

升级路径：PyInstaller 把现有 FastAPI 服务打成 one-dir sidecar → Tauri 官方 `externalBin` 挂载 → 前端经 `127.0.0.1` 回环访问（已有 2026 年多个生产项目验证此模式）。**这就是主路线刻意不用 js_api 桥的红利：壳是可替换的。**

### 2.4 明确排除

- **Flutter Desktop**：Python 逻辑无法同进程复用，UI 全量 Dart 重写，与本项目约束最不匹配。
- **Electron**：150–250 MB 包体 + 高内存，对「本机小工具」定位过重；渲染一致性优势对两个页面不值这个代价。
- **PySide6 + QWebEngine**：复用度虽高但包体与 Electron 同级，且 LGPL 在 PyInstaller 冻结分发下有合规注意点，QWebEngine 不能上 Mac App Store。

---

## 三、目标架构

### 3.1 分层总览

```
┌─────────────────────────────────────────────────────────────┐
│ 前端层 (frontend/)   Vue 3 + Vite SPA                        │
│   views/ 凭证管理页 · 历史文章页 · AI模型页 · 网络设置页       │
│   stores/ Pinia 状态   api/ REST+WS 封装   components/ 组件库 │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTP REST + WebSocket (127.0.0.1:动态端口)
┌──────────────────────┴──────────────────────────────────────┐
│ 服务层 (server/)     FastAPI (uvicorn 单 worker, 后台线程)    │
│   routes/ REST 端点   ws/ 进度推送   schemas/ Pydantic 契约    │
│   tasks.py 任务注册表（创建/取消/查询后台任务）                 │
└──────────────────────┬──────────────────────────────────────┘
┌──────────────────────┴──────────────────────────────────────┐
│ 业务层 (core/)       ★ 从 v1.7.7 原样迁移，零改动 ★           │
│   store · history_client · ai_filter · article_reader        │
│   batch_import · history_export · sightings · settings       │
│   capture_target · clipboard_watch                           │
└──────────────────────┬──────────────────────────────────────┘
┌──────────────────────┴──────────────────────────────────────┐
│ 基础设施层 (infra/)                                          │
│   mitm/ mitm_capture + mitm_addon（原样迁移）                 │
│   platform/ ★新增★ mac/win 差异适配：                         │
│     ca_setup.py 代理设置.py 数据目录.py 更新器.py             │
└─────────────────────────────────────────────────────────────┘
壳层 (shell/)  main.py → 启动 uvicorn 线程 + pywebview 窗口
```

**关键设计决策**：

1. **业务层零改动**：`core/` 直接平移现有代码与其 79 个测试用例，重构风险集中在服务层契约与前端。
2. **平台差异全部收敛到 `infra/platform/`**：旧 `ca_setup.py` 拆为 `ca_setup_win.py / ca_setup_mac.py` + 统一接口，UI 和业务层永远只调 `platform.ca.install() / platform.proxy.enable(port)` 这种抽象。
3. **任务注册表替代散落的线程**：v1.7.7 的 5 类后台线程统一为 `tasks.py` 管理的 Task 对象（id、类型、进度、可取消、结果），进度经 WebSocket 推给前端。UI 不再 `self.after(0, ...)` 回主线程——WS 天然异步。
4. **uvicorn 单 worker 绑 127.0.0.1 动态端口**：进程内状态（store、mitm inbox、任务表）要求单 worker；动态端口防冲突；只监听回环，防火墙不弹窗、局域网不可达。

### 3.2 进程与线程模型

```
主进程 (Python)
├── 主线程        pywebview GUI（macOS Cocoa 强制 GUI 在主线程）
├── 线程-uvicorn  FastAPI 服务（asyncio 事件循环在此线程）
│     └── 协程    REST 请求处理；耗时逻辑一律提交任务注册表
├── 线程-mitm     mitmproxy 代理（沿用现有实现，独立线程）
├── 线程-clip     剪贴板监听（沿用 ClipboardWatcher）
└── 任务池        ThreadPoolExecutor（拉历史/AI筛选/导出/更新下载）
```

规则：
- REST handler **必须毫秒级返回**；任何 >100ms 的工作创建 Task 立即返回 `task_id`；
- Task 进度变化 → WS 广播 `{type:"task.progress", task_id, percent, message}`；
- Task 完成 → WS 广播 `{type:"task.done", task_id, result_ref}`，前端再拉结果或结果直接内嵌；
- 取消：`POST /api/tasks/{id}/cancel` → 设置取消标志，业务层在分页/批次边界检查（现有历史拉取已支持中断，平移即可）。

### 3.3 关键数据流（凭证捕获，与旧版对照）

```
用户 → 前端「添加并抓包」 → POST /api/accounts (name, url)
  → 服务层：store.add_pending() + mitm.start() → 立即返回
用户在微信桌面刷新文章
  → mitm_addon 截获凭证 → capture_target 按 __biz 路由 → store 绑定(30min)
  → WS 推送 {type:"credential.captured", account_id, expires_at}
  → 前端对应行状态变「有效 · 29:59」并开始本地倒计时
```

与旧版的唯一区别：`UI.after` 轮询/回调变成 WS 推送，前端倒计时用本地 `expires_at` 计算，无需后端心跳。

### 3.4 数据存储

沿用 `data/` 全部文件格式（accounts.json / ai_models.json / ai_principles.txt / ai_filter_cache.json / settings.json / sightings.json / exports/），**文件格式不变意味着可以出迁移工具或直接拷目录**。

唯一变化：`data/` 的位置按平台规范放置：

| 平台 | 数据目录 | 说明 |
|---|---|---|
| Windows | `%APPDATA%\MP Harvest\data\` | 原来是exe同级 `data/`，改为用户目录避免 Program Files 权限问题 |
| macOS | `~/Library/Application Support/MP Harvest/data/` | 符合 Apple 规范，公证后 .app 内不可写 |

开发模式下仍在项目根 `data/`，由 `infra/platform/paths.py` 统一解析。

### 3.5 安全设计（全部沿用并强化）

- 密钥/凭证/CA 私钥 gitignored 不变；
- 服务只绑 `127.0.0.1`，且启动时生成一次性 token 注入前端页面（`http://127.0.0.1:port/?token=...`），REST/WS 校验，防止本机其他进程恶意调用（webview 方案的标配加固）；
- CA 私钥：Windows 沿用 `%USERPROFILE%\.mitmproxy\`，macOS 用 `~/.mitmproxy\`（mitmproxy 默认行为一致）。

---

## 四、双平台差异处理（infra/platform/）

| 能力 | Windows | macOS | 统一接口 |
|---|---|---|---|
| 安装 CA | `certutil -addstore -user Root ca.pem`（用户存储，**无需管理员**） | `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ca.pem`（**需 sudo，弹密码框**） | `platform.ca.install() -> InstallResult` |
| 设系统代理 | 写 HKCU 注册表 ProxyEnable/ProxyServer + InternetSetOption 刷新（**用户权限即可**） | `networksetup -setwebproxy/-setsecurewebproxy "Wi-Fi" 127.0.0.1 <port>`（**需 admin**） | `platform.proxy.enable(port) / .disable()` |
| 关代理 | 注册表复原 | `networksetup -setwebproxystate off`（注意遍历所有网络服务：Wi-Fi/USB 10/100/1000 等） | 同上 |
| 数据目录 | `%APPDATA%\MP Harvest` | `~/Library/Application Support/MP Harvest` | `platform.paths.data_dir()` |
| 打开文件/目录 | `os.startfile` | `subprocess open` | `platform.shell.open(path)` |
| 打包 | PyInstaller one-dir → zip（沿用现有方式） | PyInstaller one-dir → .app → **Developer ID 签名 + notarytool 公证 + stapler** → DMG | CI 矩阵（§10） |
| 在线更新 | 退出→替换目录→重启（沿用） | .app 整体替换需退出后脚本完成；公证后的 zip 下载解压即带签名 | `platform.updater`（流程同构） |

**macOS 权限 UX 设计**（重要）：mac 上装 CA 和设代理都要管理员密码，UI 必须明确区分状态：
- Windows：「安装 CA」按钮 → 静默完成 → 绿色对勾；
- macOS：「安装 CA」按钮 → 弹出说明「系统将请求管理员密码以信任抓包证书」→ 调起系统授权 → 结果反馈。
前端不感知命令差异，只渲染 `platform.info()` 返回的能力矩阵 `{ca_needs_admin: bool, proxy_needs_admin: bool, os: "mac"|"win"}`。

macOS 分发门槛（与方案无关的必付成本）：Apple Developer Program $99/年 + Developer ID 证书 + Hardened Runtime + 公证；**macOS 15 起未公证应用连「右键打开」都绕不过**，所以这条流水线是第一优先级的基建，不是可选项。

---

## 五、UI 完整设计

### 5.1 设计原则

1. **紧凑信息密度**：延续 v1.7.x 的紧凑单行列表哲学——这个工具的价值在于一屏看更多凭证/文章；
2. **状态永远可见**：代理状态、凭证倒计时、任务进度，全部常驻可视，不藏进对话框；
3. **操作就近**：行内操作（复制/续约/打开/删除）在行上，全局操作在工具条；
4. **零阻塞**：所有等待都有进度或骨架屏，所有成功/失败都有 toast 反馈；
5. **克制配色**：低饱和暖灰底 + 单一强调色，不喧宾夺主。

### 5.2 设计 Token

```css
/* 色彩 —— 低饱和暖灰体系 */
--bg-app:        #F7F6F3;   /* 应用底 */
--bg-panel:      #FFFFFF;   /* 卡片/面板 */
--bg-sidebar:    #F0EFEB;   /* 侧边栏 */
--bg-hover:      #EDECE8;
--bg-selected:   #E8EEF5;   /* 选中行 */
--border:        #E3E1DC;
--text-primary:  #1F2328;
--text-secondary:#6B7280;
--text-tertiary: #9CA3AF;
--accent:        #2F6FED;   /* 唯一强调色：主按钮、链接、选中 */
--accent-hover:  #2563D8;
--success:       #3F9B62;
--warning:       #C98A2C;
--danger:        #D0554F;
--info:          #5B8DEF;

/* 字体 */
--font-ui: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
--font-mono: "SF Mono", "Cascadia Mono", Consolas, monospace;  /* __biz / 链接 / 倒计时 */
--fs-xs: 11px; --fs-sm: 12px; --fs-md: 13px; --fs-lg: 15px; --fs-xl: 18px;
/* 桌面工具默认 13px 正文 —— 比网页小一号，信息密度优先 */

/* 间距（4px 基准） */
--sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px; --sp-6: 24px;

/* 圆角与阴影 */
--radius-sm: 4px; --radius-md: 6px; --radius-lg: 10px;
--shadow-pop: 0 4px 16px rgba(0,0,0,.10), 0 1px 3px rgba(0,0,0,.06);
--shadow-modal: 0 12px 40px rgba(0,0,0,.16);

/* 动效 */
--ease: cubic-bezier(.2, .8, .3, 1);
--dur-fast: 120ms; --dur-med: 200ms;
/* 只用 transform 与 opacity 做动画，禁用 width/height/top/left 过渡 */

/* 暗色模式（跟随系统，prefers-color-scheme） */
@media (prefers-color-scheme: dark) {
  --bg-app: #1B1D1F; --bg-panel: #242628; --bg-sidebar: #1F2123;
  --bg-hover: #2C2E31; --bg-selected: #2A3648; --border: #36383B;
  --text-primary: #E8EAED; --text-secondary: #9BA0A6; --text-tertiary: #6E7278;
  --accent: #5B8DEF;
}
```

### 5.3 整体布局

```
┌────────────────────────────────────────────────────────────────┐
│ ┌───────────┬───────────────────────────────────────────────┐  │
│ │           │  页头：页面标题 + 页级操作             H: 44px │  │
│ │  MP Harvest  ├───────────────────────────────────────────────┤  │
│ │           │                                               │  │
│ │  凭证管理  │                                               │  │
│ │  历史文章  │              主内容区（随页面）                │  │
│ │           │                                               │  │
│ │  ───────  │                                               │  │
│ │  AI 模型   │                                               │  │
│ │  网络设置  │                                               │  │
│ │           │                                               │  │
│ │  ───────  │                                               │  │
│ │  检查更新  │                                               │  │
│ │  v2.0.0   │                                               │  │
│ └───────────┴───────────────────────────────────────────────┘  │
│  W: 200px                        自适应                        │
└────────────────────────────────────────────────────────────────┘
窗口默认 1180×760，最小 960×640
```

- 侧边栏分三组：主功能（凭证管理/历史文章）、配置（AI 模型/网络设置）、系统（检查更新 + 版本号）；
- 选中项：左侧 3px 强调色竖条 + `--bg-selected` 底；
- macOS 下窗口用隐藏式标题栏（traffic lights 内嵌），侧边栏顶部留 28px 拖拽区；Windows 用系统标题栏即可（WebView2 自绘标题栏成本高、收益低，本期不做）。

### 5.4 页面一：凭证管理页

```
┌ 凭证管理 ──────────────────────────────────────────────────┐
│ ┌ MITM 代理 ────────────────────────────────────────────┐  │
│ │ ● 运行中 127.0.0.1:8080   CA: ✓已信任                  │  │
│ │ [停止代理] [安装CA证书] [打开证书文件]   抓包指引 ⓘ    │  │
│ └────────────────────────────────────────────────────────┘  │
│ ┌ 添加公众号 ────────────────────────────────────────────┐ │
│ │ 名称 [________________] 文章链接 [____________________] │ │
│ │ [添加并抓包]                          [批量导入 ▸]      │ │
│ └────────────────────────────────────────────────────────┘ │
│ 已添加 (6)                        [⏻ 一键续约全部]         │
│ ┌────────────────────────────────────────────────────────┐ │
│ │ ● 29:41 │ 互联网周刊      │ aXJvc2… │ 复制 续约 打开 ⋯│ │
│ │ ● 12:03 │ 芯片那些事      │ bWl0bX… │ 复制 续约 打开 ⋯│ │
│ │ ○ 已过期 │ 前端早读课      │ d2luZG… │ 复制 续约 打开 ⋯│ │
│ └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

**细节规范**：

- **MITM 面板**：状态点三色（绿=运行中 / 灰=已停止 / 黄=启动中）；CA 未信任时该行整体变 warning 底色，`[安装CA证书]` 按钮脉动强调一次；mac 下点击安装先弹权限说明气泡；
- **添加表单**：两个输入框同行，回车即提交（等同点「添加并抓包」）；提交后按钮变 loading 且表单禁用，直到 WS 推回 `credential.captured` 或 90s 超时（超时 toast 提示「未捕获到凭证，请在微信内刷新该公众号文章」）；
- **凭证行**：固定 40px 高；`●` 状态 + 倒计时用等宽字体防抖动；倒计时 <5min 变 warning 色，过期变灰；`⋯` 折叠删除（防误触，删除需二次确认 popover）；整行 hover 显 `--bg-hover`，行内按钮才浮现（默认只显示文字信息，视觉干净）；
- **批量导入**：点开是 Drawer（右侧 480px 滑出）：上方 textarea 多行粘贴、下方「从文件导入」支持 txt/csv/json；底部实时预览解析结果表格（名称/链接/是否重复）→「导入 N 条」；解析与去重逻辑完全复用 `batch_import.py`；
- **一键续约**：点击后逐条行内状态变「等待抓包…」，用户在微信里依次刷新即可，各行独立倒计时回写——无需整页刷新（WS 增量推送）。

### 5.5 页面二：历史文章页

```
┌ 历史文章 ──────────────────────────────────────────────────┐
│ 公众号 [互联网周刊      ▾]  范围 (近7天|近30天|近90天)      │
│ [⟳ 拉取历史]  ⠿ 任务: 第 3 页 / 已获 87 篇  ⏸取消          │
│ ┌────────────────────────────────────────────────────────┐ │
│ │ 视图 (全部 214 | 通过 96 | 过滤掉 118)                  │ │
│ │ 列表: [格式▾] [复制] [导出] [+补录链接] [刷新] [排序▾]  │ │
│ │ 正文: [全选] [取消选择] [导出 HTML (已选 12)]           │ │
│ │ [✦ AI 筛选] [⚙ 模型设置]  ⠿ 模型A 34/214…              │ │
│ ├────────────────────────────────────────────────────────┤ │
│ │ ☑│标题                        │AI理由          │时间│源│ │ │
│ │ ☑│国产EDA的突围之路           │行业深度        │08-07│M│↗│ │
│ │ ☐│周三例行发布会纪要           │信息密度低      │08-07│G│↗│ │
│ │ ☑│RISC-V 生态 2026 半年报     │技术趋势长文    │08-06│M│↗│ │
│ │   …（虚拟滚动，单窗口渲染 ~30 行）                      │ │
│ └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

**细节规范**：

- **拉取控制**：公众号下拉只列有有效凭证的（过期的灰显附「需续约」）；范围用分段控件（segmented control）；拉取中按钮变取消，进度文字内联显示（`第3页 / 已获87篇`），不打断布局；
- **视图切换**：三个计数徽章即按钮，当前视图加粗+强调色下划线；「导出列表」始终只导出当前视图（沿用旧语义，按钮 tooltip 明示）；
- **AI 筛选**：点击后弹出原则预览 + 「开始判定」；判定中工具条出现每模型一条迷你进度（`模型A 34/214`），完成后列表行原地填充理由列；结果缓存命中即时返回（旧版 `ai_filter_cache.json` 平移）；「模型设置」跳侧边栏 AI 模型页；
- **文章行**：36px 高；列 = 复选框 | 标题（溢出省略，tooltip 全文）| AI 理由（灰字，无理由时空）| 时间(MM-DD) | 来源徽章（M=MITM目击 / G=getmsg / 补=补录）| 行内操作（打开↗ / 复制链接 / 导出）；
- **正文导出**：仅 HTML（见 §6）；按钮文案实时带计数 `导出 HTML (已选 12)`；未勾选任何行时点击 = 导出当前视图全部（沿用旧语义，先弹确认）；
- **虚拟滚动**：>500 条启用（`@tanstack/vue-virtual`），行高固定 36px 使实现最简；500 条以内直接渲染。

### 5.6 页面三：AI 模型页（侧边栏入口）

```
┌ AI 模型 ───────────────────────────────────────────────────┐
│ [+ 添加模型]                                                │
│ ┌ 模型卡片 ──────────────────────────────────────────────┐ │
│ │ ☑启用 │ 请求地址 [https://api.xxx.com____] [测试][删除] │ │
│ │ API Key [•••••••••••• 👁]                              │ │
│ │ 格式 (OpenAI兼容|Anthropic) │ 模型 [claude-…] │ ●可用  │ │
│ └────────────────────────────────────────────────────────┘ │
│ ┌ 筛选原则 ──────────────────────────────────────────────┐ │
│ │ [多行编辑器: ai_principles.txt 内容]                    │ │
│ │ 输出格式由软件固定（严格 JSON），原则只管判定标准  ⓘ    │ │
│ │ [保存] [恢复默认]                                       │ │
│ └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

- 每个模型一张卡片，沿用旧版两行布局语义但纵向排布更适应宽度；「测试」按钮行内 spinner → ●可用（绿）/ ●失败（红+错误摘要 tooltip）；
- API Key 默认掩码、可显隐、失焦即保存（autosave + toast「已保存」）；
- 筛选原则等宽字体编辑器，保存写 `ai_principles.txt`。

### 5.7 页面四：网络设置页

```
┌ 网络设置 ──────────────────────────────────────────────────┐
│ 更新与下载代理                                              │
│ ○ 直连 / 系统代理    ● 自定义 HTTP 代理                     │
│   地址 [http://127.0.0.1:7890__________]  [测试连接]        │
│ ─────────────────────────────────────────────────────────  │
│ 平台能力                          （由 platform.info() 渲染）│
│   系统: macOS 15 · 安装CA需管理员密码 · 设置代理需管理员权限  │
└─────────────────────────────────────────────────────────────┘
```

### 5.8 检查更新

- 点击侧边栏「检查更新」→ 按钮变 spinner，结果用 Modal：无新版 → ✓ 已是最新；有新版 → 版本号 + 从 CHANGELOG 提取的更新说明（Markdown 渲染）+ `[立即更新]` `[稍后]`；
- 下载中 Modal 内进度条（走所选代理）；完成后「重启以应用」→ 沿用旧版替换脚本流程，mac 侧由 `platform.updater` 处理 .app 替换。

### 5.9 通用组件清单

| 组件 | 要点 |
|---|---|
| Button | primary/secondary/ghost/danger 四变体；loading 态内置 spinner 禁用 |
| Input / Textarea | 13px，focus 1px 强调色描边，错误态红描边 + 下方红字 |
| SegmentedControl | 时间范围、接口格式、视图切换共用 |
| Tag/Badge | 来源徽章、状态点、视图计数 |
| Toast | 右上角滑入，成功 2s/错误 4s，可叠 3 条 |
| Modal | 更新、确认导出全部；`--shadow-modal`，esc 关 |
| Drawer | 批量导入专用，右侧滑出，遮罩点击关 |
| Popover | 删除二次确认、抓包指引 ⓘ |
| Tooltip | 延迟 400ms，标题全文、错误摘要 |
| EmptyState | 空列表插画位 + 一句指引（如「先添加公众号并抓包」） |
| Skeleton | 列表加载骨架行，不用全屏 spinner |
| ProgressInline | 工具条内联文字进度（不用进度条打断工具条布局） |

### 5.10 流畅性策略（落实 G3 的具体手段）

1. **任务全部后台化**：REST 毫秒返回 + WS 推送（§3.2），UI 线程永不等待；
2. **虚拟滚动**：文章列表 >500 行启用，行高固定；
3. **增量渲染**：凭证倒计时、AI 理由回填、拉取进度都是局部响应式更新，禁止整列表重渲；
4. **动画只用 transform/opacity**，`--dur-fast/med` + `--ease`，无布局动画；
5. **乐观更新**：勾选、视图切换、排序立即生效，后台请求失败再回滚 + toast；
6. **防抖**：搜索/输入 300ms 防抖；倒计时 1s tick 用单个全局 interval 驱动所有行（不是每行一个 timer）；
7. **前端构建**：Vite 产物拆包，首屏 <200KB gzip；不用重型组件库（自绘轻组件 + Tailwind），这是流畅性与包体的双赢。

### 5.11 前端技术栈

- **Vue 3 + Vite + TypeScript**： Composition API，对 Python 背景开发者上手最平缓；
- **Pinia** 状态管理（accounts / articles / tasks / settings 四个 store）；
- **Tailwind CSS**（设计 Token 映射为 theme）+ 少量自绘组件；
- `@tanstack/vue-virtual` 虚拟滚动、`markdown-it` 渲染更新说明；
- 不建议上 React：对这个规模（4 页面 + 1 组件库）Vue 模板更省代码；React 亦可，不影响架构（前端与后端纯 HTTP 解耦，换框架不动后端）。

---

## 六、HTML 导出设计（正文唯一格式）

### 6.1 导出形态

- **单篇导出**：`exports/<公众号名>/<日期>_<标题清洗>.html`，**单文件自包含**：CSS 内联、图片默认引用微信 CDN 原链 + `referrerpolicy="no-referrer"`（可选下载本地化，见设置项）；
- **批量导出**：同上逐篇生成 + 一个 `index.html` 目录页（表格：标题/日期/链接，可点击跳各篇）；
- 导出为 Task（WS 推进度 `12/87`），可取消，完成后 toast 带「打开文件夹」按钮。

### 6.2 模板设计

微信 HTML 解析沿用 `article_reader.py`（bs4/lxml），只替换输出层为单一 HTML 模板：

```html
<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{标题} - {公众号}</title>
<style>/* 内联：阅读优化排版 */
 body{max-width:680px;margin:40px auto;padding:0 20px;
      font:16px/1.8 -apple-system,"PingFang SC","Microsoft YaHei",serif;
      color:#1f2328;background:#fff}
 img{max-width:100%;height:auto}
 .meta{color:#6b7280;font-size:13px;border-bottom:1px solid #e3e1dc;
       padding-bottom:12px;margin-bottom:24px}
 pre{overflow-x:auto;background:#f7f6f3;padding:12px;border-radius:6px}
 blockquote{border-left:3px solid #e3e1dc;margin-left:0;padding-left:16px;color:#6b7280}
 @media(prefers-color-scheme:dark){body{background:#1b1d1f;color:#e8eaed}
  .meta{color:#9ba0a6;border-color:#36383b}pre{background:#242628}}
</style></head>
<body>
<h1>{标题}</h1>
<div class="meta">{公众号} · {发布时间} · <a href="{原文链接}">原文</a></div>
<article>{正文HTML（白名单 sanitize）}</article>
</body></html>
```

要点：
- **sanitize 白名单**：保留 `p/h1-h6/img/blockquote/pre/code/ul/ol/li/table/a/strong/em/section/span` 等排版标签，剥离 script/iframe/微信跟踪参数；
- **图片处理**：`data-src` → `src`（微信懒加载属性），加 `referrerpolicy="no-referrer"` 绕防盗链；设置项「下载图片到本地」开启时图片存 `assets/` 并改写为相对路径（彻底解决链接失效）；
- 模板放 `core/templates/article.html`，Jinja2 渲染；模板本身就是预览稿——导出效果 = 读者看到的最终效果，无 Word 转换损耗。

---

## 七、API 契约（服务层 ↔ 前端）

### 7.1 REST 端点

| 方法 | 路径 | 说明 | 对应旧模块 |
|---|---|---|---|
| GET | /api/platform | 平台能力矩阵（os/权限需求/版本） | 新增 |
| GET/POST | /api/accounts | 公众号列表 / 添加（名称+链接） | store |
| POST | /api/accounts/import | 批量导入（文本或文件解析+去重预览→确认两段式） | batch_import |
| DELETE | /api/accounts/{id} | 删除 | store |
| GET | /api/accounts/{id}/credential | 复制凭证 JSON | store |
| POST | /api/mitm/start · /stop | 代理启停 | mitm_capture |
| POST | /api/ca/install | 安装 CA（返回需不需要管理员） | platform.ca |
| GET | /api/ca/status | CA 是否已信任 | platform.ca |
| POST | /api/history/fetch | 拉取历史 {account_id, days} → task_id | history_client |
| GET | /api/articles?account_id=&view=&order= | 文章列表（view: all/keep/drop） | history_client+sightings |
| POST | /api/articles/supplement | 补录链接 | sightings |
| GET | /api/articles/export-list?format= | 列表导出（json/csv/tsv/md/links/title+links） | history_export |
| POST | /api/articles/export-html | 正文 HTML 导出 {ids?} → task_id | article_reader |
| POST | /api/ai/filter | AI 筛选 {account_id} → task_id | ai_filter |
| GET/PUT | /api/ai/models · POST /api/ai/models/test | 模型 CRUD + 连通测试 | ai_filter |
| GET/PUT | /api/ai/principles | 筛选原则 | ai_filter |
| GET/PUT | /api/settings · POST /api/settings/test-proxy | 网络设置 | settings |
| GET | /api/update/check · POST /api/update/download | 更新 | updater |
| POST | /api/tasks/{id}/cancel | 取消任务 | tasks |

### 7.2 WebSocket 事件（/ws?token=）

| type | payload | 触发 |
|---|---|---|
| task.progress | {task_id, percent, message} | 拉历史/AI筛选/导出/下载中 |
| task.done | {task_id, result} | 完成 |
| task.error | {task_id, error} | 失败 |
| credential.captured | {account_id, expires_at} | MITM 截获凭证 |
| credential.expired | {account_id} | 到期（前端主要靠本地倒计时，此为兜底） |
| mitm.status | {running, port} | 代理启停状态变化 |
| clipboard.credential | {name, url} | 剪贴板目击凭证链接（弹 toast 提示入库） |

---

## 八、目录结构（重构后）

```text
mp_harvest/
├── shell/
│   └── main.py              # 入口：起 uvicorn 线程 → pywebview 开窗 → 退出清理
├── server/
│   ├── app.py               # FastAPI 装配、token 校验中间件
│   ├── routes/              # accounts.py mitm.py history.py ai.py export.py settings.py update.py
│   ├── ws.py                # WebSocket 广播中心
│   ├── tasks.py             # 任务注册表（线程池 + 取消 + 进度回调）
│   └── schemas.py           # Pydantic 请求/响应模型
├── core/                    # ★ v1.7.7 业务层原样平移（含 tests）
│   ├── store.py  history_client.py  ai_filter.py  article_reader.py
│   ├── batch_import.py  history_export.py  sightings.py  settings.py
│   ├── capture_target.py  clipboard_watch.py
│   └── templates/article.html
├── infra/
│   ├── mitm/                # mitm_capture.py mitm_addon.py（原样平移）
│   └── platform/            # ★新增：base.py / win.py / mac.py
│       ├── ca_setup.py  proxy.py  paths.py  shell_open.py  updater.py
├── frontend/                # Vue3 + Vite + TS + Tailwind
│   ├── src/views/ CredentialView.vue HistoryView.vue AiModelsView.vue NetworkView.vue
│   ├── src/components/      # 见 5.9 组件清单
│   ├── src/stores/          # accounts.ts articles.ts tasks.ts settings.ts
│   └── src/api/             # rest.ts ws.ts
├── tests/                   # 79 个旧用例平移 + server 层契约测试（httpx TestClient）
├── .github/workflows/
│   ├── build-windows.yml    # 沿用改造
│   └── build-macos.yml      # ★新增：签名+公证+DMG
├── data/                    # 开发模式数据目录（gitignored）
├── requirements.txt
└── build/                   # pyinstaller spec ×2、打包脚本 ×2
```

---

## 九、实施路线（建议 5 个里程碑）

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| M1 骨架 | shell + FastAPI + pywebview 开窗加载 Vite dev server；platform/paths、token 校验 | 双平台能开窗显示 Hello 页，浏览器可直接调试同一服务 |
| M2 凭证闭环 | platform 的 CA/代理适配 + mitm 平移 + 凭证管理页 + WS 推送 | 双平台完整跑通「装CA→开代理→抓凭证→倒计时→续约」 |
| M3 历史与导出 | history_client/sightings/导出平移 + 历史文章页（含虚拟滚动）+ HTML 导出 | 拉取 90 天 500+ 篇列表流畅滚动；HTML 导出成品达标 |
| M4 AI 与设置 | ai_filter/模型页/原则编辑/网络设置/批量导入 Drawer | AI 多模型并发判定 + 缓存命中 + 79 旧测试全绿 |
| M5 发布 | 双平台 CI（mac 签名公证 DMG / win zip）+ 在线更新 + 更新说明渲染 | tag 发布后双端均能在线更新到新版 |

每个里程碑内部顺序：先平移 core 模块并让旧测试通过 → 再写 server 契约 + 契约测试 → 最后前端页面。**业务逻辑永远先于 UI 就绪**，前端联调时后端已可信。

---

## 十、测试与构建发布

### 10.1 测试

- **core 层**：79 个旧用例原样平移，`run_tests.py` 零依赖运行器沿用；
- **server 层**：FastAPI TestClient 契约测试（每个端点至少一个 happy path + 一个错误路径；tasks 取消语义必测）；
- **platform 层**：win/mac 实现各配 mock 测试（注册表/subprocess 打桩），真机验证列入 M2/M5 人工 checklist；
- **前端**：组件级 Vitest 可选，不强求；E2E 用浏览器直接访问 dev server 手测（架构红利）。

### 10.2 构建

- **Windows**：PyInstaller one-dir（沿用现方式）→ 整个目录 zip → Release；
- **macOS**：PyInstaller one-dir → 组装 .app → `codesign --options runtime --entitlements`（Python 打包需 `disable-library-validation`）→ `xcrun notarytool submit --wait` → `stapler` → create-dmg；
- **CI**：GitHub Actions `matrix: [windows-latest, macos-latest]`，tag `v*` 触发双产物 Release，说明仍从 CHANGELOG.md 提取；CA 私钥与 Apple 签名材料全部走 Secrets（base64），不入库；
- **WebView2 说明**：Win10 1803+/Win11 已预装，无需处理；万一缺失，首次启动检测并弹系统下载链接即可（比 Tauri 的 bootstrapper 简单）。

---

## 十一、新旧模块迁移映射（速查）

| v1.7.7 模块 | 去向 | 改动量 |
|---|---|---|
| store / capture_target / history_client / ai_filter / batch_import / history_export / sightings / settings / clipboard_watch | `core/` 平移 | 零 |
| mitm_capture / mitm_addon | `infra/mitm/` 平移 | 零 |
| article_reader | `core/` 平移 + 输出层换 HTML 模板（删 docx/md/txt 分支） | 小 |
| ca_setup | 拆入 `infra/platform/{win,mac}.py` | 中（新增 mac 实现） |
| updater | `infra/platform/updater` 双实现 + server 端点 | 中 |
| ui.py / ai_filter_dialog.py / article_list_view.py | **废弃**，由 frontend/ + server/ 替代 | 重写 |
| tests（79 用例） | `tests/` 平移 | 零 |

---

## 十一点五、采集端分离拓扑（v2.1 补充）

### 11.5.1 三角色解耦

| 角色 | 职责 | 部署位置 |
|---|---|---|
| 采集端 Agent | MITM 代理、CA 私钥、凭证截取、文章目击 | **永远跟随微信端** |
| 应用端 App | UI + 历史拉取 + AI 筛选 + HTML 导出 | **位置自由**，REST/WS + token 连接采集端 |
| 微信端 | 微信桌面，人工刷新文章触发抓包 | Windows 或 macOS |

### 11.5.2 两个应用场景

- **场景一（微信在 Windows）**：采集端与微信同机。装 CA（`certutil -addstore -user`）与设代理（HKCU）均无需管理员，全自动，体验与 v1.7.7 一致；应用端可本机内嵌，也可在任意远程机器/浏览器。
- **场景二（微信在 macOS）**：采集端与微信同机，本身零权限（仅监听端口）；CA 信任与代理由用户手动 GUI 一次性完成（钥匙串「始终信任」+ 系统设置配代理）。应用端若在 Mac 上仅用浏览器访问，则零安装、零公证。

### 11.5.3 共同保证

1. 导出面板（应用端）与微信物理位置无关：凭证经接口同步后即可拉取/筛选/导出；
2. CA 分发简化：采集端自带证书下载页 `http://<agent>:8080/ca.pem`（仿 mitm.it），微信端浏览器打开即装；
3. 同一套 UI、同一份代码：单机时 Agent 内嵌不可见；远程时凭证管理页「采集端」面板切换远程地址 + token；
4. 走 headscale 组网后，「任意地方导出」从 LAN 扩展为随时随地。

### 11.5.4 采集端 API（Agent 侧新增）

| 端点 | 说明 |
|---|---|
| GET /agent/ca.pem | CA 证书下载页 |
| POST /agent/proxy/start · /stop | 代理启停 |
| WS /agent/events | 凭证捕获 / 文章目击事件推送 |
| GET /agent/info | 版本、状态、平台 |

鉴权沿用启动 token；单机内嵌模式不走网络、直调函数。

### 11.5.5 打包形态（同一份代码三种产物）

- Win/mac 应用端（内嵌 Agent，单机默认）；
- headless `mp_harvest-agent` CLI（Win/mac 独立部署）；
- Docker 镜像（网关/NAS 常驻采集端）。

### 11.5.6 待验证项（并入 M2 里程碑）

- Mac 微信桌面是否走系统代理、信任用户 CA（go/no-go）；
- 凭证跨 IP 使用（采集端 IP 产生、应用端 IP 使用）是否加速失效；若失效，Agent 增加「拉取转发」通道兜底（getmsg 请求经采集端出口发出）。

---

## 十二、一页纸结论

> **壳**：pywebview（WebView2/WKWebView 原生引擎，与 Tauri 同级渲染性能）；
> **后端**：FastAPI + uvicorn 单进程同享现有 Python 业务层（零改动复用）；
> **前端**：Vue 3 + Vite + Tailwind SPA，REST + WebSocket，虚拟滚动保流畅；
> **平台差异**：全部收敛 `infra/platform/`（CA/代理/路径/更新）；
> **导出**：正文仅 HTML，单文件自包含 + 可选图片本地化；
> **退路**：壳可随时换 Tauri（前后端均不用动）；
> **代价**：macOS 需 $99/年开发者账号做签名公证（任何方案都绕不开）。

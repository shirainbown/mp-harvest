# MP Harvest v2.0 跨平台重构 — Agent 开发指南

> 本文档是 AI/人类开发者的第一入口。开始任何工作前请先读这里 + `docs/PROGRESS.md` + `docs/KANBAN.md`。

## 1. 项目是什么

MP Harvest：公众号凭证捕获与历史文章工具。v1.7.7 是 Windows-only（Python + CustomTkinter），
本仓库将其重构为跨平台（macOS + Windows）应用。

- **设计定稿**：`../mp_harvest_refactor_design.md`（唯一权威设计文档，下称「设计稿」，引用格式 §N）
- **UI 原型**：`../index.html`（视觉/交互的唯一参照，前端必须 1:1 还原其 Token 与布局）
- **旧版参考代码**：`../.reference/schinza-win/`（**只读，禁止修改、禁止照搬 UI 层**；core 业务逻辑平移复用，致谢见 USER_GUIDE）

## 2. 技术路线（已定，勿再讨论）

壳 pywebview + FastAPI(127.0.0.1 动态端口 + 一次性 token) + Vue3/Vite/TS/Tailwind SPA。
通信只用 REST + WebSocket，**禁止用 pywebview js_api 桥**（设计稿 §2.2）。
正文导出**只有 HTML**（设计稿 §6）。

## 3. 目录结构

```
mp_harvest/
├── shell/main.py            # 入口：uvicorn 线程 + pywebview 窗口
├── server/                  # FastAPI 服务层：routes/ ws.py tasks.py schemas.py app.py
├── core/                    # ★ 业务层：从 .reference 平移，逻辑零改动（article_reader 输出层换 HTML 模板）
├── infra/
│   ├── mitm/                # mitm_capture / mitm_addon（平移）
│   └── platform/            # base.py / win.py / mac.py：ca_setup proxy paths shell_open updater
├── frontend/                # Vue3 + Vite + TS + Tailwind（四视图 + 组件库）
├── tests/                   # 旧 79 用例平移 + server 契约测试
├── docs/                    # PROGRESS.md KANBAN.md API.md
└── data/                    # 开发模式数据目录（gitignored）
```

## 4. 硬性约定

1. **包名与导入**：所有 Python 代码在包 `mp_harvest` 下，绝对导入 `from mp_harvest.core import store`。
   平移旧代码时只允许改 import 与数据目录解析（`infra.platform.paths.data_dir()`），不改业务逻辑。
2. **REST handler 毫秒级返回**；>100ms 的工作创建 Task（`server/tasks.py`），进度经 WS 推送（设计稿 §3.2）。
3. **平台差异只准出现在 `infra/platform/`**，业务/UI 只调抽象接口。
4. **Token 校验**：所有 `/api/*` 与 `/ws` 校验启动 token（query 或 header）。
5. 前端**禁用重型组件库**，自绘轻组件 + Tailwind；动画只用 transform/opacity。
6. 测试运行器：沿用零依赖 `tests/run_tests.py`；server 契约测试可用 httpx TestClient。
7. 改完必须跑测试并更新 `docs/PROGRESS.md` 与 `docs/KANBAN.md` 对应条目；测试中发现的 bug 记入 `docs/TEST_RECORD.md`，使用注意事项/行为边界更新到 `docs/USAGE_NOTES.md`。

## 5. 环境与运行

- Python：**3.13**（venv 由 `uv venv --python 3.13 .venv` 创建，可用现代类型语法）；解释器 `ROOT/.venv/bin/python`；
- 前端：Node + npm，`mp_harvest/frontend/`；
- 运行：`python -m mp_harvest.shell.main`（生产）/ `--dev http://localhost:5173`（前端热更）/ `--no-window`（仅服务，浏览器调试）。

## 6. 如何继续开发（继承流程）

1. 读 `docs/PROGRESS.md` 找当前里程碑与「下一步」；
2. 在 `docs/KANBAN.md` 认领任务，把状态改为 🚧 并署名日期；
3. 按 §4 约定实现；core 逻辑有疑问先查 `.reference/schinza-win/app/<模块>.py` 与旧测试；
4. 跑 `python tests/run_tests.py`（core）+ `pytest tests/server -q`（契约）；
5. 提交前更新两份文档状态。

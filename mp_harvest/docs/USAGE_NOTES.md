# MP Harvest 使用注意事项

> 面向使用者与开发者的注意事项汇总。Bug 记录见 `docs/TEST_RECORD.md`；本文件只记「怎么用、别踩什么坑」。
> 完整图文安装/使用手册见 **`docs/USER_GUIDE.md`**（含依赖清单、截图、FAQ）。

## 1. 首次运行

- **代码可放到任意目录直接运行**（2026-08-09 验证）：仓库根目录提供 `run.py` 启动器，
  不依赖 cwd——在任意目录执行 `python <项目路径>/run.py`（或 `cd <项目路径> && python run.py`）均可；
  生产模式若缺 `frontend/dist` 会自动 `npm install && npm run build`（需 Node.js，构建失败会给出提示）。
  前提：Python 3.13 + 依赖已装（`uv venv .venv && uv pip install -r requirements.txt`）。
  数据目录始终解析到项目内 `mp_harvest/data/`（开发模式），不会跑到启动时的 cwd。
- 开发模式数据目录：`mp_harvest/data/`（gitignored）；冻结版：`~/Library/Application Support/MP Harvest/data`（mac）/ `%APPDATA%\MP Harvest\data`（win）。
- 首次「启动抓包」会自动生成本机全新 mitmproxy CA（`data/mitm_conf/mitmproxy-ca.pem` + 公钥 `data/mitmproxy-ca-cert.cer`），无需手动放证书。
- 生成后**必须安装信任 CA** 才能抓 https：点「安装 CA 证书」→ macOS 弹系统授权框（需管理员密码）。`/api/ca/status` 显示 `installed:true` 才算装好。
- **安全守卫（v2.0.2 起）**：CA 未被系统信任时，「启动抓包」会**拒绝切换系统代理**并提示
  「请先安装并信任 CA」——不会再出现一开抓包整机 HTTPS 断网/本机助手断连。
  「安装 CA 证书」现在会写入显式信任设置（trust-settings-import），装完即真正受信任；
  状态校验按证书精确验证（verify-cert），开发版与打包版各持一把 CA 不会互相误判。
- 抓包流程：添加公众号（名称 + 带 `__biz` 的文章链接）→ 安装 CA → 启动抓包 → 微信桌面版刷新该公众号文章 → 凭证页出现「有效 · 29:59」倒计时。

## 2. 系统代理（重点）

- **抓包期间系统代理会被临时切到 `127.0.0.1:8088`**，停止抓包或正常退出应用后会自动**恢复你原来的代理设置**（按网络服务备份/恢复，2026-08-08 修复）。
- 与 Clash Verge 等代理工具共存：抓包前是什么代理（如 Clash 7897），停止后就还原成什么，不会把你原来的代理关掉。
- ⚠️ **抓包会劫持整机 https（含 Codex/OpenAI 桌面端等本机助手的连接）**：助手走系统代理，
  一旦被切到 8088 且不信任自签 CA，就会表现为「正在重新连接 / stream disconnected」
  （2026-08-08 事故根因，详见 TEST_RECORD）。**真机抓包请用短窗口**：启动抓包 → 微信刷新 →
  凭证绑定成功（约 10 秒内）→ 立即停止抓包；抓包期间不要依赖本机助手在线。
  Clash Verge 开着「系统代理」开关时会把代理写回 7897，与 8088 反复打架，属环境因素。
- **应用异常退出（崩溃/被杀）时系统代理可能残留 8088**。恢复：打开「系统设置 → 网络 → 详细信息 → 代理」关闭 Web/HTTPS 代理；或执行：

  ```bash
  networksetup -listallnetworkservices | while read s; do [ "${s#\*}" = "$s" ] || continue; networksetup -setwebproxystate "$s" off; networksetup -setsecurewebproxystate "$s" off; done
  ```

  注意：该命令会关掉所有网络服务的代理（含你原来的 Clash 7897），如需代理请再手动开 Clash。
- **抓包期间只操作微信**，不要登录银行、邮箱等敏感网站（https 流量会经过本地代理与自签 CA）。

## 3. 功能边界与已知限制

- **批量导入只识别 `mp.weixin.qq.com` 链接**（`find_mp_url` 正则限定该域名）。其他域名（本地/外链）的「名称+链接」会解析错乱，属设计行为。
- **正文链接跟踪参数剥离只针对 `*.weixin.qq.com` 域名**（chksm/scene 等），外部域名参数保留原样，属设计行为。
- 列表导出格式 key：`json / csv / tsv / md / links / title+links`；请求 `title+links` 必须 URL 编码为 `title%2Blinks`，否则 400。
- 列表导出返回**纯文本**（`text/plain` + RFC5987 中文文件名），浏览器环境为下载附件；生产 pywebview 场景可改后端写盘（未实现）。
- 「打开证书文件」按钮会调用后端 `POST /api/ca/open` 在 Finder 中打开证书目录
  （2026-08-09 补）；导出完成 toast 无「打开文件夹」按钮为已知遗留。
- 「检查更新」当前返回 `ok:false`（GitHub 未发布版本/网络），前端静默按「已是最新」处理，属预期；发版后需验证真实链路。
- **应用内升级（v2.0.1 起可用）**：检查更新 → 立即更新（下载 zip 到
  `data/update/`，走「网络设置」代理）→ 重启以应用（应用退出，脚本解压 zip 后
  替换 `.app` 所在目录并重新打开）。装在 `/Applications` 时替换会弹管理员授权框；
  源码/开发模式运行点升级会提示「开发模式不支持应用内升级」。升级后数据目录不变。
- AI 模型卡片无「名称」输入，保存时后端默认「未命名模型」；`PUT /api/ai/models` 必须发**裸数组**。
- AI 模型走 OpenAI 兼容 `json_object` 输出：DeepSeek 等模型要求 prompt 含 "json"，
  不满足时首请求会 400，应用自动去掉 `response_format` 重试一次，所以「测试」按钮
  偶尔需要 1–2 秒（2026-08-09 修复，此前测试必失败）。
- WS/REST 全部走一次性 token（query 或 Bearer）；开发调试用 `--no-window` 打印的 URL 加浏览器即可。

## 4. 开发与测试约定

- **mitm 相关开发验证一律 `MitmCaptureService.start(set_system_proxy=False)`**，不要反复切换真实系统代理（2026-08-08 曾残留 8088 导致本机断网）。
- 真机 M2 验证：启动抓包后立刻让微信刷新文章，凭证一绑定（后台日志出现
  `credential.captured` / 前端倒计时出现）马上停止抓包，把 8088 窗口压到最短；
  不要在抓包期间让本机助手/Codex 发起长请求。
- 测试结束核对：无残留 mp_harvest/mitm 进程、`scutil --proxy` 的 HTTP/HTTPS 端口符合预期、8088 无监听。
- 改代码后必须跑 `tests/run_tests.py`（core）+ `pytest tests/server -q`（契约）+ 前端 `npm run build`，并更新 `docs/PROGRESS.md`、`docs/KANBAN.md`、`docs/TEST_RECORD.md`。
- 契约以 `docs/API.md` 为准；前端 mock（`frontend/src/mock/index.ts`）必须与服务端行为一致。
- 模拟文章导出测试：本地 HTTP 服务要保证存活（导出任务会真实请求该 URL）；测试完清理 `data/` 测试产物（保留 `mitm_conf/` 与 CA）。

## 5. 待真机验证（M2 验收清单）

1. 安装 CA（管理员授权弹窗）→ `/api/ca/status` installed。
2. 启动抓包 → 系统代理切 8088 → 微信桌面打开公众号文章 → 凭证捕获 → 页面倒计时。
3. 续约：凭证过期/重置后再次刷新微信 → 状态恢复「有效」。
4. 停止抓包/退出应用 → 系统代理恢复原设置（含已有 Clash 代理）。
5. 90 天 500+ 篇历史拉取 + 虚拟滚动流畅度；HTML 正文导出（图片本地化可选）。

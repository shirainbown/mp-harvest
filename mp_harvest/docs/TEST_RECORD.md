# MP Harvest 测试记录

> 每次人工/自动化测试轮结束追加一节。记录：范围、环境、结果、发现的问题、处置。

## 2026-08-09 · M2 真机验证轮（macOS + 微信桌面）

**范围**：M2 核心链路真机验证：装 CA → 启动抓包 → 微信桌面刷新 → 凭证捕获 → 倒计时 → 续约。

**环境**：macOS 本机，真实 core + frontend/dist，pywebview 窗口，微信桌面版。

### 结果

| 步骤 | 结果 |
|---|---|
| CA 信任（此前 exit 60 阻塞项） | ✅ 修复：`security trust-settings-import -d` 导入显式信任（SSL + Apple X509 Basic = TrustRoot） |
| mitm 启动 + 系统代理 8088 | ✅ scutil 确认 Wi-Fi/Thunderbolt 等全部服务切 8088 |
| 微信真机刷新 → 凭证捕获 | ✅ 多组 `__biz/uin/key/pass_ticket/appmsg_token` 写入 `capture_inbox.json` |
| 凭证绑定 + 倒计时 | ❌ 前端永远「抓包中」——**服务端缺消费闭环**（见 bug 9） |

### 发现并已修复的问题

9. **凭证捕获后无人消费（M2 阻塞，真机暴露）**：mitm addon 只把凭证写进
   `capture_inbox.json`，服务端没有任何模块读取并绑定（`store.apply_credentials` 无人调用），
   也没有推 `credential.captured` WS 事件；前端一直等该事件 → 界面永远「抓包中」。
   修复：新增 `server/credential_watcher.py`（lifespan 启动守护线程，1s 轮询 inbox →
   按 `__biz` 精确匹配/唯一 awaiting 兜底 → `apply_credentials` → `ack_inbox` →
   广播 `credential.captured{account_id, expires_at(epoch秒)}`），接入 `server/app.py`
   lifespan 启停；新增 5 条契约测试，server 套件 78→83 全绿。
10. **AI 模型「测试」必失败（真机暴露）**：OpenAI 格式 payload 固定带
    `response_format: {"type":"json_object"}`，而 DeepSeek 要求 prompt 里必须出现
    "json" 字样，否则 400（真实报错 `Prompt must contain the word 'json' ...`）。
    `_call_model` 有「400 且响应体含 json → 去掉 response_format 重试」的兜底，
    但 `test_connection` 传了 `max_retries=1`（只循环一次），兜底重试被直接掐掉，
    于是测试必显示失败；真实筛选用 3 次重试所以不受影响。修复：`test_connection`
    改 `max_retries=2` 留出兜底机会；新增回归测试（模拟 400→去 response_format
    重试成功），core 套件 79→80 全绿；真机验证测试接口
    `{"ok":true,"message":"连接成功（1.2s）：'OK'"}`。
11. **macOS 打包版「重启以应用」替换位置错误（代码审查发现）**：`MacUpdater.apply`
    把安装目录算成 `sys.executable` 的父目录——对 `.app` 来说是 `Contents/MacOS`，
    新应用会被嵌套进旧包内部，而不是替换 `/Applications/MP Harvest.app`；
    且装在 `/Applications`（root 属主）时普通权限 rm/mv 必然失败。
    修复：新增 `_app_bundle_root`/`_mac_install_dir`（向上找 `.app` 根、取父目录为
    安装目录）；目标不可写时升级脚本用 `osascript ... with administrator privileges`
    弹管理员授权完成替换；开发模式直接抛「不支持应用内升级」；替换后 `open` 新应用
    而非目录；win 同步加开发模式保护。新增 5 条单测（bundle 定位/脚本内容/开发模式
    拒绝），core 82→87 全绿；发布 **v2.0.1**（APP_VERSION 2.0.1），打包版实测
    「检查更新」连通 GitHub、下载/替换/重启链路就绪。
12. **CA 信任判定不准确 → 未信任证书也会放行抓包（本机断连根因复盘）**：
    (a) `ca.status()` 只查「钥匙串里存在 mitmproxy 证书」，不查信任设置——
    之前 `add-trusted-cert` 在 macOS 上建不出信任设置时状态仍报已安装；
    (b) 改为按名字匹配 dump-trust-settings 后，开发/打包版两套数据目录各有一把 CA，
    会被另一把的信任条目误判；且 `security verify-cert` 含 CT/网络校验、同一证书
    结果不稳定（实测同一命令一次 exit=0 一次 exit=1），不能用作守卫。
    修复：`status()` 改按**证书 SHA-1 指纹**查 trust-settings-export 的 trustList
    （key 即指纹，确定性精确）；`install()` 在 add-trusted-cert 后按指纹更新或
    **新增**信任条目再 `trust-settings-import`（mkcert 同款），装完即真正受信任；
    `enable_system_proxy()` 加守卫：CA 未信任时**拒绝切换系统代理**并提示先安装。
    实测：开发 CA trusted=True、打包版 CA（未补信任前 False → 补信任后 True）；
    发布 v2.0.2（重建替换资产）。
13. **凭证过期从不推送 + CA 目录无法打开 + 更新弹窗文案（代码复查）**：
    (a) 前端有 `credential.expired` 处理但服务端从不发该事件——补 `sweep_expired()`
    随 watcher 轮询标记过期账号并广播（新增单测）；
    (b) 「打开证书文件」按钮是禁用占位——补 `POST /api/ca/open`（平台 shell_open
    打开证书目录）+ 前端接线（新增契约测试）；
    (c) 更新弹窗下载文案是旧包名 `mp_harvest-*.zip`——改为通用「更新包 vX」；
    (d) 公开仓库无 README——新增根目录 `README.md`（功能/下载/运行/文档/致谢）。
    **连带修复测试基建坑**：watcher 线程在测试环境里首次导入真实 `core.store` 后，
    父包 `mp_harvest.core` 会绑定真实模块属性，此后 fake_core 仅注入 `sys.modules`
    失效（`from pkg import mod` 优先取父包属性）→ 后续测试拿到真实 store 全挂。
    修复：fake_core 注入时同步 `monkeypatch.setattr(parent, attr, fake, raising=False)`
    绑定父包属性；`sweep_expired` 对无 `mark_expired_if_needed` 的 store 防御性跳过。

### 事故根因分析：为什么用 MP Harvest 会让 Codex/本机助手断连

证据链（2026-08-09 复核）：
- 本机助手（Codex 桌面端）的 NetworkService 进程当前所有 API 连接都走
  `127.0.0.1:7897`（Clash Verge，lsof 确认多条 ESTABLISHED）；
- MP Harvest「启动抓包」会把**所有网络服务**的系统代理切到 `127.0.0.1:8088`
  （mitmproxy），于是助手的新连接也被吸进 8088；
- mitmproxy 会用自签 CA 对 https 做中间人。此前该 CA 未受信任 → TLS 握手失败，
  助手表现为 `stream disconnected before completion: error sending request`，
  并进入「正在重新连接」；只要 8088 残留，重试全部失败，看起来一直断连；
- 另外，抓包期间切换代理会掐断已建立的长连接（SSE 流），Clash Verge 与
  MP Harvest 互相改写系统代理也会造成反复闪断。

**处置**：
- 已修：代理按服务备份/恢复 + 退出清理（2026-08-08）+ CA 信任（本轮）；
- 约定（已写入 USAGE_NOTES §2/§4）：真机抓包要短窗口操作（启动 → 微信刷新 →
  凭证绑定即停），抓包期间不要依赖助手在线；开发验证一律
  `set_system_proxy=False`；
- 用户侧注意：Clash Verge 开着「系统代理」开关时会不停把代理写回 7897，
  「关闭本机代理」操作不会持久，属环境因素不是 MP Harvest bug。

### 遗留（继续真机验证）

- ⬜ 凭证绑定 → 倒计时（bug 9 修复后需复测）
- ⬜ 续约：凭证过期/重置后再次刷新微信 → 恢复「有效」
- ⬜ 停止抓包/退出应用 → 系统代理恢复原设置（含已有 Clash 7897）
- ⬜ 90 天 500+ 篇历史拉取与虚拟滚动压力（M3）
- ⬜ AI 筛选用真实 key 全链路（D4 已验结构化错误路径）
- ⬜ M5 打包/CI/在线更新

## 2026-08-08 · E2E 验证轮（macOS 开发模式）

**范围**：M1（服务 + SPA + pywebview 窗口）、M2（CA/mitm/系统代理）、M3（文章/导出）、M4（AI/设置/更新）的 API 级端到端验证 + 前端四视图浏览器验证。

**环境**：macOS 本机，开发模式（`mp_harvest/data/`），真实 core + frontend/dist，FastAPI 动态端口。

### 结果

| 套件 | 结果 |
|---|---|
| core `run_tests.py` | 79/79 ✅ |
| server 契约 `pytest tests/server` | 78/78 ✅ |
| 前端 `vue-tsc` + `vite build` | ✅（gzip ≈ 107KB） |
| 浏览器四视图（真实后端） | ✅ 凭证/历史/AI/网络四页渲染正常 |
| API E2E（50 项断言） | ✅ 50/50（其中 2 项在本地模拟文章服务重启后复测通过） |
| pywebview 真窗口（M1 收尾） | ✅ Cocoa 引擎开窗、服务响应正常、Ctrl+C 优雅退出、代理未受影响 |

### 发现并已修复的问题

1. **dev 模式 mitm 无法启动（阻塞 M2）**：`prepare_mitm_confdir` 只查找捆绑/相邻 CA，开发环境无捆绑证书 → 报「未找到代理 CA」。旧版 Windows 是随包附带 p12。修复：`ca_setup.py` 增加第 5 分支——用 `mitmproxy.certs.CertStore.from_store(confdir, basename="mitmproxy", key_size=2048)` 本机自动生成 CA 并导出公钥 `.cer`。
2. **macOS 抓包不设系统代理**：`MitmCaptureService.enable_system_proxy()` 是 Windows-only（winreg），mac 上直接返回「请手动设置代理」；platform 层的 `MacProxyManager` 无人调用。修复：mitm_capture 的 enable/restore 委托 `get_platform().proxy`，win/mac 统一走平台层。
3. **`MacProxyManager.disable()` 直接关代理、不恢复原设置**：用户本来开着 Clash（7897）时，用一次抓包后代理会被关掉。修复：enable 时按网络服务备份（web/secure 的 Enabled/Server/Port），disable 恢复原状；无备份（非本应用开启）时安全 no-op。
4. **mitm 二次启动 EADDRINUSE（真实 crash）**：mitmproxy 的 proxyserver addon 没有 done 钩子，`master.shutdown()` 后监听 socket 不关闭；进程内二次启动绑 8088 失败，ErrorCheck `sys.exit(1)`（BaseException，`except Exception` 抓不到），线程静默退出，且 `_started` 在 bind 前就 set → start() 误报成功。修复：stop() 在事件循环内先 `proxyserver.servers.update([])` 显式停 server 再 shutdown；start() 改为 TCP 探测 8088 就绪（替代过早的 started 事件）；捕获 SystemExit 写入 `_start_error`。隔离复现 3 轮 start/stop 全部通过。
5. **前端 AI 模型契约不匹配**：`GET /api/ai/models` 服务端返回 `{models:[...]}`、前端按裸数组解析；`PUT` 服务端要裸数组、前端发 `{models:[...]}`（保存必 422）。修复：`stores/settings.ts` 解包/裸数组发送，mock 同步对齐。
6. **前端网络设置契约不匹配**：`GET /api/settings` 服务端返回 `{settings:{...}}`，前端未解包（mode/proxy_url 全 undefined）；`test-proxy` 前端发 `{mode,proxy_url}`、服务端要 `{proxy}`。修复：load 解包并映射 `proxy→proxy_url`，save 存 `{mode, proxy}`，test-proxy 发 `{proxy}`。
7. **更新检查/下载契约不匹配**：前端 `UpdateCheckResult.has_update` vs 服务端 `available`（检查更新永远弹不出弹窗）；下载发 `{version}` vs 服务端 `{zip_url}`（必 422）。修复：types/store/mock 对齐为 `available/current_version/zip_url`，`ok:false` 时返回 fail。
8. **`AiModelIn.name` 必填但原型卡片无名称输入**：保存模型必 422。修复：服务端 name 默认空串（core `ModelConfig.from_dict` 默认「未命名模型」），422 契约用例改用 `{"name": 123}` 保持覆盖。
9. **CA 状态前后端字段不一致（界面永远显示「未信任」）**：`GET /api/ca/status` 返回
   `{installed, cert_path, needs_admin}`，而前端 `CaStatus`/凭证页只读 `ca.trusted`
   ——`trusted` 恒为 undefined，即使 CA 已装好且系统信任，界面也一直显示「未信任」、
   「安装 CA」按钮常驻。修复：服务端契约双写，`trusted` 与 `installed` 同值返回
   （API.md 同步），契约测试补 `data["trusted"] is True` 断言。
   新用户模拟验证：清空两把 CA（数据目录 + 系统/登录钥匙串 + 信任设置）→
  `status=False` → 启动抓包生成新 CA → 安装信任 → `status=True` 全链路通过。
10. **新用户「安装 CA 证书」一键流程三处加固（v2.0.3 实测暴露）**：
    - 证书缺失时 `install()` 报「请先启动一次抓包」，新用户必须理解先后顺序；改为
      **自动调用 `prepare_mitm_confdir` 生成 CA**，点「安装」即可（发现原代码引错模块：
      `ca_setup.app_root()` 应为 `paths.app_root()`，已修）。
    - `_patch_trust_plist` **新增条目缺 `modDate`**，`trust-settings-import` 报
      「Trust Settings Record was corrupted」（仅当 add-trusted-cert 未先建条目时触发）；
      补写 `modDate` 后从零新增条目可正常导入。
    - 主路径（osascript 管理员 + 系统钥匙串）非取消失败时不再提前 return，继续补
      显式信任设置；仍未生效则降级「登录钥匙串 + 显式信任设置」（无需管理员）。
    真机验收（新用户全链路）：清空全部 CA/信任 → 启动应用 `trusted:false` →
    点「安装 CA 证书」输入一次管理员密码 → `trusted:true`；应用内「抓包指引」同步
    改为首次使用三步（安装 CA → 启动代理 → 微信刷新文章）。

### 测试环境事故与处置（重要）

- **系统代理残留 8088**：E2E 用真实 mitm 会把系统代理切到 127.0.0.1:8088；修复前的 crash 与一次漏停 mitm 导致 8088 残留，期间本机网络（Codex 桌面端连接）被中断，表现为「正在重新连接」。已清理全部测试进程并把系统代理恢复到用户原状（Wi-Fi 127.0.0.1:7897 = Clash Verge，其余服务关闭）。**后续测试约定：mitm 相关验证一律 `set_system_proxy=False` 或测试结束立即核对恢复；不在开发测试中反复切换用户系统代理。**
- Clash Verge 配置核对：`mixed-port: 7897`，`enable_system_proxy: false`——8088 并非 Clash Verge 设置，是 MP Harvest 测试残留。

### 测试中的假阴性 / 易误解项（避免重复踩坑）

- **`export-list` 的 `title+links` 必须 URL 编码为 `title%2Blinks`**：query 里裸 `+` 会解码成空格 → 400。前端会自动编码，curl/脚本测试要手动编码。
- **跟踪参数剥离只对 `*.weixin.qq.com` 链接生效**（TRACKING_PARAMS：chksm/scene/…；`utm_*` 不在列表内）。用 example.com 链接断言剥离会误报 FAIL，属设计行为。
- **批量导入只识别 `mp.weixin.qq.com` 链接**（`_URL_RE` 限定域名）。用 127.0.0.1 等本地链接测试预览会解析错乱（行被吞并），不是 bug。
- **系统代理核对要按服务枚举**：`networksetup -listallnetworkservices` 的服务名可能带空格（如 "Thunderbolt Bridge"），shell 直接 for 循环会拆词；用 Python/subprocess 按行处理。
- **模拟文章导出测试**：导出任务会真实请求文章 URL，本地 HTTP 服务必须保活；服务挂了任务会 `ok:0 failed:1`（网络错误），不是导出逻辑 bug。
- **服务端契约测试用 fake 平台（sys.modules 注入）**，代理/CA 真实行为只有真实 core 冒烟能暴露（本轮 EADDRINUSE 与 CA 缺失都是真实环境才暴露的）。
- 用户已有的系统代理（如 Clash 7897）在测试前必须先记录，结束后核对；**不要把「测试前就有代理」当成残留**。

### 遗留（需真机/人工）

- ⬜ CA 安装信任（macOS 弹管理员授权框）未自动化验证，需用户授权一次。
- ⬜ 微信桌面真机抓包 → 凭证绑定 → 倒计时 → 续约全链路（M2 核心验收）。
- ⬜ 90 天 500+ 篇历史拉取与虚拟滚动压力（M3）。
- ⬜ AI 筛选用真实 key 全链路（D4 已验结构化错误路径；用户实测 7897 代理到 deepseek HTTP 200）。
- ⬜ M5 打包/CI/在线更新。

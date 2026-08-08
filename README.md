# MP Harvest

公众号凭证捕获与历史文章工具（macOS / Windows 跨平台重构版）。

## 功能

- **凭证捕获**：微信桌面版打开公众号文章，自动捕获 `__biz / uin / key / pass_ticket / appmsg_token`，30 分钟有效，一键续约；
- **历史文章**：近 7 / 30 / 90 天拉取，500+ 篇虚拟滚动流畅；
- **AI 筛选**：DeepSeek / OpenAI 兼容模型，每批篇数（默认 50）与并发批数可调，每批结果实时刷新；
- **导出**：列表（Markdown / JSON / CSV / TSV / 纯链接 / 标题+链接）+ HTML 正文导出；
- **安全守卫**：CA 未受信任时拒绝开启抓包，杜绝整机 HTTPS 断网。

## 下载与安装

- 最新发布：[GitHub Releases](https://github.com/shirainbown/mp-harvest/releases)（macOS Apple Silicon，DMG / ZIP）
- 使用手册（含依赖清单、截图、FAQ）：[docs/USER_GUIDE.md](mp_harvest/docs/USER_GUIDE.md)

## 从源码运行

```bash
uv venv .venv --python 3.13
uv pip install -r requirements.txt
python run.py            # 生产模式（首次自动构建前端，需 Node.js）
```

任意目录均可运行（`run.py` 自动定位项目根）。详见使用手册。

## 文档

- `docs/USER_GUIDE.md` — 图文使用手册
- `docs/USAGE_NOTES.md` — 使用注意事项（系统代理 / CA 信任 / 安全）
- `docs/API.md` — REST / WS 接口契约
- `docs/TEST_RECORD.md` — 测试记录与事故复盘
- `docs/PROGRESS.md` / `docs/KANBAN.md` — 进度与任务看板

## 致谢

核心业务逻辑脱胎于早期 Windows 版 [schinza-wechat-certificate](https://github.com/Alexxxxxxxxxxxxy/schinza-wechat-certificate)（原作者 Alexxxxxxxxxxxxy），在此致谢；界面、服务层与平台适配均为本项目独立实现。

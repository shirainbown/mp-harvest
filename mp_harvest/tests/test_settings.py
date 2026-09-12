"""应用本地设置读写测试。"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.settings import load_settings, save_settings, settings_path  # noqa: E402


def test_settings_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert settings_path(root).name == "settings.json"
        assert load_settings(root) == {}
        save_settings(root, {"proxy": "http://127.0.0.1:7897"})
        assert load_settings(root)["proxy"] == "http://127.0.0.1:7897"


def test_load_settings_invalid_file():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = settings_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("不是 JSON", encoding="utf-8")
        assert load_settings(root) == {}


# ── 设置项接线（2026-09）──────────────────────────────────────────
#
# 「拉取历史」那一组键是用户明确要求放进设置页的（微信限流的应对）。配置项
# 最容易出的一类问题是**静默失灵**：键名在某一侧打错/漏登记，界面上照样能填、
# 保存也返回 200，但值从来没被读过 —— 用户看到的是「改了没用」。

SETTINGS_TS = (
    Path(__file__).resolve().parents[2]
    / "mp_harvest" / "frontend" / "src" / "stores" / "settings.ts"
)


def _settings_ts_body(fn_name: str) -> str:
    """截出 settings.ts 里某个方法的函数体（按缩进找下一个同级方法）。

    不能直接 `index(fn_name + "(")` —— 调用点（`this._applyRawSettings(s)`）
    会先被找到，截出来的片段里一个键都没有，那种测试是**空转的**。
    """
    src = SETTINGS_TS.read_text(encoding="utf-8")
    m = re.search(rf"\n    {re.escape(fn_name)}\(", src)
    assert m, f"settings.ts 里找不到 {fn_name} 的定义"
    nxt = re.search(r"\n    [A-Za-z_$][\w$]*\(", src[m.end():])
    return src[m.end(): m.end() + (nxt.start() if nxt else len(src))]


def _settings_ts_keys(fn_name: str) -> set[str]:
    """函数体里出现的 'ns.key' 字面量（读取与写入两侧的键名）。"""
    body = _settings_ts_body(fn_name)
    return set(re.findall(r"'([a-z]+\.[a-z_]+)'", body))


def test_frontend_reads_and_writes_the_same_setting_keys():
    """`_applyRawSettings`（读）与 `_mergedSettings`（写）的键集必须一模一样。

    少一个键的后果不对称，但都很难看：
    - 只在**读**里有：用户改完存下去，下一次任何保存都会用 rawSettings 里的旧值
      把它顶回去 —— 「改了没用」。
    - 只在**写**里有：值被推到后端，但界面永远拿不到，刷新后打回默认。
    两种都不报错，所以只能靠这条钉子拦。
    """
    read = _settings_ts_keys("_applyRawSettings")
    written = _settings_ts_keys("_mergedSettings")
    assert read == written, (
        f"只读不写：{sorted(read - written)}；只写不读：{sorted(written - read)}"
    )


def test_every_frontend_setting_key_exists_on_the_backend():
    """前端键名必须是后端登记过的，否则 PUT 进了 settings.json 也没人读。

    后端对未知键**不报错**（设计如此：允许前端先带键），所以拼错一个字母
    不会有任何提示 —— 值静静地存进文件、静静地谁也不用。

    读写**两侧都要查**：只查写入侧的话，一个在两侧都拼错的键（read 和 write
    一致，所以键集相等那条钉子也不响）会被完整漏掉 —— 界面能填、能存、
    存进去没人读，全程零报错。
    """
    from mp_harvest.server.routes.settings import SETTING_DEFAULTS

    known = set(SETTING_DEFAULTS)
    for fn in ("_applyRawSettings", "_mergedSettings"):
        unknown = sorted(_settings_ts_keys(fn) - known)
        assert not unknown, f"{fn} 里用了后端没登记的设置键：{unknown}"


def test_fetch_settings_are_exposed_in_the_settings_page():
    """拉取节奏的每个后端键都要在设置页里能改（用户明确要求的）。

    加了后端键却忘了接前端 = 用户看不到、也改不了，等于这个配置项不存在 ——
    而默认值恰好是「不能用」，出了问题没人能调。
    """
    from mp_harvest.server.routes.settings import SETTING_DEFAULTS

    fetch_keys = {k for k in SETTING_DEFAULTS if k.startswith("fetch.")}
    assert fetch_keys, "fetch.* 设置键不见了"
    read = _settings_ts_keys("_applyRawSettings")
    assert not (fetch_keys - read), f"这些键没接到设置页：{sorted(fetch_keys - read)}"


def test_backend_default_keys_are_all_typed():
    """`SETTING_DEFAULTS` 与 `_SETTING_TYPES` 必须一一对应。

    漏登记类型的键会走「未知键」分支：只校验是标量，类型随便填。对 int 键而言
    后果是字符串 `"5"` 被原样写进 settings.json，读的时候 int() 抛异常 →
    悄悄回退成默认值（而默认值恰好是合法值，所以看不出来）。
    """
    from mp_harvest.server.routes.settings import _SETTING_TYPES, SETTING_DEFAULTS

    missing = sorted(set(SETTING_DEFAULTS) - set(_SETTING_TYPES))
    assert not missing, f"这些设置键没有声明类型：{missing}"
    stale = sorted(set(_SETTING_TYPES) - set(SETTING_DEFAULTS))
    assert not stale, f"这些类型声明没有对应的默认值：{stale}"


def test_frontend_fetch_defaults_match_backend():
    """设置页里写的初值必须等于后端 SETTING_DEFAULTS。

    这六个值在两边各存了一份（前端要先渲染出来、后端要能独立回答）。
    不一致时的表现是：全新安装看到的数字是一套，用户没动过的那些键实际生效的
    是另一套 —— 而「按界面默认值填」正是用户排查问题时的第一反应。
    """
    from mp_harvest.server.routes.settings import SETTING_DEFAULTS

    src = SETTINGS_TS.read_text(encoding="utf-8")
    pairs = {
        "fetchDelayMin": "fetch.delay_min",
        "fetchDelayMax": "fetch.delay_max",
        "fetchCooldownPages": "fetch.cooldown_pages",
        "fetchCooldownSeconds": "fetch.cooldown_seconds",
        "fetchRetries": "fetch.retries",
        "fetchMaxPages": "fetch.max_pages",
    }
    for pref, key in pairs.items():
        m = re.search(rf"\b{pref}: (\d+)", src)
        assert m, f"settings.ts 里找不到 {pref} 的初值"
        assert int(m.group(1)) == int(SETTING_DEFAULTS[key]), (
            f"{pref} 前端初值 {m.group(1)} ≠ 后端 {key}={SETTING_DEFAULTS[key]}"
        )

"""界面文案里的平台假设（2026-09，做 Windows 版时加的静态检查）。

界面要「两个平台看起来一样」，但**平台本来就不同的地方不能硬写成一样** ——
典型是 CA 信任：macOS 要弹管理员授权框，Windows 只是往当前用户存储里加一条，
全程不要密码。写死一边，另一边的人就照着做一件永远不会发生的事。

这组是**静态检查**：盯住「这段文案是数据驱动的」，而不是真的渲染一遍。
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "mp_harvest" / "frontend"
CREDENTIAL = FRONTEND / "src" / "views" / "CredentialView.vue"
MOCK = FRONTEND / "src" / "mock" / "index.ts"


def test_ca_step_text_is_driven_by_platform():
    """「安装 CA 证书」那一步不能写死「输入管理员密码」。"""
    text = CREDENTIAL.read_text(encoding="utf-8")
    assert "caStepText" in text, "凭证页的 CA 步骤文案应该是算出来的"
    assert "ca_needs_admin" in text, "要读后端给的平台能力，不能猜平台"
    # 模板里引用算出来的文案，而不是把 mac 的说法直接写在标签里
    assert "{{ caStepText }}" in text
    assert not re.search(r"1\.\s*点「安装 CA 证书」，输入管理员密码", text), (
        "带管理员密码的写法只该出现在 caStepText 的 needs_admin 分支里"
    )


def test_ca_step_text_covers_both_platforms():
    """两个分支都要有，否则 Windows 用户会看到 mac 的说法。"""
    text = CREDENTIAL.read_text(encoding="utf-8")
    body = text.split("const caStepText", 1)[1].split("})", 1)[0]
    assert "needsAdmin === true" in body
    assert "needsAdmin === false" in body, "少了 Windows（无需管理员）那一支"
    assert "无需管理员" in body


def test_mock_platform_follows_the_real_os():
    """演示模式（?mock=1）不能写死 macOS。

    写死的话，在 Windows 上开演示，「设置 → 平台能力」会报出一个假的平台，
    凭证页那句提示也跟着变成错的。
    """
    text = MOCK.read_text(encoding="utf-8")
    assert "isWindows" in text, "mock 的平台信息应该跟着运行环境走"
    assert "navigator.userAgent" in text
    # ca_needs_admin 必须与 os 一致，不能一个跟着环境走、另一个写死
    assert "ca_needs_admin: !isWindows" in text


def test_mock_implements_the_endpoints_the_ui_calls():
    """UI 会调的端点，mock 里不能缺 —— 缺了会走到「未实现的端点」直接抛。"""
    text = MOCK.read_text(encoding="utf-8")
    for endpoint in ("/api/shell/open", "/api/shell/reveal", "/api/ca/open"):
        assert f"'{endpoint}'" in text, f"mock 没实现 {endpoint}"

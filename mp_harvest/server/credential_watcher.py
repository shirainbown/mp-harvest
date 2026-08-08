"""抓包凭证轮询闭环（M2 核心链路，2026-08-08 真机验证补上）。

mitm addon 只负责把捕获到的凭证写入 ``capture_inbox.json``；本模块是它的
消费者：轮询 inbox → 挑选等待抓包的公众号 → ``store.apply_credentials`` 绑定
（expires_at = now + 30min，status=active）→ 经 WS 推送 ``credential.captured``，
前端据此刷新倒计时。

之前缺失该环节：前端一直等 ``credential.captured``，凭证停在 inbox 无人消费，
界面永远「抓包中」（M2 真机验证发现，见 docs/TEST_RECORD.md）。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from mp_harvest.server import state
from mp_harvest.server.ws import broadcast_event

POLL_INTERVAL = 1.0


def _epoch_seconds(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except Exception:  # noqa: BLE001
        return None


def pick_awaiting_target(creds: dict[str, str]):
    """挑要绑定的等待账号：优先 ``__biz`` 精确匹配，其次唯一 awaiting 兜底。"""
    from mp_harvest.core import capture_target

    biz = str(creds.get("__biz") or "")
    awaiting = [
        a for a in state.get_store().list_accounts() if a.get("status") == "awaiting"
    ]
    if not awaiting:
        return None
    if biz:
        for a in awaiting:
            if capture_target.expected_biz(a) == biz:
                return a
    if len(awaiting) == 1:
        return awaiting[0]
    return None


def poll_once() -> bool:
    """读一次 inbox；有新凭证且成功绑定并推送返回 True。"""
    svc = state.get_mitm()
    creds = svc.read_new_credentials(consume=False)
    if not creds:
        return False
    account = pick_awaiting_target(creds)
    if account is None:
        return False
    row = state.get_store().apply_credentials(account["id"], creds)
    if row is None:
        return False
    svc.ack_inbox()  # 标记已消费，避免重复绑定
    print(
        f"[mp_harvest] 凭证已绑定 {row.get('name')} expires={row.get('expires_at')}",
        flush=True,
    )
    expires_at = _epoch_seconds(row.get("expires_at"))
    if expires_at:
        broadcast_event(
            "credential.captured",
            {"account_id": row["id"], "expires_at": expires_at},
        )
    return True


class CredentialWatcher:
    """守护线程轮询 inbox（服务 lifespan 启停）。"""

    def __init__(self, interval: float = POLL_INTERVAL) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="mp_harvest-cred-watcher", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                poll_once()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self._interval)

"""credential_watcher 闭环契约：inbox → 绑定 → WS 推送（M2 真机验证补测）。"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta

from mp_harvest.server import credential_watcher as watcher_mod
from mp_harvest.server import state


class FakeSvc:
    def __init__(self, creds=None) -> None:
        self.creds = creds
        self.acked = 0

    def read_new_credentials(self, *, consume: bool = True):
        return dict(self.creds) if self.creds else None

    def ack_inbox(self) -> None:
        self.acked += 1


class FakeStore:
    def __init__(self, accounts) -> None:
        self.accounts = accounts
        self.applied: list[str] = []

    def list_accounts(self):
        return [dict(a) for a in self.accounts]

    def apply_credentials(self, account_id, credentials):
        for a in self.accounts:
            if a["id"] == account_id:
                a["credentials"] = dict(credentials)
                a["expires_at"] = "2026-08-08T00:30:00"
                a["status"] = "active"
                self.applied.append(account_id)
                return dict(a)
        return None

    def mark_expired_if_needed(self) -> bool:
        now = datetime.now()
        changed = False
        for a in self.accounts:
            exp = a.get("expires_at")
            if a.get("status") == "active" and exp:
                try:
                    if datetime.fromisoformat(exp) <= now:
                        a["status"] = "expired"
                        changed = True
                except Exception:  # noqa: BLE001
                    continue
        return changed


def _awaiting(account_id: str, biz: str = "") -> dict:
    return {
        "id": account_id,
        "name": f"公众号-{account_id}",
        "biz": biz,
        "article_url": f"https://mp.weixin.qq.com/s?__biz={biz}" if biz else "",
        "status": "awaiting",
        "credentials": {},
        "expires_at": None,
    }


def _install_fakes(monkeypatch, *, creds=None, accounts=None):
    svc = FakeSvc(creds)
    store = FakeStore(accounts or [])
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(state, "get_mitm", lambda: svc)
    monkeypatch.setattr(state, "get_store", lambda: store)
    monkeypatch.setattr(
        watcher_mod, "broadcast_event", lambda t, p=None: events.append((t, p))
    )
    fake_capture_target = types.ModuleType("mp_harvest.core.capture_target")
    fake_capture_target.expected_biz = (
        lambda row: str(row.get("biz") or "").strip()
    )
    monkeypatch.setitem(sys.modules, "mp_harvest.core.capture_target", fake_capture_target)
    return svc, store, events


def test_poll_once_binds_and_broadcasts(monkeypatch):
    creds = {
        "__biz": "biz123",
        "uin": "u1",
        "key": "k1",
        "pass_ticket": "p1",
        "appmsg_token": "a1",
    }
    svc, store, events = _install_fakes(
        monkeypatch, creds=creds, accounts=[_awaiting("acc-1", biz="biz123")]
    )

    assert watcher_mod.poll_once() is True
    assert store.applied == ["acc-1"]
    assert store.accounts[0]["status"] == "active"
    assert svc.acked == 1
    assert len(events) == 1
    ev_type, payload = events[0]
    assert ev_type == "credential.captured"
    assert payload["account_id"] == "acc-1"
    assert isinstance(payload["expires_at"], int)
    assert payload["expires_at"] == int(
        datetime.fromisoformat("2026-08-08T00:30:00").timestamp()
    )


def test_pick_awaiting_prefers_biz_match(monkeypatch):
    creds = {"__biz": "biz-other", "uin": "u1", "key": "k1"}
    _, store, _ = _install_fakes(
        monkeypatch,
        creds=creds,
        accounts=[_awaiting("acc-1", biz="biz-a"), _awaiting("acc-2", biz="biz-other")],
    )
    target = watcher_mod.pick_awaiting_target(creds)
    assert target["id"] == "acc-2"


def test_poll_once_single_awaiting_fallback(monkeypatch):
    creds = {"__biz": "biz-x", "uin": "u1", "key": "k1"}
    _, store, events = _install_fakes(
        monkeypatch, creds=creds, accounts=[_awaiting("acc-1", biz="")]
    )
    assert watcher_mod.poll_once() is True
    assert store.applied == ["acc-1"]
    assert events


def test_poll_once_no_awaiting_keeps_inbox(monkeypatch):
    creds = {"__biz": "biz-x", "uin": "u1", "key": "k1"}
    svc, store, events = _install_fakes(
        monkeypatch, creds=creds, accounts=[_awaiting("acc-1", biz="other")]
    )
    # 有多个 awaiting 且 __biz 都不匹配 → 不绑定、不消费
    store.accounts.append(_awaiting("acc-2", biz="other2"))
    assert watcher_mod.poll_once() is False
    assert store.applied == []
    assert svc.acked == 0
    assert events == []


def test_watcher_thread_polls_until_stop(monkeypatch):
    calls = []
    monkeypatch.setattr(watcher_mod, "poll_once", lambda: calls.append(1) or False)
    w = watcher_mod.CredentialWatcher(interval=0.02)
    w.start()
    import time

    deadline = time.time() + 2
    while len(calls) < 3 and time.time() < deadline:
        time.sleep(0.01)
    w.stop()
    assert len(calls) >= 3
    assert not w._thread or not w._thread.is_alive()


def test_sweep_expired_broadcasts(monkeypatch):
    """过期的 active 账号被标记并广播 credential.expired（2026-08-09 补）。"""
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    future = (datetime.now() + timedelta(minutes=10)).isoformat(timespec="seconds")
    accounts = [
        {"id": "a1", "status": "active", "expires_at": past},
        {"id": "a2", "status": "active", "expires_at": future},
    ]
    _, store, events = _install_fakes(monkeypatch, creds=None, accounts=accounts)

    expired = watcher_mod.sweep_expired()

    assert expired == {"a1"}
    assert store.accounts[0]["status"] == "expired"
    assert store.accounts[1]["status"] == "active"
    assert ("credential.expired", {"account_id": "a1"}) in events

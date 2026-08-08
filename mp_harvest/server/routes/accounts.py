"""/api/accounts —— 公众号列表 / 添加 / 批量导入（两段式）/ 删除 / 取凭证。

对应旧模块：store、batch_import、credentials（设计稿 §7.1）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from mp_harvest.server import mappers, state
from mp_harvest.server.schemas import AccountCreateIn, ImportIn
from mp_harvest.server.ws import broadcast_event

router = APIRouter(tags=["accounts"])


@router.get("/api/accounts")
def list_accounts() -> list[dict]:
    """裸账号数组（前端 Account[]，API.md §3 第 5 条对齐）。"""
    return [mappers.account_out(r) for r in state.get_store().list_accounts()]


@router.post("/api/accounts", status_code=201)
def add_account(body: AccountCreateIn) -> dict:
    """添加待抓包账号；按设计稿 §3.3 顺带确保 mitm 在运行（best-effort）。

    响应为前端 Account 对象本身（裸对象）；mitm 提示附加为 ``mitm_message`` 字段。
    """
    row = state.get_store().add_pending(name=body.name, article_url=body.url)
    mitm_msg = ""
    try:
        svc = state.get_mitm()
        if not svc.running:
            ok, msg = svc.start()
            mitm_msg = msg
            broadcast_event(
                "mitm.status",
                {"running": bool(svc.running), "port": getattr(svc, "port", 8088)},
            )
            if not ok:
                mitm_msg = f"账号已添加，但抓包代理启动失败：{msg}"
    except Exception as exc:  # noqa: BLE001
        mitm_msg = f"账号已添加，但抓包代理不可用：{exc}"
    out = mappers.account_out(row)
    if mitm_msg:
        out["mitm_message"] = mitm_msg
    return out


@router.post("/api/accounts/import")
def import_accounts(body: ImportIn) -> dict:
    """两段式批量导入（§7.1，与前端约定形状）：

    - preview：``{text}`` → ``{items:[{name,url,dup}]}``（解析+批内/已有去重预览）
    - confirm：``{stage:'confirm', items}`` → ``{imported, skipped}``（dup/无链接跳过）
    """
    from mp_harvest.core import batch_import

    store = state.get_store()
    if body.stage == "preview":
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="导入文本为空")
        entries = batch_import.parse_batch_lines(text)
        entries = batch_import.dedupe_by_name(entries)
        existing = store.list_accounts()
        existing_urls = {str(a.get("article_url") or "") for a in existing}
        existing_names = {str(a.get("name") or "") for a in existing}
        fresh, dup_urls, dup_names = batch_import.split_fresh_duplicates(
            entries, existing_urls, existing_names
        )
        items = [
            {"name": str(e.get("name") or ""), "url": str(e.get("url") or ""), "dup": False}
            for e in fresh
        ]
        items += [
            {"name": str(e.get("name") or ""), "url": str(e.get("url") or ""), "dup": True}
            for e in (dup_urls + dup_names)
        ]
        return {"items": items}
    # confirm
    if not body.items:
        raise HTTPException(status_code=400, detail="确认导入缺少 items")
    imported = skipped = 0
    for it in body.items:
        url = (it.url or "").strip()
        if it.dup or not url:
            skipped += 1
            continue
        store.add_pending(name=it.name or "未命名公众号", article_url=url)
        imported += 1
    return {"imported": imported, "skipped": skipped}


@router.post("/api/accounts/{account_id}/renew")
def renew_account(account_id: str) -> dict:
    """续约：重置为等待抓包状态（§5.4 语义，参考旧 ui.renew_account）。

    校验 __biz/文章链接 → 确保 mitm 运行 → 清掉已合并凭证 → set_awaiting；
    之后用户在微信内刷新文章，mitm 捕获后经 WS 推 credential.captured。
    """
    from mp_harvest.core import capture_target

    store = state.get_store()
    account = store.get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not capture_target.expected_biz(account) and not str(
        account.get("article_url") or ""
    ).strip():
        raise HTTPException(
            status_code=400, detail="该公众号缺少 __biz / 文章链接，无法续约"
        )
    try:
        svc = state.get_mitm()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"抓包组件不可用：{exc}") from exc
    if not svc.running:
        ok, msg = svc.start()
        broadcast_event(
            "mitm.status",
            {"running": bool(svc.running), "port": getattr(svc, "port", 8088)},
        )
        if not ok:
            raise HTTPException(status_code=500, detail=f"抓包代理启动失败：{msg}")
    svc.reset_capture_state()  # 清 inbox + 内存合并，强制等待新流量
    store.set_awaiting(account_id)
    return {"ok": True, "account_id": account_id, "status": "awaiting"}


@router.delete("/api/accounts/{account_id}")
def delete_account(account_id: str) -> dict:
    store = state.get_store()
    if store.get(account_id) is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    store.delete(account_id)
    state.drop_articles(account_id)
    return {"ok": True, "id": account_id}


@router.get("/api/accounts/{account_id}/credential")
def get_credential(account_id: str) -> dict:
    from mp_harvest.core import credentials as cred_mod

    account = state.get_store().get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    cred = account.get("credentials") or {}
    if not cred:
        raise HTTPException(status_code=409, detail="该账号尚无有效凭证")
    return {
        "account_id": account_id,
        "name": account.get("name"),
        "expires_at": account.get("expires_at"),
        "credentials": cred,
        "json": cred_mod.credentials_to_json(cred),
    }

"""/api/accounts —— 公众号列表 / 添加 / 批量导入（两段式）/ 删除 / 取凭证 / 合并重复。

对应旧模块：store、batch_import、credentials（设计稿 §7.1）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from mp_harvest.server import mappers, state
from mp_harvest.server.schemas import AccountCreateIn, ImportIn, MergeAccountsIn
from mp_harvest.server.ws import broadcast_event

router = APIRouter(tags=["accounts"])


def _biz_of(acct: dict) -> str:
    """账号的 __biz（行上或凭证里）；两者都没有返回空串。

    空串**不等于**「同一个公众号」—— 两个都取不到 biz 的账号不能被当成重复，
    否则合并会把两个毫不相干的号并到一起（文章混一起，不可逆）。
    """
    return str(acct.get("biz") or (acct.get("credentials") or {}).get("__biz") or "").strip()


def _article_key(row: dict) -> str:
    """与 state._article_key 同一口径：identity 优先，退回 link。"""
    return str(row.get("identity") or row.get("link") or "")


def _ensure_mitm_started() -> tuple[bool, str]:
    """Best-effort 确保抓包代理在运行（设计稿 §3.3，添加/导入账号共用）。

    返回原始 ``(ok, msg)``：``ok=False`` 表示代理不可用（如 CA 未信任时
    ``start()`` 拒绝设置系统代理，2026-09 修复假 ok）；``msg`` 仅在本次
    真正尝试启动时非空。调用方负责按场景包装提示文案。
    """
    svc = state.get_mitm()
    if svc.running:
        return True, ""
    ok, msg = svc.start()
    broadcast_event(
        "mitm.status",
        {"running": bool(svc.running), "port": getattr(svc, "port", 8088)},
    )
    return ok, msg


@router.get("/api/accounts")
def list_accounts() -> list[dict]:
    """裸账号数组（前端 Account[]，API.md §3 第 5 条对齐）。"""
    return [mappers.account_out(r) for r in state.get_store().list_accounts()]


@router.post("/api/accounts", status_code=201)
def add_account(body: AccountCreateIn) -> dict:
    """添加待抓包账号；按设计稿 §3.3 顺带确保 mitm 在运行（best-effort）。

    响应为前端 Account 对象本身（裸对象）；mitm 提示附加为 ``mitm_message`` 字段。
    """
    store = state.get_store()
    url = (body.url or "").strip()
    for a in store.list_accounts():
        if str(a.get("article_url") or "").strip() == url:
            raise HTTPException(
                status_code=409,
                detail=f"该文章链接的账号已存在：{a.get('name') or url}",
            )
    row = store.add_pending(name=body.name, article_url=body.url)
    mitm_msg = ""
    try:
        ok, msg = _ensure_mitm_started()
        # 只在**失败**时回传提示（2026-09 修复）：start() 成功时 msg 是
        # 「已开启系统代理…请用微信桌面打开公众号文章」这类成功文案，原先
        # 也塞进 mitm_message，而前端把任何 mitm_message 当失败 → 首次
        # 「添加并抓包」明明成功却弹红色错误让用户去装 CA。
        if not ok and msg:
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
    out: dict = {"imported": imported, "skipped": skipped}
    # 与单个 add_account 一致：导入完成后 best-effort 拉起抓包代理（§3.3）。
    # 成功时保持 {imported, skipped} 形状契约，仅失败时附加 mitm_message。
    try:
        ok, msg = _ensure_mitm_started()
        if not ok and msg:
            out["mitm_message"] = f"账号已导入，但抓包代理启动失败：{msg}"
    except Exception as exc:  # noqa: BLE001
        out["mitm_message"] = f"账号已导入，但抓包代理不可用：{exc}"
    return out


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


@router.get("/api/accounts/duplicates")
def list_duplicate_accounts() -> dict:
    """同一个公众号（``__biz``）被添加了多次的分组（2026-09）。

    批量导入按「名称」和「链接」去重，**不按公众号**；而短链（``/s/xxx``）
    里根本没有 ``__biz``，导入时无从判断。于是同一个号写成两个名字、各给一条
    链接时就会建出两行。后果不只是列表难看：文章在每个账号下各存一份 ——
    「全部公众号」里同一篇出现两次、筛选跑两遍、正文拉两遍、执行日志打两遍。

    ``article_count`` 给前端做默认选择（保留文章多的那行），**不替用户决定**：
    名字是用户可见的，哪个名字才对只有他知道。
    """
    groups: dict[str, list[dict]] = {}
    for acct in state.get_store().list_accounts():
        biz = _biz_of(acct)
        if not biz:
            continue
        groups.setdefault(biz, []).append(acct)

    out: list[dict] = []
    for biz, rows in groups.items():
        if len(rows) < 2:
            continue
        items = [
            {
                "id": str(a.get("id") or ""),
                "name": str(a.get("name") or ""),
                "article_count": len(state.get_articles(str(a.get("id") or ""))),
                "created_at": str(a.get("created_at") or ""),
            }
            for a in rows
        ]
        # 文章多的排前面（前端默认选第一个，用户改主意只需点另一个）
        items.sort(key=lambda x: (-x["article_count"], x["created_at"], x["id"]))
        out.append({"biz": biz, "accounts": items})
    out.sort(key=lambda g: g["biz"])
    return {"groups": out}


@router.post("/api/accounts/merge-duplicates")
def merge_duplicate_accounts(body: MergeAccountsIn) -> dict:
    """把重复的账号行合并成一行：文章按 identity 取并集，然后删掉多余的行。

    - 保留哪一行由调用方指定（``keep_id``）—— 名字是用户可见的，不替他选；
    - **只在同一个 ``__biz`` 内允许合并**：``__biz`` 不同就不是同一个公众号，
      合并等于把两个号的文章混进一个账号，且不可逆；
    - 文章取并集、**已存在的以保留行为准**（判定结果/正文都在保留行上，
      不能被覆盖掉）；只在保留行缺这一篇时才从被合并行搬过来；
    - 只动本地：微信那边没有「账号」，凭证是抓包来的，删掉的行不影响其它行。
    """
    store = state.get_store()
    keep = store.get(body.keep_id)
    if keep is None:
        raise HTTPException(status_code=404, detail="要保留的账号不存在")
    drop_ids = [str(d) for d in body.drop_ids if str(d).strip() and str(d) != body.keep_id]
    if not drop_ids:
        raise HTTPException(status_code=400, detail="至少要指定一个要合并掉的账号")

    keep_biz = _biz_of(keep)
    drops: list[str] = []
    for did in drop_ids:
        row = store.get(did)
        if row is None:
            raise HTTPException(status_code=404, detail=f"要合并掉的账号不存在：{did}")
        if not keep_biz or _biz_of(row) != keep_biz:
            raise HTTPException(
                status_code=400,
                detail="只能合并同一个公众号的账号（__biz 不同，合并会把两个号的文章混在一起）",
            )
        drops.append(did)

    rows = list(state.get_articles(body.keep_id))
    seen = {_article_key(r) for r in rows if _article_key(r)}
    added = 0
    for did in drops:
        for r in state.get_articles(did):
            key = _article_key(r)
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(dict(r))
            added += 1
    # 合并后按发布时间排序，和 merge_articles 的收尾一致（列表顺序不会看起来乱了）
    rows.sort(key=lambda r: int(r.get("publish_ts") or 0), reverse=True)
    state.set_articles(body.keep_id, rows)

    for did in drops:
        state.drop_articles(did)
        store.delete(did)
    broadcast_event("accounts.changed", {"account_id": body.keep_id, "merged": drops})
    return {
        "ok": True,
        "keep_id": body.keep_id,
        "merged": drops,
        "added_articles": added,
        "total": len(rows),
    }

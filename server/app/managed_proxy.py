"""Encrypted subscription inventory and transactional managed-core orchestration."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from threading import Event, RLock, Thread
import uuid
from urllib.parse import urlsplit

from .database import SessionLocal
from .integrations.credentials import decrypt_credentials, encrypt_credentials
from .models import NetworkSetting
from .proxy_subscription import SubscriptionError, fetch_subscription

KEY = "managed_proxy"
MAX_SUBSCRIPTIONS = 10
MAX_TOTAL_NODES = 2000
_lock = RLock()
_stop = Event()
_thread = None
_startup_error = None
log = logging.getLogger(__name__)


def core_manager():
    from .managed_core import core
    return core


def now():
    return datetime.now(timezone.utc)


def timestamp(value=None):
    return (value or now()).isoformat().replace("+00:00", "Z")


def read_state(db=None):
    if db is None:
        with SessionLocal() as session:
            return read_state(session)
    row = db.get(NetworkSetting, KEY)
    return decrypt_credentials(row.value_encrypted) if row else {"enabled": False, "subscriptions": [], "selected_node_id": None}


def all_nodes(state):
    return [node for subscription in state["subscriptions"] for node in subscription["nodes"]]


def core_nodes(state):
    return [{**node["config"], "name": node["id"]} for node in all_nodes(state)]


def save_state(db, state):
    encoded = json.dumps(state, ensure_ascii=False)
    if len(encoded.encode()) > 8 * 1024 * 1024 or len(all_nodes(state)) > MAX_TOTAL_NODES:
        raise SubscriptionError("订阅合计超过 2000 个节点或 8 MiB 保存限制")
    row = db.get(NetworkSetting, KEY)
    if row is None:
        row = NetworkSetting(key=KEY)
        db.add(row)
    row.value_encrypted = encrypt_credentials(state)
    db.commit()


def apply_and_save(db, previous, updated):
    # Check size before configuring the running core, and roll back if DB commit
    # fails. Failed subscription updates never replace the last known nodes.
    if len(all_nodes(updated)) > MAX_TOTAL_NODES or len(json.dumps(updated).encode()) > 8 * 1024 * 1024:
        raise SubscriptionError("订阅合计超过 2000 个节点或 8 MiB 保存限制")
    manager = core_manager()
    manager.apply_nodes(core_nodes(updated), updated["selected_node_id"])
    try:
        save_state(db, updated)
    except Exception:
        db.rollback()
        try:
            manager.apply_nodes(core_nodes(previous), previous["selected_node_id"])
        except Exception:
            # Never silently switch traffic to a direct route during recovery.
            manager.stop()
        raise


def outbound_proxy():
    state = read_state()
    # Even if the child process is down this returns its endpoint. The request
    # fails closed instead of falling back to direct/another external proxy.
    return core_manager().proxy_url if state["enabled"] else None


def public_state(db):
    state = read_state(db)
    status = core_manager().status()
    if _startup_error and not status.get("running"):
        status = {**status, "error": _startup_error}
    subscriptions, nodes = [], []
    for subscription in state["subscriptions"]:
        subscriptions.append({key: subscription.get(key) for key in (
            "id", "label", "auto_update", "interval_hours", "fetch_via_proxy", "last_updated_at", "last_error", "next_update_at")}
            | {"url_host": urlsplit(subscription["url"]).hostname, "node_count": len(subscription["nodes"])})
        for node in subscription["nodes"]:
            nodes.append({"id": node["id"], "name": node["name"], "type": node["config"]["type"],
                "subscription_id": subscription["id"], "subscription_label": subscription["label"],
                "selected": node["id"] == state["selected_node_id"], "delay": node.get("delay"),
                "delay_checked_at": node.get("delay_checked_at")})
    return {"enabled": state["enabled"], "core": status, "subscriptions": subscriptions, "nodes": nodes,
        "selected_node_id": state["selected_node_id"],
        "message": "内置代理优先于高级外部代理；失败不会回退直连。仅代理书库服务端的新连接，不修改电脑或 NAS 的系统代理。"}


def mapped_nodes(subscription_id, definitions, old_nodes=()):
    previous = {node["id"]: node for node in old_nodes}
    result = []
    for config in definitions:
        ident = "q-" + hashlib.sha256((subscription_id + "\0" + config["name"]).encode()).hexdigest()[:32]
        old = previous.get(ident)
        node = {"id": ident, "name": config["name"], "config": config,
                "delay": old.get("delay") if old and old["config"] == config else None}
        if old and old["config"] == config and old.get("delay_checked_at"):
            node["delay_checked_at"] = old["delay_checked_at"]
        result.append(node)
    return result


def create_subscription(db, values):
    with _lock:
        previous = read_state(db)
        if len(previous["subscriptions"]) >= MAX_SUBSCRIPTIONS:
            raise SubscriptionError("最多支持 10 个订阅")
        if any(s["url"] == values["url"] for s in previous["subscriptions"]):
            raise SubscriptionError("该订阅链接已存在，请更新已有订阅")
        definitions = fetch_subscription(values["url"], values["fetch_via_proxy"])
        subscription_id = uuid.uuid4().hex
        subscription = {**values, "id": subscription_id, "nodes": mapped_nodes(subscription_id, definitions),
            "last_updated_at": timestamp(), "last_error": None,
            "next_update_at": timestamp(now() + timedelta(hours=values["interval_hours"])) if values["auto_update"] else None}
        updated = deepcopy(previous)
        updated["subscriptions"].append(subscription)
        if not updated["selected_node_id"] and not previous["enabled"]:
            updated["selected_node_id"] = subscription["nodes"][0]["id"]
        apply_and_save(db, previous, updated)
        return public_state(db)


def find_subscription(state, subscription_id):
    result = next((s for s in state["subscriptions"] if s["id"] == subscription_id), None)
    if result is None:
        raise SubscriptionError("订阅不存在")
    return result


def update_subscription(db, subscription_id, changes=None, *, refresh=False):
    with _lock:
        previous = read_state(db)
        updated = deepcopy(previous)
        subscription = find_subscription(updated, subscription_id)
        changes = changes or {}
        subscription.update(changes)
        if any(s["id"] != subscription_id and s["url"] == subscription["url"] for s in updated["subscriptions"]):
            raise SubscriptionError("该订阅链接已存在")
        if refresh or "url" in changes:
            try:
                definitions = fetch_subscription(subscription["url"], subscription["fetch_via_proxy"])
                subscription["nodes"] = mapped_nodes(subscription_id, definitions, subscription["nodes"])
                subscription.update(last_updated_at=timestamp(), last_error=None)
                if updated["selected_node_id"] not in {n["id"] for n in all_nodes(updated)}:
                    updated["selected_node_id"] = None
                subscription["next_update_at"] = timestamp(now() + timedelta(hours=subscription["interval_hours"])) if subscription["auto_update"] else None
                apply_and_save(db, previous, updated)
            except (SubscriptionError, ValueError, RuntimeError):
                # Retain the old URL and nodes on failed replacement. No remote
                # body, URL query, password or detailed core log is persisted.
                db.rollback()
                kept = deepcopy(previous)
                old = find_subscription(kept, subscription_id)
                old["last_error"] = "订阅更新失败，已保留上次节点；请检查订阅、下载方式及内核状态"
                old["next_update_at"] = timestamp(now() + timedelta(hours=old["interval_hours"])) if old["auto_update"] else None
                save_state(db, kept)
                raise
        else:
            subscription["next_update_at"] = timestamp(now() + timedelta(hours=subscription["interval_hours"])) if subscription["auto_update"] else None
            save_state(db, updated)
        return public_state(db)


def delete_subscription(db, subscription_id):
    with _lock:
        previous = read_state(db)
        find_subscription(previous, subscription_id)
        updated = deepcopy(previous)
        updated["subscriptions"] = [s for s in updated["subscriptions"] if s["id"] != subscription_id]
        if updated["selected_node_id"] not in {n["id"] for n in all_nodes(updated)}:
            updated["selected_node_id"] = None
        apply_and_save(db, previous, updated)
        return public_state(db)


def enable_proxy(db, enabled):
    with _lock:
        previous = read_state(db)
        updated = deepcopy(previous)
        if enabled and not updated["selected_node_id"]:
            raise SubscriptionError("请先导入订阅并选择一个节点")
        updated["enabled"] = enabled
        if enabled:
            apply_and_save(db, previous, updated)
        else:
            save_state(db, updated)
        return public_state(db)


def select_node(db, node_id):
    with _lock:
        previous = read_state(db)
        if node_id not in {n["id"] for n in all_nodes(previous)}:
            raise SubscriptionError("节点不存在，请刷新订阅列表")
        updated = deepcopy(previous)
        updated["selected_node_id"] = node_id
        apply_and_save(db, previous, updated)
        return public_state(db)


def measure_delay(db, node_id):
    with _lock:
        state = read_state(db)
        node = next((n for n in all_nodes(state) if n["id"] == node_id), None)
        if node is None:
            raise SubscriptionError("节点不存在")
        signature = node_signature(node)
    result = probe_delay(node_id)
    return persist_delay_results([result], {node_id: signature})[0]


def node_signature(node):
    # Internal comparison only. Neither node configuration nor its digest is
    # returned by the batch API, which exposes opaque IDs and latency only.
    return hashlib.sha256(json.dumps(node["config"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def delay_targets(db, node_ids=None):
    with _lock:
        nodes = all_nodes(read_state(db))
        if node_ids is not None:
            requested = set(node_ids)
            if not requested or not requested.issubset({n["id"] for n in nodes}):
                raise SubscriptionError("测速节点不存在，请刷新节点列表")
            nodes = [node for node in nodes if node["id"] in requested]
        if not nodes:
            raise SubscriptionError("请先导入包含节点的订阅")
        if len(nodes) > MAX_TOTAL_NODES:
            raise SubscriptionError("批量测速最多支持 2000 个节点")
        if not core_manager().status().get("running"):
            raise SubscriptionError("内置代理内核尚未运行，请先重启内核再测速")
        return {node["id"]: node_signature(node) for node in nodes}


def probe_delay(node_id, cancelled=None):
    from .managed_core import ManagedCoreBusy
    # Fixed precision sorts across processes/restarts and lets a newer single
    # probe supersede an older result waiting for the rest of its batch.
    checked_at = now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    try:
        if cancelled is not None:
            delay = core_manager().delay(node_id, wait_for_slot=True, cancelled=cancelled)
        else:
            delay = core_manager().delay(node_id)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 60000:
            raise ValueError()
    except ManagedCoreBusy:
        return {"node_id": node_id, "status": "skipped", "delay": None, "ok": False,
                "checked_at": checked_at, "message": "测速队列繁忙或任务已取消，本节点未测试；已保留上次延迟"}
    except Exception:
        # Native/controller errors may include credentials. A failed probe is a
        # per-node result and must not kill the remaining background work.
        return {"node_id": node_id, "status": "failed", "delay": None, "ok": False,
                "checked_at": checked_at, "message": "节点测速失败或超时，请检查节点状态与网络"}
    return {"node_id": node_id, "status": "success", "delay": int(delay), "ok": True,
            "checked_at": checked_at, "message": "节点测速完成；不代表平台账号权限有效"}


def persist_delay_results(results, signatures, cancelled=None):
    # Merge into the CURRENT inventory once per batch, not a stale snapshot.
    # This avoids rewriting a large encrypted subscription for every probe and
    # preserves concurrent selection, enablement and subscription edits.
    with _lock, SessionLocal() as db:
        if cancelled is not None and cancelled.is_set():
            return results
        state = read_state(db)
        nodes = {node["id"]: node for node in all_nodes(state)}
        merged = deepcopy(results)
        changed = False
        for result in merged:
            if result["status"] == "skipped":
                continue
            node = nodes.get(result["node_id"])
            if node is None or node_signature(node) != signatures[result["node_id"]]:
                result.update(status="skipped", delay=None, ok=False, message="节点已更新或删除，本次测速结果已忽略")
            elif node.get("delay_checked_at", "") > result["checked_at"]:
                result.update(status="skipped", delay=None, ok=False, message="已有较新的测速结果，本次旧结果已忽略")
            else:
                node["delay"] = result["delay"]
                node["delay_checked_at"] = result["checked_at"]
                changed = True
        if changed and not (cancelled is not None and cancelled.is_set()):
            save_state(db, state)
        return merged


def restart_core(db):
    with _lock:
        state = read_state(db)
        core_manager().stop()
        core_manager().apply_nodes(core_nodes(state), state["selected_node_id"])
        return public_state(db)


def refresh_due():
    with _lock, SessionLocal() as db:
        for subscription in read_state(db)["subscriptions"]:
            if _stop.is_set():
                return
            due = subscription.get("next_update_at")
            if subscription["auto_update"] and due and datetime.fromisoformat(due.replace("Z", "+00:00")) <= now():
                try:
                    update_subscription(db, subscription["id"], refresh=True)
                except Exception:
                    log.warning("Managed proxy subscription refresh failed; previous nodes retained")


def _updater():
    while not _stop.wait(60):
        try:
            refresh_due()
        except Exception:
            log.warning("Managed proxy update scheduler temporarily unavailable")


def start_service():
    global _thread, _startup_error
    from .managed_proxy_delay import batches
    batches.resume()
    if os.getenv("LIBRARY_MANAGED_PROXY_RUNTIME", "true").lower() in {"false", "0", "no"}:
        return
    _stop.clear()
    try:
        state = read_state()
        core_manager().apply_nodes(core_nodes(state), state["selected_node_id"])
        _startup_error = None
    except Exception:
        _startup_error = "内置代理尚未就绪，请检查内核安装与运行目录；已启用代理时请求不会回退直连"
        log.warning("Managed proxy core unavailable; library remains accessible")
    if not _thread or not _thread.is_alive():
        _thread = Thread(target=_updater, name="managed-proxy-updater", daemon=True)
        _thread.start()


def stop_service():
    from .managed_proxy_delay import batches
    batches.stop()
    if os.getenv("LIBRARY_MANAGED_PROXY_RUNTIME", "true").lower() in {"false", "0", "no"}:
        return
    _stop.set()
    # Serialize with an in-flight update: stopping the child before the update
    # commits could let apply_nodes launch an orphan after application shutdown.
    with _lock:
        core_manager().stop()
    if _thread:
        _thread.join(timeout=2)

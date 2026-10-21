"""Read-only queue explanations. Never claim jobs, decrypt account tokens or call platforms."""
from sqlalchemy import select

from . import queue_control
from .models import Job, utcnow


def explain_lanes(db, control, lanes, runtime):
    now = utcnow()
    workers = {row["lane"]: row for row in runtime.get("workers", [])}
    resources = None
    resources_read = False
    for lane in lanes:
        ident, counts = lane["id"], lane["counts"]
        reasons = queue_control.pause_reasons(control, ident)
        if reasons:
            state, code = "paused", reasons[0]["code"]
        elif runtime.get("mode") == "disabled":
            state, code = "waiting", "worker_disabled"
            reasons.append({"code": code, "message": "后台工作进程未启用，队列开关本身未暂停", "action": "检查服务器 LIBRARY_WORKER_ENABLED 配置"})
        elif runtime.get("mode") == "process" and (ident not in workers or workers[ident]["state"] != "running"):
            worker = workers.get(ident, {})
            state, code = "waiting", "worker_" + worker.get("state", "missing")
            reason = {"starting": "工作进程正在启动", "restarting": "工作进程退出后正在重启", "unresponsive": "工作进程心跳超时", "missing": "工作进程尚未就绪"}.get(worker.get("state", "missing"), "工作进程暂不可用")
            reasons.append({"code": code, "message": reason + "，队列开关本身未暂停", "action": "等待自动恢复；持续异常时检查服务器日志"})
        elif ident in queue_control.NETWORK_LANES and control.backoff_until and control.backoff_until > now and (counts["queued"] or counts["running"]):
            state, code = "waiting", "platform_backoff"
            reasons.append({"code": code, "message": "平台触发请求冷却，队列未暂停；关闭随机限速也会遵守此等待", "action": "冷却结束后自动继续", "resume_at": control.backoff_until.isoformat() + "Z"})
        elif counts["running"] >= lane["concurrency"] and counts["queued"]:
            state, code = "running", "lane_concurrency"
            reasons.append({"code": code, "message": "本队列并发已满，其他任务排队等待", "action": "现有任务结束后自动领取；也可调整本队列并发"})
        elif counts["queued"]:
            state, code = "ready", "queued"
            reasons.append({"code": code, "message": "队列未暂停，等待工作进程领取任务", "action": "自动领取；账号并发与功能权限仍生效"})
            if ident in queue_control.NETWORK_LANES:
                from .pixiv_accounts import assignment_state, assignment_filter
                if not resources_read:
                    resources = assignment_state(db)
                    resources_read = True
                if resources is not None:
                    eligible = db.scalar(select(Job.id).where(Job.queue_lane == ident, Job.status == "queued", assignment_filter(resources)).limit(1))
                    if not eligible:
                        state, code = "waiting", "account_capacity"
                        reasons = [{"code": code, "message": "待执行任务所需账号的并发额度已占满，队列未暂停", "action": "账号其他任务完成后自动继续；可在账号管理查看各账号并发"}]
        elif counts["running"]:
            state, code = "running", "running"
            reasons.append({"code": code, "message": "正在处理任务", "action": "无需操作"})
        else:
            state, code = "idle", "empty"
            reasons.append({"code": code, "message": "当前没有待执行任务，队列未暂停", "action": "新任务入队后自动执行；失败任务需重试后才会重新入队"})
        lane["diagnostic"] = {"state": state, "code": code, "reasons": reasons}

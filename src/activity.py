"""把 OpenCode 会话列表整理成手机上能看懂的「其他会话在做什么」。

compose 是纯函数，只吃数据不碰网络，方便离线测试。
"""

import os
import time

OUTCOME_ZH = {
    "succeeded": "已完成",
    "failed": "失败",
    "interrupted": "已中断",
}


def ago_text(updated_ms, now=None):
    if not updated_ms:
        return "时间未知"
    now = now if now is not None else time.time()
    seconds = max(0, now - updated_ms / 1000)
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def status_of(session, active_ids):
    if session.get("id") in active_ids:
        return "运行中"
    return OUTCOME_ZH.get(session.get("outcome"), "空闲")


def dir_name(session):
    directory = (session.get("location") or {}).get("directory") or ""
    return os.path.basename(directory.rstrip("/")) or directory or "未知目录"


def compose(sessions, active_ids=None, exclude_id=None, limit=8, now=None):
    active_ids = active_ids or set()
    rows = [s for s in sessions if s.get("id") and s.get("id") != exclude_id]
    rows.sort(
        key=lambda s: (
            s.get("id") not in active_ids,
            -((s.get("time") or {}).get("updated") or 0),
        )
    )
    rows = rows[:limit]
    if not rows:
        return "没有其他 OpenCode 会话。"
    lines = ["**OpenCode 其他会话**", ""]
    for index, session in enumerate(rows, start=1):
        title = (session.get("title") or "无标题").strip().replace("\n", " ")
        if len(title) > 40:
            title = title[:40] + "…"
        updated = (session.get("time") or {}).get("updated")
        lines.append(f"{index}. {status_of(session, active_ids)} · {title}")
        lines.append(f"   目录 {dir_name(session)} · {ago_text(updated, now)}")
    return "\n".join(lines)

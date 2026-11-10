"""从本机 OpenCode 数据库抽取某一天的工作记录，输出 Markdown 摘要。

只读数据库，不联网，不产生任何费用。
关键参数写成本文件顶部的全局变量，也可用同名环境变量覆盖。
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta

DB_PATH = os.environ.get(
    "OPENCODE_DB", os.path.expanduser("~/.local/share/opencode/opencode.db")
)
REPORT_DATE = os.environ.get("OPENCODE_REPORT_DATE", "")  # 留空表示今天
OUT_PATH = os.environ.get("OPENCODE_REPORT_OUT", "")
TITLE_LIMIT = 60
PROMPT_LIMIT = 70
MAX_TOOLS_PER_SESSION = 12
# 生成记录这个动作本身也会留下会话，按标题前缀跳过，免得记录里写记录。
SKIP_TITLE_PREFIXES = ("每日工作记录",)

TOOL_ZH = {
    "shell": "终端",
    "read": "读文件",
    "write": "写文件",
    "edit": "改文件",
    "glob": "找文件",
    "grep": "搜代码",
    "execute": "沙箱脚本",
    "webfetch": "抓网页",
    "websearch": "搜网络",
    "question": "问用户",
    "skill": "加载技能",
    "subagent": "派子任务",
    "task": "派子任务",
    "todowrite": "写待办",
    "todoread": "读待办",
}

OUTCOME_ZH = {
    "succeeded": "已完成",
    "failed": "失败",
    "interrupted": "已中断",
}


def day_bounds(date_str):
    if date_str:
        start = datetime.strptime(date_str, "%Y-%m-%d")
    else:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((start + timedelta(days=1)).timestamp() * 1000)
    return start_ms, end_ms


def load_rows(conn, start_ms, end_ms):
    return conn.execute(
        """
        SELECT m.session_id, m.type, m.data, m.time_created,
               s.title, s.directory, s.parent_id, s.time_created AS session_created
        FROM session_message m
        JOIN session_v2 s ON s.id = m.session_id
        WHERE m.time_created >= ? AND m.time_created < ?
        ORDER BY m.session_id, m.seq
        """,
        (start_ms, end_ms),
    ).fetchall()


def scan(rows):
    sessions = {}
    for row in rows:
        sid = row["session_id"]
        if (row["title"] or "").startswith(SKIP_TITLE_PREFIXES):
            continue
        info = sessions.setdefault(
            sid,
            {
                "id": sid,
                "title": row["title"] or "无标题",
                "directory": row["directory"] or "",
                "parent_id": row["parent_id"],
                "prompts": [],
                "tools": [],
                "models": {},
                "user_count": 0,
                "assistant_count": 0,
                "outcome": "",
                "cost": 0.0,
                "tok_in": 0,
                "tok_out": 0,
                "tok_reason": 0,
                "first_ms": row["time_created"],
                "last_ms": row["time_created"],
            },
        )
        info["last_ms"] = max(info["last_ms"], row["time_created"])
        info["first_ms"] = min(info["first_ms"], row["time_created"])
        try:
            payload = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        mtype = row["type"]
        if mtype == "user":
            info["user_count"] += 1
            text = (payload.get("text") or "").strip().replace("\n", " ")
            if text and not text.startswith("The previous response was interrupted"):
                info["prompts"].append(text)
        elif mtype == "assistant":
            info["assistant_count"] += 1
            info["cost"] += payload.get("cost") or 0.0
            tokens = payload.get("tokens") or {}
            info["tok_in"] += tokens.get("input") or 0
            info["tok_out"] += tokens.get("output") or 0
            info["tok_reason"] += tokens.get("reasoning") or 0
            model = payload.get("model") or {}
            name = f"{model.get('providerID', '')}/{model.get('id', '')}".strip("/")
            if name:
                info["models"][name] = info["models"].get(name, 0) + 1
            for item in payload.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool":
                    info["tools"].append(item.get("name") or "unknown")
        elif mtype == "idle":
            info["outcome"] = payload.get("outcome") or info["outcome"]
    return sessions


def clip(text, limit):
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def clock(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def tool_summary(tools):
    counts = {}
    for name in tools:
        counts[name] = counts.get(name, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[
        :MAX_TOOLS_PER_SESSION
    ]
    return "、".join(f"{TOOL_ZH.get(n, n)} {c}" for n, c in ordered)


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def demand_line(info):
    title = info["title"].strip()
    prompts = info["prompts"]
    if not prompts:
        return ""
    first = prompts[0]
    if first[:18] and first[:18] in title:
        if len(prompts) > 1 and prompts[-1][:18] not in title:
            return f"最后指令：{clip(prompts[-1], PROMPT_LIMIT)}"
        return ""
    return f"用户诉求：{clip(first, PROMPT_LIMIT)}"


def model_line(info):
    if not info["models"]:
        return ""
    ordered = sorted(info["models"].items(), key=lambda kv: -kv[1])
    names = "、".join(name.split("/")[-1] for name, _ in ordered[:3])
    return names


def build_report(date_str):
    start_ms, end_ms = day_bounds(date_str)
    label = date_str or datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sessions = scan(load_rows(conn, start_ms, end_ms))
    conn.close()

    total_cost = sum(s["cost"] for s in sessions.values())
    total_in = sum(s["tok_in"] for s in sessions.values())
    total_out = sum(s["tok_out"] for s in sessions.values())
    total_reason = sum(s["tok_reason"] for s in sessions.values())
    total_tools = sum(len(s["tools"]) for s in sessions.values())
    tops = [s for s in sessions.values() if not s["parent_id"]]
    subs = [s for s in sessions.values() if s["parent_id"]]
    all_models = {}
    for info in sessions.values():
        for name, count in info["models"].items():
            all_models[name] = all_models.get(name, 0) + count

    lines = [f"# OpenCode 工作记录 {label}", ""]
    lines.append("## 今日概览")
    lines.append("")
    lines.append("（待补）")
    lines.append("")
    lines.append("## 数据")
    lines.append("")
    lines.append(
        f"{len(tops)} 个会话，{len(subs)} 个子任务，工具调用 {total_tools} 次。"
    )
    lines.append("")
    lines.append(
        f"输入 {fmt_tokens(total_in)}，输出 {fmt_tokens(total_out)}，"
        f"推理 {fmt_tokens(total_reason)}，花费 ${total_cost:.2f}。"
    )
    lines.append("")
    if all_models:
        ordered = sorted(all_models.items(), key=lambda kv: -kv[1])
        names = "、".join(f"{name.split('/')[-1]}({n})" for name, n in ordered[:4])
        lines.append(f"模型：{names}")
        lines.append("")

    groups = {}
    for info in sorted(tops, key=lambda s: s["first_ms"]):
        directory = os.path.basename(info["directory"].rstrip("/")) or info["directory"]
        groups.setdefault(directory, []).append(info)

    for directory, items in groups.items():
        lines.append(f"## {directory}")
        lines.append("")
        for index, info in enumerate(items, start=1):
            state = OUTCOME_ZH.get(info["outcome"], "空闲")
            lines.append(
                f"{index}. {clip(info['title'], TITLE_LIMIT)} — {state} · "
                f"{clock(info['first_ms'])} 到 {clock(info['last_ms'])} · "
                f"${info['cost']:.2f}"
            )
            demand = demand_line(info)
            if demand:
                lines.append(f"   {demand}")
            if info["tools"]:
                lines.append(f"   动作：{tool_summary(info['tools'])}")
            kids = [k for k in subs if k["parent_id"] == info["id"]]
            if kids:
                names = "、".join(clip(k["title"], 20) for k in kids)
                lines.append(f"   派生 {len(kids)} 个子任务：{names}")
        lines.append("")

    if not sessions:
        lines.append("今天没有 OpenCode 会话记录。")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main():
    report = build_report(REPORT_DATE)
    if OUT_PATH:
        with open(OUT_PATH, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"已写入 {OUT_PATH}，{len(report)} 字符")
    else:
        print(report)


if __name__ == "__main__":
    main()

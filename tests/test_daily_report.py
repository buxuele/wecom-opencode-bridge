import importlib.util
import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "opencode_daily_report", ROOT / "scripts" / "opencode_daily_report.py"
)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

FAILURES = []


def check(name, actual, predicate, detail=""):
    ok = predicate(actual)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def make_db():
    base = int(datetime(2026, 10, 11, 9, 0, 0).timestamp() * 1000)
    path = Path(tempfile.mkdtemp()) / "fake.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE project (id text PRIMARY KEY, worktree text);
        CREATE TABLE session_v2 (
            id text PRIMARY KEY, title text, directory text, parent_id text,
            project_id text, time_created integer, time_updated integer
        );
        CREATE TABLE session_message (
            id text PRIMARY KEY, session_id text, type text, seq integer,
            time_created integer, time_updated integer, data text
        );
        """
    )
    conn.execute("INSERT INTO project VALUES ('p1', '/Users/x/alpha')")
    conn.execute("INSERT INTO project VALUES ('p2', '/Users/x/beta')")
    rows_session = [
        ("s1", "第一个任务", "/Users/x/alpha", None, "p1"),
        ("s2", "被跳过", "/Users/x/alpha", None, "p1"),
        ("s3", "子任务甲", "/Users/x/beta", "s1", "p2"),
    ]
    for sid, title, directory, parent, project in rows_session:
        conn.execute(
            "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
            (sid, title, directory, parent, project, base, base + 5000),
        )
    messages = [
        ("m1", "s1", "user", 1, base, json.dumps({"text": "帮我做这件事"})),
        (
            "m2",
            "s1",
            "assistant",
            2,
            base + 200,
            json.dumps(
                {
                    "cost": 0.5,
                    "tokens": {"input": 100, "output": 20, "reasoning": 5},
                    "model": {"providerID": "opencode-go", "id": "deepseek-v4.1-flash"},
                    "content": [
                        {"type": "tool", "name": "shell"},
                        {"type": "tool", "name": "shell"},
                        {"type": "tool", "name": "edit"},
                        {"type": "text", "text": "done"},
                    ],
                }
            ),
        ),
        ("m3", "s1", "idle", 3, base + 300, json.dumps({"outcome": "succeeded"})),
        ("m4", "s2", "user", 1, base + 100, json.dumps({"text": "每日工作记录的会话"})),
        ("m5", "s3", "user", 1, base + 400, json.dumps({"text": "帮我提炼金句"})),
    ]
    for mid, sid, mtype, seq, ts, data in messages:
        conn.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (mid, sid, mtype, seq, ts, ts, data),
        )
    conn.execute("UPDATE session_v2 SET title='每日工作记录 2026-01-01' WHERE id='s2'")
    conn.commit()
    conn.close()
    return path


def main():
    start, end = report.day_bounds("2026-10-11")
    check("day_bounds 起点", start, lambda v: v == int(datetime(2026, 10, 11).timestamp() * 1000))
    check("day_bounds 跨度一天", end - start, lambda v: v == 86400000)

    check("clip 截断", report.clip("a" * 100, 10), lambda v: v == "a" * 10 + "…")
    check("clip 不截断", report.clip("短", 10), lambda v: v == "短")
    check("fmt_tokens 百万", report.fmt_tokens(9_360_000), lambda v: v == "9.36m")
    check("fmt_tokens 千", report.fmt_tokens(574_500), lambda v: v == "574.5k")
    check("fmt_tokens 小值", report.fmt_tokens(42), lambda v: v == "42")
    check(
        "tool_summary 排序与中文化",
        report.tool_summary(["shell", "edit", "shell"]),
        lambda v: v == "终端 2、改文件 1",
    )
    check("tool_summary 未知工具原名", report.tool_summary(["bash"]), lambda v: v == "bash 1")

    fake = make_db()
    report.DB_PATH = str(fake)
    out = report.build_report("2026-10-11")
    check("含标题", out, lambda t: "# OpenCode 工作记录 2026-10-11" in t)
    check("含待补占位", out, lambda t: "## 今日概览" in t and "（待补）" in t)
    check("按目录分组", out, lambda t: "## alpha" in t and "## beta" not in t)
    check("会话标题出现", out, lambda t: "第一个任务" in t)
    check("跳过记录自身", out, lambda t: "被跳过" not in t and "每日工作记录" not in t)
    check("概览统计 1 个会话", out, lambda t: "1 个会话" in t)
    check("子任务计数", out, lambda t: "1 个子任务" in t)
    check("花费汇总", out, lambda t: "花费 $0.50" in t)
    check("模型名出现", out, lambda t: "deepseek-v4.1-flash(1)" in t)
    check("工具中文计数", out, lambda t: "终端 2、改文件 1" in t)
    check("派生关系", out, lambda t: "派生 1 个子任务：子任务甲" in t)
    check("空白天返回空状态", report.build_report("2000-01-01"), lambda t: "今天没有 OpenCode 会话记录" in t)

    print()
    if FAILURES:
        print(f"每日记录用例失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("每日记录全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import activity

FAILURES = []
NOW = 1_800_000_000.0


def check(name, actual, predicate, detail=""):
    ok = predicate(actual)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def sample():
    return [
        {
            "id": "ses_run",
            "title": "修一个 bug",
            "outcome": None,
            "time": {"updated": (NOW - 30) * 1000},
            "location": {"directory": "/Users/x/proj_a"},
        },
        {
            "id": "ses_done",
            "title": "写文档",
            "outcome": "succeeded",
            "time": {"updated": (NOW - 3600) * 1000},
            "location": {"directory": "/Users/x/proj_b"},
        },
        {
            "id": "ses_cur",
            "title": "当前对话",
            "outcome": None,
            "time": {"updated": (NOW - 5) * 1000},
            "location": {"directory": "/Users/x/cur"},
        },
    ]


def main():
    check("刚刚", activity.ago_text((NOW - 10) * 1000, now=NOW), lambda t: t == "刚刚")
    check("分钟", activity.ago_text((NOW - 300) * 1000, now=NOW), lambda t: t == "5 分钟前")
    check("小时", activity.ago_text((NOW - 7200) * 1000, now=NOW), lambda t: t == "2 小时前")
    check("天", activity.ago_text((NOW - 172800) * 1000, now=NOW), lambda t: t == "2 天前")
    check("缺时间戳", activity.ago_text(0, now=NOW), lambda t: t == "时间未知")

    check("运行中优先级", activity.status_of({"id": "a", "outcome": None}, {"a"}), lambda t: t == "运行中")
    check("已完成", activity.status_of({"id": "b", "outcome": "succeeded"}, set()), lambda t: t == "已完成")
    check("空闲", activity.status_of({"id": "c", "outcome": None}, set()), lambda t: t == "空闲")

    out = activity.compose(sample(), {"ses_run"}, exclude_id="ses_cur", now=NOW)
    check("含运行标记", out, lambda t: "运行中" in t)
    check("含完成标记", out, lambda t: "已完成" in t)
    check("含目录名", out, lambda t: "proj_a" in t)
    check("含相对时间", out, lambda t: "1 小时前" in t)
    check("排除当前会话", out, lambda t: "当前对话" not in t)
    check("运行中排在前", out, lambda t: t.index("运行中") < t.index("已完成") if "已完成" in t else False)

    check("空列表", activity.compose([], set()), lambda t: "没有其他" in t)
    check("全被排除", activity.compose(sample(), set(), exclude_id=None, limit=0), lambda t: "没有其他" in t)

    print()
    if FAILURES:
        print(f"会话动态用例失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("会话动态全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import describe, parse_command
from src import renderer
from src.session_router import SessionRouter

FAILURES = []


def check(name, actual, predicate, detail=""):
    ok = predicate(actual)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main():
    check("普通文本", parse_command("帮我改代码"), lambda r: r == (None, "帮我改代码"))
    check("带参命令", parse_command("/proj /tmp/x"), lambda r: r == ("proj", "/tmp/x"))
    check("大小写归一", parse_command("/HELP"), lambda r: r[0] == "help")
    check("空消息", parse_command("   "), lambda r: r == (None, ""))
    check("斜杠后无参", parse_command("/status"), lambda r: r == ("status", ""))

    tmp = Path("/private/var/folders/dy/rxcvzlvn61385zy1tkhh3_5h0000gn/T/opencode")
    allow = str((tmp / "cmdproj").resolve())
    (tmp / "cmdproj").mkdir(parents=True, exist_ok=True)

    r = SessionRouter(allow, {}, [allow])

    check("无会话描述", describe(None, r, "single:u1"),
          lambda t: "没有会话" in t)

    r.set_session("single:u1", "ses_abc", allow)
    d = describe(r.get("single:u1"), r, "single:u1")
    check("有会话含目录", d, lambda t: "cmdproj" in t)
    check("有会话含 id", d, lambda t: "ses_abc" in t)
    check("有会话含映射键", d, lambda t: "single:u1" in t)

    body = renderer.render("**标题**\n\n- 一\n- 二", 3000)
    check("帮助文本可渲染", body, lambda t: "标题" in t and "一" in t)

    print()
    if FAILURES:
        print(f"命令解析用例失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("命令解析全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
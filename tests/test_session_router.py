import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.session_router import DirNotAllowed, SessionRouter, resolve_dir

FAILURES = []


def check(name, actual, predicate, detail=""):
    ok = predicate(actual)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main():
    tmp = Path("/private/var/folders/dy/rxcvzlvn61385zy1tkhh3_5h0000gn/T/opencode")
    allow_a = str((tmp / "proj_a").resolve())
    allow_b = str((tmp / "proj_b").resolve())
    (tmp / "proj_a").mkdir(parents=True, exist_ok=True)
    (tmp / "proj_b").mkdir(parents=True, exist_ok=True)
    outside = str(Path("/etc").resolve())

    check("白名单内通过", resolve_dir(allow_a, [allow_a]), lambda v: v == allow_a)

    try:
        resolve_dir(outside, [allow_a])
        check("白名单外拒绝", "未抛出", lambda v: False)
    except DirNotAllowed:
        check("白名单外拒绝", "抛出", lambda v: True)

    try:
        resolve_dir(allow_a, [])
        check("空白名单拒绝", "未抛出", lambda v: False)
    except DirNotAllowed:
        check("空白名单拒绝", "抛出", lambda v: True)

    r = SessionRouter(allow_a, {"gid_1": allow_b}, [allow_a, allow_b])

    check("单聊键", r.key_for("single", None, "u1"), lambda v: v == "single:u1")
    check("群聊键", r.key_for("group", "gid_9", "u1"), lambda v: v == "group:gid_9")
    check("单聊目录", r.dir_for("single", None), lambda v: v == allow_a)
    check("群聊目录命中", r.dir_for("group", "gid_1"), lambda v: v == allow_b)
    try:
        r.dir_for("group", "gid_zzz")
        check("群聊未配置拒绝", "未抛出", lambda v: False)
    except DirNotAllowed:
        check("群聊未配置拒绝", "抛出", lambda v: True)

    r.set_session("single:u1", "ses_1", allow_a)
    r.set_stream("single:u1", "stream_1")
    check("流式id已记录", r.get("single:u1"), lambda e: e["stream_id"] == "stream_1")
    r.clear_stream("single:u1")
    check("流式id已清", r.get("single:u1"), lambda e: e["stream_id"] is None)

    r.switch_dir("single:u1", allow_b)
    entry = r.get("single:u1")
    check("切目录生效", entry, lambda e: e["directory"] == allow_b)
    check("切目录清会话", entry, lambda e: e["session_id"] is None)
    check("切目录清在途标记", entry, lambda e: e["in_flight"] is False)

    r.set_in_flight("single:u1", True)
    check("在途标记已置位", r.get("single:u1"), lambda e: e["in_flight"] is True)
    r.set_session("single:u1", "ses_2", allow_b)
    check("新建会话清在途标记", r.get("single:u1"), lambda e: e["in_flight"] is False)

    state = tmp / "router_state.json"
    r3 = SessionRouter(allow_a, {}, [allow_a], str(state))
    r3.set_session("single:u9", "ses_9", allow_a)
    r3.set_in_flight("single:u9", True)
    r4 = SessionRouter(allow_a, {}, [allow_a], str(state))
    check("在途标记落盘恢复", r4.get("single:u9"), lambda e: e["in_flight"] is True, str(r4.get("single:u9")))

    r2 = SessionRouter(allow_a, {}, [allow_a])
    try:
        r2.switch_dir("single:u2", "/etc")
        check("切换越权拒绝", "未抛出", lambda v: False)
    except DirNotAllowed:
        check("切换越权拒绝", "抛出", lambda v: True)

    async def run():
        lock = r.lock_for("single:u1")
        async with lock:
            got = "held"
        lock2 = r.lock_for("single:u1")
        check("同键复用锁", lock2 is lock, lambda v: v is True)
        return got

    check("锁内执行", asyncio.run(run()), lambda v: v == "held")

    print()
    if FAILURES:
        print(f"会话路由用例失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("会话路由全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
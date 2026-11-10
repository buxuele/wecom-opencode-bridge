import asyncio
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import Bridge, heartbeat_interval, heartbeat_targets, heartbeat_text
from src.session_router import SessionRouter

FAILURES = []
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

ALLOW = str((Path("/private/var/folders/dy/rxcvzlvn61385zy1tkhh3_5h0000gn/T/opencode") / "hbproj").resolve())


def check(name, actual, predicate, detail=""):
    ok = predicate(actual)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


class RecordingLink:
    def __init__(self, boom=False):
        self.sent = []
        self.boom = boom

    async def send_markdown(self, chatid, chat_type, content):
        if self.boom:
            raise RuntimeError("模拟企业微信推送失败")
        self.sent.append((chatid, chat_type, content))


def make_stub(router, link, interval):
    return SimpleNamespace(
        router=router, link=link, cfg={"heartbeat_interval": interval}, menu_shown={}
    )


def test_targets():
    empty = SessionRouter(ALLOW, {}, [])
    check("空路由无目标", heartbeat_targets(empty), lambda r: r == [])

    r = SessionRouter(ALLOW, {}, [ALLOW])
    r.set_session("single:u1", "ses_1", ALLOW)
    r.set_session("single:u2", "ses_2", ALLOW)
    got = heartbeat_targets(r)
    check(
        "单聊两个目标",
        got,
        lambda g: g == [("single:u1", "u1", 1), ("single:u2", "u2", 1)],
        str(got),
    )
    check("目标顺序稳定", got, lambda ignored: got == heartbeat_targets(r), str(got))

    g = SessionRouter(ALLOW, {"g1": ALLOW}, [ALLOW])
    g.set_session("group:g1", "ses_3", ALLOW)
    check(
        "群聊目标带类型 2",
        heartbeat_targets(g),
        lambda r: r == [("group:g1", "g1", 2)],
        str(heartbeat_targets(g)),
    )

    c = SessionRouter(ALLOW, {}, [ALLOW])
    c.set_session("group:a:b", "ses_4", ALLOW)
    check(
        "chatid 含冒号不被切错",
        heartbeat_targets(c),
        lambda r: r == [("group:a:b", "a:b", 2)],
        str(heartbeat_targets(c)),
    )


def test_text():
    body = heartbeat_text(0)
    first = body.splitlines()[0]
    check("首行是时间戳", first, lambda t: bool(STAMP.match(t)), first)
    local = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(0))
    check("时间戳按本地时区", first, lambda t: t == local, f"{first} != {local}")
    check("时间戳无千位分隔", first, lambda t: "," not in t)
    check("带快捷菜单标题", body, lambda t: "快捷菜单" in t, body)
    check("带菜单第 1 项", body, lambda t: "1. 其他会话动态" in t, body)
    check("带菜单第 2 项", body, lambda t: "2. 日期与天气" in t, body)
    check("带控制命令", body, lambda t: "**控制命令**" in t and "/status" in t, body)


def test_once():
    r = SessionRouter(ALLOW, {}, [ALLOW])
    r.set_session("single:u1", "ses_1", ALLOW)
    r.set_session("group:g1", "ses_9", ALLOW)
    link = RecordingLink()
    stub = make_stub(r, link, 60)
    asyncio.run(Bridge._heartbeat_once(stub))
    check("推送两条", link.sent, lambda s: len(s) == 2, str(link.sent))
    check("单聊用类型 1", link.sent, lambda s: ("u1", 1, s[0][2]) in s, str(link.sent))
    check("群聊用类型 2", link.sent, lambda s: ("g1", 2, s[1][2]) in s, str(link.sent))
    check("内容带时间戳", link.sent, lambda s: bool(STAMP.match(s[0][2].splitlines()[0])), str(link.sent))
    check("内容带菜单", link.sent, lambda s: "快捷菜单" in s[0][2], str(link.sent))
    check("心跳打开菜单窗口", stub.menu_shown, lambda m: set(m) == {"single:u1", "group:g1"}, str(stub.menu_shown))

    empty = SessionRouter(ALLOW, {}, [ALLOW])
    link2 = RecordingLink()
    asyncio.run(Bridge._heartbeat_once(make_stub(empty, link2, 60)))
    check("无目标时不推送", link2.sent, lambda s: s == [], str(link2.sent))

    boom = RecordingLink(boom=True)
    try:
        asyncio.run(Bridge._heartbeat_once(make_stub(r, boom, 60)))
        check("推送异常不抛出", True, lambda v: v is True)
    except Exception as exc:
        check("推送异常不抛出", exc, lambda v: False, repr(exc))


def test_loop():
    r = SessionRouter(ALLOW, {}, [ALLOW])
    r.set_session("single:u1", "ses_1", ALLOW)
    link = RecordingLink()
    stop = asyncio.Event()
    stop.set()
    stub = make_stub(r, link, 60)
    stub.stop_event = stop
    asyncio.run(Bridge._heartbeat_loop(stub))
    check("已置停止位则不推送", link.sent, lambda s: s == [], str(link.sent))

    off = make_stub(r, RecordingLink(), 0)
    off.stop_event = asyncio.Event()
    asyncio.run(Bridge._heartbeat_loop(off))
    check("间隔为 0 直接返回", True, lambda v: v is True)


def test_interval_not_compressed():
    check("间隔只取配置值", heartbeat_interval({"heartbeat_interval": 18000}), lambda v: v == 18000)
    check("缺配置视为关闭", heartbeat_interval({}), lambda v: v == 0)
    check("空配置视为关闭", heartbeat_interval(None), lambda v: v == 0)

    r = SessionRouter(ALLOW, {}, [ALLOW])
    r.set_session("single:u1", "ses_1", ALLOW)
    link = RecordingLink()
    stop = asyncio.Event()
    stub = make_stub(r, link, 18000)
    stub.stop_event = stop
    stub.running = True

    seen = []
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(aw, timeout):
        seen.append(timeout)
        stop.set()
        return await real_wait_for(aw, timeout)

    asyncio.wait_for = fake_wait_for
    try:
        asyncio.run(Bridge._heartbeat_loop(stub))
    finally:
        asyncio.wait_for = real_wait_for
    check("运行中也不压缩间隔", seen[:1], lambda v: v == [18000], str(seen))


def main():
    from log_redirect import log_redirect

    with log_redirect():
        test_targets()
        test_text()
        test_once()
        test_loop()
        test_interval_not_compressed()
    print()
    if FAILURES:
        print(f"心跳用例失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("心跳全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

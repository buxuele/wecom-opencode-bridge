import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from bridge import (
    PENDING_PERMISSION,
    TASK_WATCHDOG_SEC,
    Bridge,
    acquire_singleton_lock,
)
from src.session_router import SessionRouter

FAILURES = []


def check(name, actual, predicate, detail=""):
    ok = predicate(actual)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


class MockLink:
    def __init__(self, robot_name=""):
        self.robot_name = robot_name
        self.streams = []
        self.markdowns = []
        self.cards = []
        self.updated_cards = []
        self.welcomes = []
        self.stopped = False
        self._seq = 0

    def new_stream_id(self):
        self._seq += 1
        return f"s{self._seq}"

    async def reply_stream(self, frame, stream_id, content, finish=False):
        self.streams.append({"id": stream_id, "content": content, "finish": finish})

    async def send_markdown(self, chatid, chat_type, content):
        self.markdowns.append({"chatid": chatid, "chat_type": chat_type, "content": content})

    async def send_card(self, chatid, chat_type, card):
        self.cards.append({"chatid": chatid, "chat_type": chat_type, "card": card})

    async def update_card(self, frame, card):
        self.updated_cards.append(card)

    async def reply_welcome(self, frame, content):
        self.welcomes.append(content)

    async def start(self):
        return None

    def stop(self):
        self.stopped = True
        return None


class MockClient:
    def __init__(self):
        self.created = []
        self.prompts = []
        self.deleted = []
        self.permission_replies = []
        self.fail_prompt_times = 0
        self.fail_permission_times = 0

    async def create_session(self, directory, title=None):
        self.created.append(directory)
        return {"id": f"ses_{len(self.created)}"}

    async def delete_session(self, session_id):
        self.deleted.append(session_id)
        return True

    async def send_prompt(self, session_id, text):
        if self.fail_prompt_times > 0:
            self.fail_prompt_times -= 1
            raise _http_error(404)
        self.prompts.append({"session_id": session_id, "text": text})
        return {}

    async def interrupt(self, session_id):
        return True

    async def reply_permission(self, session_id, request_id, decision, message=None):
        if self.fail_permission_times > 0:
            self.fail_permission_times -= 1
            raise _http_error(500)
        self.permission_replies.append(
            {"session_id": session_id, "request_id": request_id, "decision": decision}
        )
        return True

    async def default_model(self):
        return "opencode-go/deepseek-v4.1-flash"

    async def list_sessions(self, limit=20, order="desc", parent_id=None):
        return [
            {
                "id": "ses_other",
                "title": "别人的活",
                "outcome": "succeeded",
                "time": {"updated": 1_800_000_000_000},
                "location": {"directory": "/Users/x/proj_other"},
            }
        ]

    async def active_sessions(self):
        return {"ses_other"}

    async def stream_events(self, queue, stop_event):
        await stop_event.wait()


def _http_error(status):
    request = httpx.Request("POST", "http://127.0.0.1:4096/api/session/x/prompt")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def make_cfg(tmp, **over):
    cfg = {
        "bot_id": "bot",
        "bot_secret": "sec",
        "base_url": "http://127.0.0.1:4096",
        "username": "opencode",
        "password": "pwd",
        "default_dir": str(tmp),
        "group_dirs": {},
        "allowed_dirs": [str(tmp)],
        "robot_name": "",
        "owner_ids": [],
        "state_path": None,
        "stream_min_chars": 300,
        "stream_min_interval": 0,
        "render_max_chars": 3000,
        "heartbeat_interval": 60,
        "confirm_timeout": 0,
    }
    cfg.update(over)
    return cfg


def make_bridge(cfg, robot_name=""):
    bridge = Bridge(cfg)
    bridge.link = MockLink(robot_name)
    bridge.client = MockClient()
    return bridge


def single_frame(text, userid="u1"):
    return {
        "body": {
            "msgtype": "text",
            "chattype": "single",
            "from": {"userid": userid},
            "text": {"content": text},
        }
    }


def group_frame(text, chatid="g1", userid="u1", mentioned=None):
    body = {
        "msgtype": "text",
        "chattype": "group",
        "chatid": chatid,
        "from": {"userid": userid},
        "text": {"content": text},
    }
    if mentioned is not None:
        body["mentioned_list"] = mentioned
    return {"body": body}


def _text_payload(session_id, ref):
    mid, _, ordinal = ref.partition(":")
    return {"sessionID": session_id, "assistantMessageID": mid, "ordinal": int(ordinal)}


def started_event(session_id, ref="msg_1:0"):
    return {"type": "session.text.started", "data": _text_payload(session_id, ref)}


def delta_event(session_id, delta, ref="msg_1:0"):
    data = _text_payload(session_id, ref)
    data["delta"] = delta
    return {"type": "session.text.delta", "data": data}


def ended_event(session_id, text, ref="msg_1:0"):
    data = _text_payload(session_id, ref)
    data["text"] = text
    return {"type": "session.text.ended", "data": data}


def succeeded_event(session_id):
    return {"type": "session.execution.succeeded", "data": {"sessionID": session_id}}


def permission_event(req_id, session_id, message="编辑文件", resources=("a.py",)):
    return {
        "type": "permission.asked",
        "data": {
            "id": req_id,
            "sessionID": session_id,
            "action": "edit",
            "resources": list(resources),
            "message": message,
        },
    }


def card_frame(event_key, task_id):
    return {
        "body": {
            "msgtype": "event",
            "chattype": "single",
            "from": {"userid": "u1"},
            "event": {
                "eventtype": "template_card_event",
                "template_card_event": {
                    "card_type": "button_interaction",
                    "event_key": event_key,
                    "task_id": task_id,
                },
            },
        }
    }


def unsupported_frame(msgtype, userid="u1"):
    return {
        "body": {
            "msgtype": msgtype,
            "chattype": "single",
            "from": {"userid": userid},
            msgtype: {"url": "https://example.invalid/media"},
        }
    }


def last_content(link):
    return link.streams[-1]["content"] if link.streams else ""


async def case_reply_finish(tmp):
    cfg = make_cfg(tmp)
    b = make_bridge(cfg)
    await b._reply(single_frame("x"), "single:u1", "x" * 4000)
    finishes = [s["finish"] for s in b.link.streams]
    check("命令回复仅最后一片收尾", finishes, lambda f: f[-1] is True and not any(f[:-1]))
    check("命令回复分片不超限", b.link.streams, lambda ss: all(len(s["content"]) <= 1801 for s in ss))
    check("命令回复内容完整", "".join(s["content"] for s in b.link.streams), lambda t: "xxxx" in t)


async def case_group_mention(tmp):
    cfg = make_cfg(tmp, group_dirs={"g1": str(tmp)})
    b = make_bridge(cfg, robot_name="MyBot")
    await b._on_text(group_frame("@MyBot 帮我改代码", mentioned=["MyBot"]))
    check("群聊精确剥离@", b.client.prompts, lambda p: len(p) == 1 and p[0]["text"] == "帮我改代码", str(b.client.prompts))
    b2 = make_bridge(cfg)
    await b2._on_text(group_frame("@优宝 你好", mentioned=["优宝"]))
    check("群聊通用剥离@", b2.client.prompts, lambda p: len(p) == 1 and p[0]["text"] == "你好", str(b2.client.prompts))
    b3 = make_bridge(cfg, robot_name="MyBot")
    await b3._on_text(group_frame("没提我", mentioned=["OtherBot"]))
    check("群聊未提机器人不处理", b3.client.prompts, lambda p: p == [])


async def case_group_dir_denied(tmp):
    cfg = make_cfg(tmp)
    b = make_bridge(cfg)
    await b._on_text(group_frame("hello", chatid="gX"))
    check("群聊未配置目录拒绝", b.link.streams, lambda ss: ss and "拒绝" in ss[0]["content"])
    check("群聊拒绝不建会话", b.client.created, lambda c: c == [])


async def case_permission_timeout(tmp):
    cfg = make_cfg(tmp, confirm_timeout=0)
    b = make_bridge(cfg)
    b.router.set_session("single:u1", "ses_1", str(tmp))
    await b._dispatch(permission_event("per_1", "ses_1", resources=["a.py", "b.py"]))
    check("权限卡片带资源", b.link.cards, lambda c: c and "a.py" in c[0]["card"]["main_title"]["desc"])
    check("待确认已登记", "per_1" in PENDING_PERMISSION, lambda v: v is True)
    await asyncio.sleep(0.05)
    replies = b.client.permission_replies
    check("超时自动拒绝", replies, lambda r: len(r) == 1 and r[0]["decision"] == "reject", str(replies))
    check("超时通知用户", b.link.markdowns, lambda m: m and "超时" in m[0]["content"])
    check("超时后清空待确认", "per_1" in PENDING_PERMISSION, lambda v: v is False)


async def case_card_decision(tmp):
    cfg = make_cfg(tmp, confirm_timeout=60)
    b = make_bridge(cfg)
    b.router.set_session("single:u1", "ses_1", str(tmp))
    await b._dispatch(permission_event("per_1", "ses_1"))
    await b._on_card_event(card_frame("allow", "per_1"))
    replies = b.client.permission_replies
    check("允许按键放行一次", replies, lambda r: len(r) == 1 and r[0]["decision"] == "once")
    check("点击后卡片更新", b.link.updated_cards, lambda c: len(c) == 1 and c[0]["main_title"]["title"] == "已处理")
    check("点击后清空待确认", "per_1" in PENDING_PERMISSION, lambda v: v is False)

    b2 = make_bridge(cfg)
    b2.router.set_session("single:u1", "ses_1", str(tmp))
    await b2._dispatch(permission_event("per_2", "ses_1"))
    await b2._on_card_event(card_frame("未知按键", "per_2"))
    replies2 = b2.client.permission_replies
    check("未知按键按拒绝", replies2, lambda r: len(r) == 1 and r[0]["decision"] == "reject", str(replies2))

    b3 = make_bridge(cfg)
    b3.router.set_session("single:u1", "ses_1", str(tmp))
    await b3._dispatch(permission_event("per_3", "ses_1"))
    await b3._on_card_event(card_frame("deny", "不存在"))
    check("无待确认项不回复", b3.client.permission_replies, lambda r: r == [])


async def case_part_aggregation(tmp):
    cfg = make_cfg(tmp)
    b = make_bridge(cfg)
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b.router.set_stream("single:u1", "s1")
    await b._dispatch(started_event("ses_1"))
    await b._dispatch(delta_event("ses_1", "你好"))
    check("首个增量有内容", last_content(b.link), lambda t: "你好" in t, last_content(b.link))
    await b._dispatch(delta_event("ses_1", "，世界"))
    check("增量累加", last_content(b.link), lambda t: t == "你好，世界", last_content(b.link))
    await b._dispatch(ended_event("ses_1", "你好，世界！"))
    check("ended 全文替换不叠加", last_content(b.link), lambda t: t == "你好，世界！", last_content(b.link))

    reasoning = {
        "type": "session.reasoning.delta",
        "data": {"sessionID": "ses_1", "assistantMessageID": "msg_1", "ordinal": 0, "delta": "思考中"},
    }
    await b._dispatch(reasoning)
    check("思考事件不进正文", last_content(b.link), lambda t: t == "你好，世界！", last_content(b.link))

    b2 = make_bridge(cfg)
    b2.router.set_session("single:u1", "ses_1", str(tmp))
    b2.router.set_stream("single:u1", "s1")
    await b2._dispatch(delta_event("ses_1", "第一段"))
    await b2._dispatch(ended_event("ses_1", "第一段全文", ref="msg_1:0"))
    await b2._dispatch(delta_event("ses_1", "第二段", ref="msg_2:1"))
    check("多段文本都保留", last_content(b2.link), lambda t: t == "第一段全文第二段", last_content(b2.link))

    b3 = make_bridge(cfg)
    b3.router.set_session("single:u1", "ses_1", str(tmp))
    b3.router.set_stream("single:u1", "s1")
    await b3._dispatch(delta_event("ses_1", "旧内容"))
    await b3._dispatch(started_event("ses_1"))
    check("started 重置槽位", b3._plain("single:u1"), lambda t: t == "", repr(b3._plain("single:u1")))

    b4 = make_bridge(cfg)
    b4.router.set_session("single:u1", "ses_1", str(tmp))
    b4.router.set_stream("single:u1", "s1")
    await b4._dispatch(delta_event("ses_1", "完成内容"))
    await b4._dispatch(succeeded_event("ses_1"))
    check("执行成功收尾", b4.link.streams, lambda ss: ss and ss[-1]["finish"] is True)
    check("收尾带正文", last_content(b4.link), lambda t: "完成内容" in t, last_content(b4.link))
    check("收尾后清流", b4.router.get("single:u1")["stream_id"], lambda v: v is None)


async def case_session_error(tmp):
    cfg = make_cfg(tmp)
    b = make_bridge(cfg)
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b.router.set_stream("single:u1", "s1")
    await b._dispatch(delta_event("ses_1", "半截输出"))
    await b._dispatch({"type": "session.error", "data": {"sessionID": "ses_1", "error": {"message": "模型连接失败"}}})
    check("出错也收尾", b.link.streams, lambda ss: ss and ss[-1]["finish"] is True)
    check("出错有提示", last_content(b.link), lambda t: "任务中断" in t and "模型连接失败" in t)
    check("出错后清流", b.router.get("single:u1")["stream_id"], lambda v: v is None)


async def case_idle_empty(tmp):
    cfg = make_cfg(tmp)
    b = make_bridge(cfg)
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b.router.set_stream("single:u1", "s1")
    await b._dispatch({"type": "session.idle", "data": {"sessionID": "ses_1"}})
    check("空输出仍收尾", b.link.streams, lambda ss: ss and ss[-1]["finish"] is True)
    check("空输出有占位", last_content(b.link), lambda t: "无输出" in t)


async def case_long_output_chunks(tmp):
    cfg = make_cfg(tmp, render_max_chars=1000)
    b = make_bridge(cfg)
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b.router.set_stream("single:u1", "s1")
    await b._dispatch(delta_event("ses_1", "内" * 3500))
    await b._dispatch({"type": "session.idle", "data": {"sessionID": "ses_1"}})
    check("超长收尾首片收流", b.link.streams, lambda ss: ss and ss[-1]["finish"] is True)
    check("超长首片不超限", last_content(b.link), lambda t: len(t) <= 1001, str(len(last_content(b.link))))
    check("超长剩余继续推", b.link.markdowns, lambda m: len(m) >= 2, f"得到 {len(b.link.markdowns)} 条")
    check("续推带序号", b.link.markdowns, lambda m: m and "(2/" in m[0]["content"])
    total = len(last_content(b.link)) + sum(len(m["content"]) for m in b.link.markdowns)
    check("续推后内容完整", total, lambda n: n >= 3500, f"合计 {total}")


async def case_cmd_new(tmp):
    cfg = make_cfg(tmp)
    b = make_bridge(cfg)
    await b._on_text(single_frame("干活"))
    await b._on_text(single_frame("/new"))
    await asyncio.sleep(0.05)
    entry = b.router.get("single:u1")
    check("新建清会话", entry, lambda e: e["session_id"] is None and e["stream_id"] is None)
    check("新建删远端会话", b.client.deleted, lambda d: len(d) == 1, str(b.client.deleted))


async def case_session_rebuild(tmp):
    cfg = make_cfg(tmp)
    b = make_bridge(cfg)
    b.client.fail_prompt_times = 1
    await b._on_text(single_frame("干活"))
    check("失效后重建会话", b.client.created, lambda c: len(c) == 2, str(b.client.created))
    check("重建后重发成功", b.client.prompts, lambda p: len(p) == 1 and p[0]["session_id"] == "ses_2", str(b.client.prompts))
    check("重建不误报失败", b.link.streams, lambda ss: all(not s["finish"] for s in ss))

    b2 = make_bridge(cfg)
    b2.client.fail_prompt_times = 2
    await b2._on_text(single_frame("干活"))
    check("二次失败仍收尾", b2.link.streams, lambda ss: ss and ss[-1]["finish"] is True)
    check("二次失败有提示", last_content(b2.link), lambda t: "任务失败" in t)


async def case_no_proxy(tmp):
    from src.opencode_client import OpenCodeClient

    client = OpenCodeClient("http://127.0.0.1:4096", {"Authorization": "x"})
    check("本地客户端不走系统代理", client._client().trust_env, lambda v: v is False)


async def case_wecom_direct(tmp):
    from src import wecom_link

    wecom_link.force_direct_connection()
    wecom_link.force_direct_connection()
    check("重复调用不叠加包装", wecom_link.websockets.connect is wecom_link._connect_direct, lambda v: v is True)

    captured = {}

    def fake_connect(uri, **kwargs):
        captured["uri"] = uri
        captured.update(kwargs)
        raise RuntimeError("拦截，不真的建连")

    original = wecom_link._WsConnect
    wecom_link._WsConnect = fake_connect
    try:
        try:
            await wecom_link.websockets.connect("wss://openws.work.weixin.qq.com", close_timeout=5)
        except RuntimeError:
            pass
    finally:
        wecom_link._WsConnect = original

    check("企业微信 Websocket 强制直连", captured.get("proxy") if "proxy" in captured else "未注入", lambda v: v is None)
    check("proxy 键确实被注入而非缺省", "proxy" in captured, lambda v: v is True)
    check("转发 SDK 原有参数", captured.get("close_timeout"), lambda v: v == 5)


async def case_create_session_unwrap(tmp):
    import httpx

    from src.opencode_client import OpenCodeClient

    def wrapped(request):
        return httpx.Response(
            200,
            json={"data": {"id": "ses_wrapped", "location": {"directory": str(tmp)}}},
        )

    def plain(request):
        return httpx.Response(200, json={"id": "ses_plain"})

    def make_client(handler):
        class Patched(OpenCodeClient):
            def _client(self, extra=None):
                h = dict(self.headers)
                h.update(extra or {})
                return httpx.AsyncClient(
                    base_url=self.base_url,
                    headers=h,
                    timeout=self.timeout,
                    trust_env=False,
                    transport=httpx.MockTransport(handler),
                )

        return Patched("http://127.0.0.1:1", {})

    session = await make_client(wrapped).create_session(str(tmp))
    check("建会话解包 data", session.get("id"), lambda v: v == "ses_wrapped", str(session))
    session2 = await make_client(plain).create_session(str(tmp))
    check("无 data 包裹时原样", session2.get("id"), lambda v: v == "ses_plain", str(session2))


async def case_chat_and_error_log(tmp):
    import bridge as bridge_mod

    chats = []
    errors = []
    orig_chat = bridge_mod.logger.chat
    orig_error = bridge_mod.logger.error
    bridge_mod.logger.chat = lambda *a, **k: chats.append(a)
    bridge_mod.logger.error = lambda msg, *a: errors.append(msg)
    try:
        cfg = make_cfg(tmp)
        b = make_bridge(cfg)
        await b._on_text(single_frame("帮我改代码"))
        check("收到消息进聊天记录", chats, lambda c: any(a[0] == "收" and a[3] == "帮我改代码" for a in c), str(chats[:3]))
        check("发出消息进聊天记录", chats, lambda c: any(a[0] == "发" for a in c), str(chats[:3]))

        b.router.set_session("single:u1", "ses_1", str(tmp))
        b.router.set_stream("single:u1", "s1")
        await b._dispatch(
            {
                "type": "session.error",
                "data": {"sessionID": "ses_1", "error": {"message": "key limit exceeded"}},
            }
        )
        check(
            "错误原文进日志",
            errors,
            lambda e: any("key limit exceeded" in m for m in e),
            str(errors),
        )
        check(
            "错误原始事件进日志",
            errors,
            lambda e: any("session.error" in m for m in e),
            str(errors),
        )
    finally:
        bridge_mod.logger.chat = orig_chat
        bridge_mod.logger.error = orig_error


async def case_card_failure_keeps_pending(tmp):
    cfg = make_cfg(tmp, confirm_timeout=60)
    b = make_bridge(cfg)
    b.router.set_session("single:u1", "ses_1", str(tmp))
    await b._dispatch(permission_event("per_1", "ses_1"))
    b.client.fail_permission_times = 1
    await b._on_card_event(card_frame("allow", "per_1"))
    check("回复失败仍保留待确认", "per_1" in PENDING_PERMISSION, lambda v: v is True)
    check(
        "回复失败卡片标处理失败",
        b.link.updated_cards,
        lambda c: c and c[-1]["main_title"]["title"] == "处理失败",
        str(b.link.updated_cards[-1:]),
    )
    pending = PENDING_PERMISSION.pop("per_1", None)
    if pending and pending.get("timer"):
        pending["timer"].cancel()


async def case_interrupted_finishes(tmp):
    cfg = make_cfg(tmp)
    b = make_bridge(cfg)
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b.router.set_stream("single:u1", "s1")
    await b._dispatch(delta_event("ses_1", "半截输出"))
    await b._dispatch({"type": "session.execution.interrupted", "data": {"sessionID": "ses_1"}})
    check("中断也收尾", b.link.streams, lambda ss: ss and ss[-1]["finish"] is True)
    check("中断有提示", last_content(b.link), lambda t: "已中断" in t, last_content(b.link))
    check("中断后清流", b.router.get("single:u1")["stream_id"], lambda v: v is None)


async def case_commands_finish_stream(tmp):
    cfg = make_cfg(tmp)
    b = make_bridge(cfg)
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b.router.set_stream("single:u1", "s1")
    await b._dispatch(delta_event("ses_1", "跑着呢"))
    await b._on_text(single_frame("/stop"))
    check(
        "stop 收尾原流",
        b.link.streams,
        lambda ss: any(s["finish"] and "跑着呢" in s["content"] for s in ss),
        str(b.link.streams)[:200],
    )

    b2 = make_bridge(cfg)
    b2.router.set_session("single:u1", "ses_1", str(tmp))
    b2.router.set_stream("single:u1", "s1")
    await b2._dispatch(delta_event("ses_1", "跑着呢"))
    await b2._on_text(single_frame("/new"))
    check(
        "new 收尾原流",
        b2.link.streams,
        lambda ss: any(s["finish"] and "跑着呢" in s["content"] for s in ss),
        str(b2.link.streams)[:200],
    )

    b3 = make_bridge(cfg)
    b3.router.set_session("single:u1", "ses_1", str(tmp))
    b3.router.set_stream("single:u1", "s1")
    await b3._dispatch(delta_event("ses_1", "跑着呢"))
    await b3._on_text(single_frame(f"/proj {tmp}"))
    check(
        "proj 收尾原流",
        b3.link.streams,
        lambda ss: any(s["finish"] and "跑着呢" in s["content"] for s in ss),
        str(b3.link.streams)[:200],
    )


async def case_owner_whitelist(tmp):
    cfg = make_cfg(tmp, owner_ids=["u1"])
    b = make_bridge(cfg)
    await b._on_text(single_frame("干活", userid="u1"))
    check("白名单内放行", b.client.prompts, lambda p: len(p) == 1)
    b2 = make_bridge(cfg)
    await b2._on_text(single_frame("干活", userid="u9"))
    check("白名单外不处理", b2.client.prompts, lambda p: p == [])
    check("白名单外有提示", b2.link.streams, lambda ss: ss and "无权" in ss[0]["content"])


async def case_rate_limit(tmp):
    cfg = make_cfg(tmp, stream_min_chars=1, stream_min_interval=0)
    b = make_bridge(cfg)
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b.router.set_stream("single:u1", "s1")
    for _ in range(30):
        await b._dispatch(delta_event("ses_1", "x" * 10))
    pushed = [s for s in b.link.streams if not s["finish"]]
    check("超限后跳过推送", len(pushed), lambda n: n <= 25, f"实际 {len(pushed)} 条")


async def case_status_text(tmp):
    cfg = make_cfg(tmp)
    b = make_bridge(cfg)
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b.running.add("single:u1")
    text = await b._status_text("single:u1")
    check("状态含运行中", text, lambda t: "运行中" in t, text)
    check("状态含默认模型", text, lambda t: "deepseek" in t, text)
    check("状态含待确认数", text, lambda t: "待确认权限" in t, text)


async def case_quick_menu(tmp):
    import bridge as bridge_mod

    cfg = make_cfg(tmp)
    b = make_bridge(cfg)
    for trigger in ["help", "帮助", "?", "？"]:
        b.link.streams.clear()
        await b._on_text(single_frame(trigger))
        check(f"触发词 {trigger} 弹菜单", last_content(b.link), lambda t: "快捷菜单" in t and "1." in t and "2." in t, last_content(b.link))
    check("菜单不建会话", b.client.created, lambda c: c == [])
    check("菜单不发 prompt", b.client.prompts, lambda p: p == [])

    b.link.streams.clear()
    await b._on_text(single_frame("1"))
    check("序号 1 显示会话动态", last_content(b.link), lambda t: "别人的活" in t and "proj_other" in t, last_content(b.link))

    orig_fetch = bridge_mod.weather.fetch
    bridge_mod.weather.fetch = lambda city="", now=None, ip_fallback=False: f"天气占位 {city}"
    try:
        b.link.streams.clear()
        await b._on_text(single_frame("2"))
        check("序号 2 显示天气", last_content(b.link), lambda t: "天气占位" in t, last_content(b.link))

        b.link.streams.clear()
        await b._on_text(single_frame("菜单2"))
        check("菜单2 前缀也可选", last_content(b.link), lambda t: "天气占位" in t, last_content(b.link))
    finally:
        bridge_mod.weather.fetch = orig_fetch

    b2 = make_bridge(make_cfg(tmp))
    await b2._on_text(single_frame("帮我 help 一下"))
    check("含 help 的句子照常发 prompt", b2.client.prompts, lambda p: len(p) == 1 and p[0]["text"] == "帮我 help 一下", str(b2.client.prompts))
    b3 = make_bridge(make_cfg(tmp))
    await b3._on_text(single_frame("9"))
    check("越界数字照常发 prompt", b3.client.prompts, lambda p: len(p) == 1 and p[0]["text"] == "9", str(b3.client.prompts))


async def case_menu_always(tmp):
    import bridge as bridge_mod

    b = make_bridge(make_cfg(tmp))
    await b._on_text(single_frame("1"))
    check("没弹过菜单也按菜单执行", last_content(b.link), lambda t: "别人的活" in t, last_content(b.link))
    check("菜单编号不进模型", b.client.prompts, lambda p: p == [], str(b.client.prompts))

    b2 = make_bridge(make_cfg(tmp))
    b2.menu_shown["single:u1"] = 0
    orig_fetch = bridge_mod.weather.fetch
    bridge_mod.weather.fetch = lambda city="", now=None, ip_fallback=False: "天气占位"
    try:
        await b2._on_text(single_frame("2"))
        check("菜单状态清零后数字仍是菜单", last_content(b2.link), lambda t: "天气占位" in t, last_content(b2.link))
    finally:
        bridge_mod.weather.fetch = orig_fetch

    b3 = make_bridge(make_cfg(tmp))
    lock = b3.router.lock_for("single:u1")
    await lock.acquire()
    try:
        await asyncio.wait_for(b3._on_text(single_frame("1")), timeout=2)
        check("任务占用会话锁时菜单仍即时响应", last_content(b3.link),
              lambda t: "别人的活" in t, last_content(b3.link))
    finally:
        lock.release()

    b4 = make_bridge(make_cfg(tmp))
    await b4._on_text(single_frame("菜单2"))
    check("显式前缀仍然可用", last_content(b4.link), lambda t: "天气" in t, last_content(b4.link))


async def case_push_handover(tmp):
    b = make_bridge(make_cfg(tmp))
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b.router.set_stream("single:u1", "s1")
    b._begin_stream("single:u1")
    b.parts["single:u1"] = {"m:0": {"type": "text", "text": "第一段"}}

    await b._handover_stream("single:u1")
    check("交接时给原流收尾", b.link.streams, lambda ss: ss and ss[-1]["finish"] is True, str(b.link.streams))
    check("交接后进入推送模式", "single:u1" in b.push_mode, lambda v: v is True)
    check("交接后流已清空", b.router.get("single:u1")["stream_id"], lambda v: v is None)
    check("交接时把已有正文补推出去", b.link.markdowns,
          lambda m: any("第一段" in x["content"] for x in m), str(b.link.markdowns))

    b.parts["single:u1"]["m:0"]["text"] += "第二段"
    await b._flush("single:u1", finish=False)
    check("交接后增量走推送", b.link.markdowns, lambda m: m and "第二段" in m[-1]["content"], str(b.link.markdowns))
    check("推送不重复已发内容", b.link.markdowns, lambda m: m[-1]["content"].count("第一段") == 0, str(b.link.markdowns))

    await b._flush("single:u1", finish=True)
    check("推送收尾不带重复正文", b.link.markdowns,
          lambda m: "第一段" not in m[-1]["content"] and "第二段" not in m[-1]["content"],
          str(b.link.markdowns))


async def case_watchdog_finish(tmp):
    b = make_bridge(make_cfg(tmp))
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b.router.set_stream("single:u1", "s1")
    b._begin_stream("single:u1")
    b.running.add("single:u1")
    b.parts["single:u1"] = {"m:0": {"type": "text", "text": "写到一半"}}
    b.last_event["single:u1"] = time.time() - TASK_WATCHDOG_SEC - 5

    await b._guard_once()
    check("看门狗强制收尾", b.link.streams, lambda ss: any(s["finish"] for s in ss), str(b.link.streams))
    check("看门狗后清流", b.router.get("single:u1")["stream_id"], lambda v: v is None)
    check("看门狗后移除运行标记", "single:u1" in b.running, lambda v: v is False)


async def case_unsupported_message(tmp):
    b = make_bridge(make_cfg(tmp))
    await b._on_unsupported(unsupported_frame("image"))
    check("图片消息给出明确提示", last_content(b.link), lambda t: "只处理文字" in t, last_content(b.link))
    check("图片消息不建会话", b.client.created, lambda c: c == [])

    b2 = make_bridge(make_cfg(tmp))
    await b2._on_unsupported(unsupported_frame("file"))
    check("文件消息也有提示", last_content(b2.link), lambda t: "只处理文字" in t, last_content(b2.link))


async def case_cancel_pending_rejects(tmp):
    b = make_bridge(make_cfg(tmp, confirm_timeout=60))
    b.router.set_session("single:u1", "ses_1", str(tmp))
    await b._dispatch(permission_event("per_1", "ses_1"))
    await b._cancel_pending_for("single:u1")
    replies = b.client.permission_replies
    check("取消待确认时回传拒绝", replies, lambda r: len(r) == 1 and r[0]["decision"] == "reject", str(replies))
    check("取消后清空待确认", "per_1" in PENDING_PERMISSION, lambda v: v is False)


async def case_inflight_notice(tmp):
    cfg = make_cfg(tmp, state_path=str(tmp / "state.json"))
    b = make_bridge(cfg)
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b.router.set_in_flight("single:u1", True)
    await b._notify_interrupted()
    check("重启后通知中断", b.link.markdowns, lambda m: m and "中断" in m[0]["content"], str(b.link.markdowns))
    check("通知后清在途标记", b.router.get("single:u1")["in_flight"], lambda v: v is False)


async def case_state_persist(tmp):
    path = tmp / "state.json"
    r = SessionRouter(str(tmp), {}, [str(tmp)], str(path))
    r.set_session("single:u1", "ses_9", str(tmp))
    r2 = SessionRouter(str(tmp), {}, [str(tmp)], str(path))
    check(
        "会话映射落盘恢复",
        r2.get("single:u1"),
        lambda e: e and e["session_id"] == "ses_9",
        str(r2.get("single:u1")),
    )
    check("恢复后流状态为空", r2.get("single:u1")["stream_id"], lambda v: v is None)


async def case_graceful_stop(tmp):
    cfg = make_cfg(tmp, heartbeat_interval=0)
    b = make_bridge(cfg)
    b.stop_event.set()
    try:
        await asyncio.wait_for(b.run(), timeout=5)
        check("停止位下优雅退出", True, lambda v: v is True)
    except Exception as exc:
        check("停止位下优雅退出", exc, lambda v: False, repr(exc))
    check("退出时断开长连接", b.link.stopped, lambda v: v is True)


async def case_singleton(tmp):
    lock_path = tmp / "unit.lock"
    first, pid = acquire_singleton_lock(lock_path)
    check("单实例锁可获取", first, lambda f: f is not None)
    if first is None:
        return
    try:
        second, holder = acquire_singleton_lock(lock_path, wait_sec=0)
        check("单实例锁二次被拒", second, lambda s: s is None)
        check("锁文件记录进程号", holder, lambda h: h.isdigit(), holder)
    finally:
        first.close()


async def case_stream_disabled_no_partial(tmp):
    b = make_bridge(make_cfg(tmp, stream_enabled=False))
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b.router.set_stream("single:u1", "s1")
    b._begin_stream("single:u1")
    b.push_mode.add("single:u1")
    b.pushed_len["single:u1"] = 0
    b.parts["single:u1"] = {"m:0": {"type": "text", "text": "半截内容"}}

    await b._flush("single:u1", finish=False)
    check("关闭流式后不推中间片段", b.link.markdowns, lambda m: m == [], str(b.link.markdowns))
    check("关闭流式后不发流式更新",
          [x for x in b.link.streams if not x["finish"]], lambda x: x == [], str(b.link.streams))

    b.parts["single:u1"]["m:0"]["text"] += "，全部结束。"
    await b._flush("single:u1", finish=True)
    check("收尾一次推完整正文", b.link.markdowns,
          lambda m: len(m) == 1 and "半截内容" in m[0]["content"] and "全部结束" in m[0]["content"],
          str(b.link.markdowns))


async def case_stream_disabled_final_chunks(tmp):
    text = "\n".join(f"第{i}行内容文字" for i in range(80))
    b = make_bridge(make_cfg(tmp, stream_enabled=False, render_max_chars=200))
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b._begin_stream("single:u1")
    b.push_mode.add("single:u1")
    b.pushed_len["single:u1"] = 0
    b.parts["single:u1"] = {"m:0": {"type": "text", "text": text}}
    await b._flush("single:u1", finish=True)

    check("长文拆成多条推送", len(b.link.markdowns), lambda v: v >= 2, str(len(b.link.markdowns)))
    joined = "".join(m["content"] for m in b.link.markdowns)
    counts = (joined.count("第0行"), joined.count("第79行"))
    check("长文首尾都送达", counts, lambda v: v == (1, 1), str(counts))
    check("分条带序号", b.link.markdowns,
          lambda m: all(f"({i+1}/{len(m)})" in m[i]["content"] for i in range(len(m))),
          str([x["content"][:12] for x in b.link.markdowns]))


async def case_progress_once(tmp):
    now = time.time()
    b = make_bridge(make_cfg(tmp, stream_progress_minutes=5, stream_enabled=False))
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b._begin_stream("single:u1")
    b.running.add("single:u1")
    b.parts["single:u1"] = {"m:0": {"type": "text", "text": "已经写了三百字"}}
    b.task_started["single:u1"] = now - 400
    b.last_event["single:u1"] = now - 400
    await b._progress_once(now)
    check("静默满 3 分钟推一条真实进度", b.link.markdowns,
          lambda m: len(m) == 1 and "仍在处理" in m[0]["content"]
          and "没有新输出" in m[0]["content"], str(b.link.markdowns))
    check("进度里带已产出字数", b.link.markdowns,
          lambda m: "已产出 7 字" in m[0]["content"], str(b.link.markdowns))
    sent = len(b.link.markdowns)
    await b._progress_once(now + 10)
    check("同一间隔内不重复推", len(b.link.markdowns), lambda v: v == sent, str(len(b.link.markdowns)))

    fresh = make_bridge(make_cfg(tmp, stream_progress_minutes=5, stream_enabled=False))
    fresh.router.set_session("single:u2", "ses_2", str(tmp))
    fresh._begin_stream("single:u2")
    fresh.running.add("single:u2")
    fresh.parts["single:u2"] = {"m:0": {"type": "text", "text": "x"}}
    fresh.task_started["single:u2"] = now - 400
    fresh.last_event["single:u2"] = now - 10
    await fresh._progress_once(now)
    check("刚有输出时不打扰", fresh.link.markdowns, lambda m: m == [], str(fresh.link.markdowns))

    off = make_bridge(make_cfg(tmp, stream_progress_minutes=0, stream_enabled=False))
    off.router.set_session("single:u1", "ses_1", str(tmp))
    off._begin_stream("single:u1")
    off.running.add("single:u1")
    off.parts["single:u1"] = {"m:0": {"type": "text", "text": "x"}}
    off.task_started["single:u1"] = now - 4000
    off.last_event["single:u1"] = now - 4000
    await off._progress_once(now)
    check("间隔为 0 不发进度", off.link.markdowns, lambda m: m == [], str(off.link.markdowns))


async def case_push_text_chunks(tmp):
    b = make_bridge(make_cfg(tmp, render_max_chars=200))
    b.router.set_session("single:u1", "ses_1", str(tmp))
    text = "\n".join(f"第{i}行内容文字" for i in range(80))
    await b._push_text("single:u1", text)

    check("推送助手按上限分条", len(b.link.markdowns), lambda v: v >= 2, str(len(b.link.markdowns)))
    joined = "".join(m["content"] for m in b.link.markdowns)
    counts = (joined.count("第0行"), joined.count("第79行"))
    check("分条首尾都送达", counts, lambda v: v == (1, 1), str(counts))
    check("分条带序号", b.link.markdowns,
          lambda m: all(f"({i+1}/{len(m)})" in m[i]["content"] for i in range(len(m))),
          str([x["content"][:12] for x in b.link.markdowns]))

    sent = len(b.link.markdowns)
    await b._push_text("single:u1", "")
    check("空文本不产生消息", len(b.link.markdowns), lambda v: v == sent, str(len(b.link.markdowns)))


async def case_finish_push_not_truncated(tmp):
    b = make_bridge(make_cfg(tmp, render_max_chars=200))
    b.router.set_session("single:u1", "ses_1", str(tmp))
    b._begin_stream("single:u1")
    b.push_mode.add("single:u1")
    b.pushed_len["single:u1"] = 0
    b.parts["single:u1"] = {"m:0": {"type": "text", "text": "\n".join(f"第{i}行" for i in range(60))}}
    await b._finish_stream("single:u1", "收尾备注")

    joined = "".join(m["content"] for m in b.link.markdowns)
    check("收尾长文分条推完", len(b.link.markdowns), lambda v: v >= 2, str(len(b.link.markdowns)))
    check("收尾不丢末尾内容", joined.count("第59行"), lambda v: v == 1, str(joined.count("第59行")))
    check("收尾带上备注", "收尾备注" in joined, lambda v: v is True, joined[-40:])


async def case_end_stream_cleans_state(tmp):
    b = make_bridge(make_cfg(tmp))
    key = "single:u1"
    b.router.set_session(key, "ses_1", str(tmp))
    b._begin_stream(key)
    b._throttle_for(key)
    b.send_times[key] = [time.time()]
    b._frame_by_key[key] = {}
    b.parts[key] = {"m:0": {"type": "text", "text": "x"}}
    b.running.add(key)
    b._end_stream(key)

    check("结束清空节流器", key in b.throttles, lambda v: v is False, str(list(b.throttles)))
    check("结束清空发送时间", key in b.send_times, lambda v: v is False, str(list(b.send_times)))
    check("结束清空帧缓存", key in b._frame_by_key, lambda v: v is False, str(list(b._frame_by_key)))
    check("结束清空部件", key in b.parts, lambda v: v is False, str(list(b.parts)))


def run_all():
    from log_redirect import log_redirect

    cases = [
        case_reply_finish,
        case_group_mention,
        case_group_dir_denied,
        case_permission_timeout,
        case_card_decision,
        case_part_aggregation,
        case_session_error,
        case_idle_empty,
        case_long_output_chunks,
        case_cmd_new,
        case_session_rebuild,
        case_no_proxy,
        case_wecom_direct,
        case_create_session_unwrap,
        case_chat_and_error_log,
        case_card_failure_keeps_pending,
        case_interrupted_finishes,
        case_commands_finish_stream,
        case_owner_whitelist,
        case_rate_limit,
        case_status_text,
        case_quick_menu,
        case_menu_always,
        case_push_handover,
        case_watchdog_finish,
        case_unsupported_message,
        case_cancel_pending_rejects,
        case_inflight_notice,
        case_state_persist,
        case_graceful_stop,
        case_singleton,
        case_stream_disabled_no_partial,
        case_stream_disabled_final_chunks,
        case_progress_once,
        case_push_text_chunks,
        case_finish_push_not_truncated,
        case_end_stream_cleans_state,
    ]
    with log_redirect():
        for case in cases:
            print(f"--- {case.__name__}")
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                asyncio.run(case(tmp))
    for req_id, pending in list(PENDING_PERMISSION.items()):
        timer = pending.get("timer")
        if timer:
            timer.cancel()
        PENDING_PERMISSION.pop(req_id, None)
    print()
    if FAILURES:
        print(f"bridge 用例失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("bridge 用例全部通过")
    return 0

if __name__ == "__main__":
    sys.exit(run_all())

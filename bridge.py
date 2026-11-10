import asyncio
import fcntl
import json
import os
import signal
import time
from pathlib import Path

from src import activity, config, logger, quick_menu, renderer, throttle, weather
from src.opencode_client import (
    OpenCodeClient,
    delta_text,
    ended_text,
    error_text,
    is_error,
    is_execution_started,
    is_idle,
    is_interrupted,
    is_permission_asked,
    is_succeeded,
    is_text_delta,
    is_text_ended,
    is_text_started,
    permission_of,
    session_id_of,
    text_ref,
)
from src.session_router import DirNotAllowed, SessionRouter
from src.wecom_link import WeComLink

PROJECT_ROOT = Path(__file__).resolve().parent
LOCK_PATH = PROJECT_ROOT / ".bridge.lock"
# 重启瞬间新旧实例会重叠，等旧实例释放锁再放弃，避免两边都退出。
LOCK_WAIT_SEC = 15
GC_INTERVAL_SEC = 600

# 企业微信流式消息硬上限是首次发送后 10 分钟，这里提前到 540 秒交接，留 60 秒余量。
STREAM_TTL_SEC = 540
STREAM_GUARD_INTERVAL_SEC = 30
# 运行中任务长时间没有任何事件就收尾，避免气泡挂到流式超时。
TASK_WATCHDOG_SEC = 1200
# 任务静默超过 3 分钟就先发一条真实进度，之后按配置的间隔复述，避免用户干等。
PROGRESS_FIRST_SEC = 180
# 菜单编号 1、2 是企业微信机器人按钮菜单的系统快捷键，任何时候都按菜单执行，
# 不受时间窗限制，也不再退化成发给模型的普通指令。
SEND_RETRY_ATTEMPTS = 2
SEND_RETRY_BASE_DELAY = 1.0
# 长文分条推送时，两条之间的间隔，避免瞬时连发被平台限流。
PUSH_CHUNK_GAP_SEC = 0.3
UNSUPPORTED_TYPES = ("message.image", "message.file", "message.voice", "message.mixed")

WELCOME = (
    "已连接电脑上的 OpenCode。\n"
    "输入 help 或 ? 打开快捷菜单；\n"
    "直接发文字就是指令；/proj /绝对路径 切换项目；/new 清空会话；"
    "/stop 中断；/status 查看状态。"
)
PENDING_PERMISSION = {}
DECISION_BY_KEY = {"allow": "once", "deny": "reject"}


def parse_command(text):
    body = text.strip()
    if not body.startswith("/"):
        return None, body
    head, _, rest = body.partition(" ")
    return head.strip().lower().lstrip("/"), rest.strip()


def describe(entry, router, key):
    if not entry:
        return "当前没有会话，发一句话就会自动新建。"
    return (
        f"目录：{entry['directory']}\n"
        f"会话：{entry['session_id'] or '未建立'}\n"
        f"映射键：{key}"
    )


def _chat_of(key):
    kind, _, ident = key.partition(":")
    if kind == "group":
        return ident, 2
    return ident, 1


def _is_session_gone(exc):
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 404
    return False


def heartbeat_targets(router):
    targets = []
    for key in router.map:
        chatid, chat_type = _chat_of(key)
        targets.append((key, chatid, chat_type))
    return targets


def menu_with_commands():
    return (
        quick_menu.menu_text()
        + "\n\n**控制命令**\n"
        "`/proj /绝对路径` 切换项目目录\n"
        "`/new` 清空当前会话\n"
        "`/stop` 中断正在跑的任务\n"
        "`/status` 查看当前目录与会话"
    )


def heartbeat_text(now=None):
    """心跳内容：时间戳加菜单。间隔由 HEARTBEAT_INTERVAL_SEC 控制，当前 5 小时一次。"""
    if now is None:
        now = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    return f"{stamp}\n\n{menu_with_commands()}"


def heartbeat_interval(cfg):
    """心跳间隔秒数，只看配置。返回 0 表示关闭心跳。

    这里刻意不引入任何按运行状态压缩间隔的逻辑：菜单是静态内容，
    压缩间隔只会变成刷屏。
    """
    return max(0, int((cfg or {}).get("heartbeat_interval") or 0))


def message_meta(frame):
    """从企微回调里取出会话标识与消息类型，非文本消息也要用。"""
    body = frame.get("body") or {}
    chat_type = body.get("chattype") or "single"
    chatid = body.get("chatid") or (body.get("from") or {}).get("userid") or ""
    userid = (body.get("from") or {}).get("userid") or ""
    return chat_type, chatid, userid


def acquire_singleton_lock(path=LOCK_PATH, wait_sec=LOCK_WAIT_SEC):
    """抢单实例锁。launchd 重启时会短暂与旧实例重叠，先等旧实例退干净再判定失败。"""
    deadline = time.monotonic() + max(wait_sec, 0)
    while True:
        handle = open(path, "a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            try:
                holder = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                holder = ""
            if time.monotonic() >= deadline:
                return None, holder
            time.sleep(0.5)
            continue
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        return handle, str(os.getpid())


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = OpenCodeClient(cfg["base_url"], config.auth_headers(cfg))
        self.link = WeComLink(cfg["bot_id"], cfg["bot_secret"], cfg.get("robot_name", ""))
        self.router = SessionRouter(
            cfg["default_dir"],
            cfg["group_dirs"],
            cfg["allowed_dirs"],
            cfg.get("state_path"),
        )
        self.throttles = {}
        self.parts = {}
        self.running = set()
        self.send_times = {}
        self.queue = asyncio.Queue()
        self.stop_event = asyncio.Event()
        self._frame_by_key = {}
        self._stream_seq = 0
        self.stream_started = {}
        self.push_mode = set()
        self.pushed_len = {}
        self.progress_at = {}
        self.task_started = {}
        self.last_event = {}
        self.menu_shown = {}
        self._bg = set()
        self.metrics = {"recv": 0, "reply": 0, "push": 0, "fail": 0, "tasks": 0}
        self.link.client.on("message.text", self._on_text)
        for event in UNSUPPORTED_TYPES:
            self.link.client.on(event, self._on_unsupported)
        self.link.client.on("event.enter_chat", self._on_enter)
        self.link.client.on("event.template_card_event", self._on_card_event)

    def _new_stream_id(self):
        self._stream_seq += 1
        return f"stream-{self._stream_seq}"

    def _throttle_for(self, key):
        if key not in self.throttles:
            self.throttles[key] = throttle.Throttle(
                self.cfg["stream_min_chars"], self.cfg["stream_min_interval"]
            )
        return self.throttles[key]

    async def _retry(self, factory, attempts=SEND_RETRY_ATTEMPTS):
        last = None
        for attempt in range(attempts):
            try:
                return await factory()
            except Exception as exc:
                last = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(SEND_RETRY_BASE_DELAY * (attempt + 1))
        raise last

    async def _on_enter(self, frame):
        try:
            await self.link.reply_welcome(frame, WELCOME)
            userid = (frame.get("body") or {}).get("from", {}).get("userid") or ""
            logger.chat("发", f"single:{userid}", "welcome", WELCOME)
        except Exception as exc:
            logger.error(f"欢迎语发送失败: {exc}")

    async def _on_unsupported(self, frame):
        body = frame.get("body") or {}
        msgtype = body.get("msgtype") or "unknown"
        chat_type, chatid, userid = message_meta(frame)
        key = self.router.key_for(chat_type, chatid, userid)
        logger.info(f"收到非文本消息 msgtype={msgtype} key={key}")
        logger.chat("收", key, msgtype, "")
        logger.info(f"非文本消息原始帧待采样: {json.dumps(frame, ensure_ascii=False)[:1200]}")
        if self.cfg.get("owner_ids") and userid not in self.cfg["owner_ids"]:
            logger.warn(f"发送者不在白名单，拒绝 userid={userid}")
            return
        if chat_type == "group":
            mention = body.get("mentioned_list") or []
            if mention and self.link.robot_name and self.link.robot_name not in mention:
                return
        await self._reply(
            frame,
            key,
            "目前只处理文字消息，图片、文件、语音、图文混排还没支持，请把内容转成文字发我。",
        )

    async def _on_text(self, frame):
        chat_type, chatid, userid = message_meta(frame)
        content = ((frame.get("body") or {}).get("text") or {}).get("content") or ""
        key = self.router.key_for(chat_type, chatid, userid)

        if self.cfg.get("owner_ids") and userid not in self.cfg["owner_ids"]:
            logger.warn(f"发送者不在白名单，拒绝 userid={userid} key={key} len={len(content)}")
            logger.chat("收", key, "blocked", f"len={len(content)}")
            await self._reply(frame, key, "无权使用这个机器人。")
            return

        self.metrics["recv"] += 1
        logger.info(f"收到消息 key={key} len={len(content)}")
        logger.chat("收", key, "text", content)

        if chat_type == "group":
            mention = (frame.get("body") or {}).get("mentioned_list") or []
            if mention and self.link.robot_name and self.link.robot_name not in mention:
                return

        # 企业微信机器人自带的按钮菜单点击后到达的就是 1、2 这样的文本，
        # 与手打无法区分。这类编号属于系统快捷键，任何时刻都先按菜单执行，
        # 不进模型，也不受任务锁影响。
        text = renderer.strip_at_mention(content, self.link.robot_name)

        try:
            if quick_menu.is_trigger(text):
                self.menu_shown[key] = time.time()
                await self._reply(frame, key, self._menu_text())
                return
            selection = quick_menu.selection_of(text)
            if selection is not None:
                await self._run_menu_item(selection, frame, key)
                return
            async with self.router.lock_for(key):
                await self._handle_text(frame, key, chat_type, chatid, content)
        except Exception as exc:
            logger.error(f"消息处理失败 {type(exc).__name__}: {exc}")
            entry = self.router.get(key)
            if entry and entry["stream_id"]:
                await self._finish_with_error(key, f"处理失败：{exc}")
                return
            try:
                await self._reply(frame, key, f"处理失败：{exc}")
            except Exception as reply_exc:
                logger.error(f"失败提示也发送失败: {reply_exc}")

    async def _handle_text(self, frame, key, chat_type, chatid, content):
        text = renderer.strip_at_mention(content, self.link.robot_name)

        cmd, rest = parse_command(text)

        if cmd == "status":
            await self._reply(frame, key, await self._status_text(key))
            return
        if cmd == "proj":
            await self._finish_stream(key, "会话已重置，正在切换目录…")
            try:
                real = self.router.switch_dir(key, rest)
                self.parts.pop(key, None)
                await self._reply(frame, key, f"已切换到 {real}")
            except DirNotAllowed as exc:
                await self._reply(frame, key, f"拒绝：{exc}")
            return
        if cmd == "new":
            await self._cmd_new(frame, key)
            return
        if cmd == "stop":
            entry = self.router.get(key)
            if entry and entry["session_id"]:
                try:
                    await self.client.interrupt(entry["session_id"])
                except Exception as exc:
                    logger.warn(f"中断请求失败: {exc}")
                await self._finish_stream(key, "已中断当前任务。")
                await self._reply(frame, key, "已中断当前任务。")
            else:
                await self._reply(frame, key, "没有正在运行的任务。")
            return

        if not text.strip():
            await self._reply(frame, key, "收到空消息。")
            return

        try:
            directory = self.router.dir_for(chat_type, chatid)
        except DirNotAllowed as exc:
            await self._reply(frame, key, f"拒绝：{exc}")
            return

        entry = self.router.get(key)
        if not entry or not entry["session_id"]:
            try:
                session = await self.client.create_session(directory)
            except Exception as exc:
                logger.error(f"创建会话失败 key={key}: {type(exc).__name__}: {exc}")
                await self._reply(frame, key, f"创建会话失败：{exc}")
                return
            entry = self.router.set_session(key, session["id"], directory)
            logger.info(f"新建会话 {session['id']} @ {directory}")

        entry["directory"] = directory
        stream_id = self._new_stream_id()
        entry["stream_id"] = stream_id
        self._frame_by_key[key] = frame
        self.router.touch(key)
        self.parts[key] = {}
        self._throttle_for(key).reset()
        await self._cancel_pending_for(key)
        self._begin_stream(key)
        self.router.set_in_flight(key, True)

        if self.cfg.get("stream_enabled", True):
            await self._safe_stream(frame, stream_id, "已收到，正在处理…", finish=False)
            logger.chat("发", key, "stream", "已收到，正在处理…")
        else:
            # 关闭流式后不开流式消息，进度与结果都走独立消息，避免碎片刷屏。
            entry["stream_id"] = None
            self.push_mode.add(key)
            self.pushed_len[key] = 0
            await self._send_push(key, "已收到，正在处理…")
        try:
            await self.client.send_prompt(entry["session_id"], text)
        except Exception as exc:
            logger.error(f"prompt 发送失败 key={key}: {type(exc).__name__}: {exc}")
            if _is_session_gone(exc) and await self._rebuild_and_retry(entry, directory, text):
                return
            body = renderer.render(
                f"任务失败：{exc}\n\n完整原文见电脑端 logs/bridge.log",
                self.cfg["render_max_chars"],
            )
            logger.chat("发", key, "error", body)
            try:
                await self._safe_stream(frame, stream_id, body, finish=True)
            except Exception as send_exc:
                logger.warn(f"失败提示推送异常: {send_exc}")
            self._end_stream(key)

    async def _rebuild_and_retry(self, entry, directory, text):
        entry["session_id"] = None
        try:
            session = await self.client.create_session(directory)
        except Exception as exc:
            logger.error(f"会话重建失败: {exc}")
            return False
        entry["session_id"] = session["id"]
        logger.info(f"会话失效已重建 {session['id']} @ {directory}")
        try:
            await self.client.send_prompt(entry["session_id"], text)
            return True
        except Exception as exc:
            logger.error(f"重建后重发失败: {exc}")
            entry["session_id"] = None
            return False

    async def _cmd_new(self, frame, key):
        await self._finish_stream(key, "会话已重置。")
        entry = self.router.get(key)
        stale = None
        if entry:
            stale = entry["session_id"]
            entry["session_id"] = None
            entry["stream_id"] = None
        self.parts.pop(key, None)
        self._frame_by_key.pop(key, None)
        self.running.discard(key)
        await self._cancel_pending_for(key)
        self.router.set_in_flight(key, False)
        self.router.save()
        if stale:
            # 必须留引用，否则任务可能在跑一半被 GC 掉。
            task = asyncio.create_task(self._delete_session(stale))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
        await self._reply(frame, key, "已清空会话，下一条消息会新建。")

    async def _delete_session(self, session_id):
        try:
            await self.client.delete_session(session_id)
        except Exception as exc:
            logger.warn(f"删除远端会话 {session_id} 失败: {exc}")

    async def _status_text(self, key):
        entry = self.router.get(key)
        lines = ["**当前状态**", describe(entry, self.router, key)]
        lines.append(f"任务：{'运行中' if key in self.running else '空闲'}")
        if key in self.push_mode:
            lines.append("推送模式：内容已转为独立消息推送")
        pending = [rid for rid, p in PENDING_PERMISSION.items() if p.get("key") == key]
        lines.append(f"待确认权限：{len(pending)} 条")
        try:
            model = await self.client.default_model()
        except Exception as exc:
            model = f"查询失败 {type(exc).__name__}"
        lines.append(f"默认模型：{model}")
        return "\n".join(lines)

    def _begin_stream(self, key):
        now = time.time()
        self.stream_started[key] = now
        self.task_started[key] = now
        self.last_event[key] = now
        self.push_mode.discard(key)
        self.pushed_len.pop(key, None)
        self.progress_at[key] = 0

    def _mark_event(self, key):
        self.last_event[key] = time.time()

    def _end_stream(self, key):
        self.router.clear_stream(key)
        self.parts.pop(key, None)
        self.running.discard(key)
        self.stream_started.pop(key, None)
        self.push_mode.discard(key)
        self.pushed_len.pop(key, None)
        self.progress_at.pop(key, None)
        self.task_started.pop(key, None)
        self.last_event.pop(key, None)
        self.throttles.pop(key, None)
        self.send_times.pop(key, None)
        self._frame_by_key.pop(key, None)
        self.router.set_in_flight(key, False)

    async def _safe_stream(self, frame, stream_id, body, finish):
        await self._retry(lambda: self.link.reply_stream(frame, stream_id, body, finish))

    async def _send_push(self, key, body):
        if not body:
            return
        chatid, chat_type = _chat_of(key)
        logger.chat("发", key, "push", body)
        try:
            await self._retry(lambda: self.link.send_markdown(chatid, chat_type, body))
            self.metrics["push"] += 1
        except Exception as exc:
            self.metrics["fail"] += 1
            logger.error(f"主动推送失败 key={key}: {exc}")

    async def _push_text(self, key, text):
        """按渲染上限把文本切块推送。长内容不截断，分条发完。"""
        if text is None or not str(text).strip():
            return
        body = renderer.render_raw(text)
        if not body:
            return
        pieces = throttle.chunk_text(body, self.cfg["render_max_chars"])
        total = len(pieces)
        for idx, piece in enumerate(pieces, start=1):
            if idx > 1:
                # 分条之间留一点间隔，避免瞬时连发触发平台限流。
                await asyncio.sleep(PUSH_CHUNK_GAP_SEC)
            prefix = f"**({idx}/{total})**\n" if total > 1 else ""
            await self._send_push(key, prefix + piece)

    async def _finish_stream(self, key, note=""):
        entry = self.router.get(key)
        if not entry:
            self._end_stream(key)
            return
        text = self._plain(key)
        pushing = key in self.push_mode or not entry["stream_id"]
        if pushing:
            remaining = text[self.pushed_len.get(key, 0):]
            merged = f"{remaining}\n\n{note}".strip() if remaining else note
            if merged:
                await self._push_text(key, merged)
        else:
            body = renderer.render(
                f"{text}\n\n{note}" if text else note, self.cfg["render_max_chars"]
            )
            logger.chat("发", key, "finish", body)
            try:
                await self._safe_stream(
                    self._frame_by_key.get(key), entry["stream_id"], body, finish=True
                )
            except Exception as exc:
                logger.warn(f"收尾推送失败: {exc}")
        self._end_stream(key)

    def _allow_send(self, key, limit=25, window=60):
        now = time.time()
        stamps = [t for t in self.send_times.get(key, []) if now - t < window]
        if len(stamps) >= limit:
            self.send_times[key] = stamps
            logger.warn(f"刷新过频，本轮跳过一次推送 key={key}（{window} 秒内已 {len(stamps)} 条）")
            return False
        stamps.append(now)
        self.send_times[key] = stamps
        return True

    async def _reply(self, frame, key, text):
        body = renderer.render(text, self.cfg["render_max_chars"])
        pieces = throttle.chunk_text(body, 1800)
        if not pieces:
            pieces = [body]
        for idx, piece in enumerate(pieces):
            logger.chat("发", key, "reply", piece)
            try:
                await self._safe_stream(
                    frame, self._new_stream_id(), piece, finish=idx == len(pieces) - 1
                )
                self.metrics["reply"] += 1
            except Exception as exc:
                self.metrics["fail"] += 1
                logger.error(f"回复发送失败 key={key}: {exc}")
                return

    def _menu_text(self):
        return menu_with_commands()

    async def _run_menu_item(self, index, frame, key):
        item = quick_menu.item_at(index)
        if not item:
            await self._reply(frame, key, "没有这个序号。")
            return
        logger.chat("收", key, "menu", f"{index}. {item['title']}")
        if item["key"] == "activity":
            await self._reply(frame, key, await self._activity_text(key))
        elif item["key"] == "weather":
            await self._reply(frame, key, await self._weather_text())

    async def _activity_text(self, key):
        entry = self.router.get(key)
        current = entry["session_id"] if entry else None
        try:
            sessions = await self.client.list_sessions(limit=30, parent_id="null")
            active = await self.client.active_sessions()
        except Exception as exc:
            logger.error(f"读取会话列表失败: {type(exc).__name__}: {exc}")
            return f"读取 OpenCode 会话失败：{exc}"
        return activity.compose(sessions, active, exclude_id=current)

    async def _weather_text(self):
        city = self.cfg.get("weather_city") or ""
        fallback = bool(self.cfg.get("weather_ip_fallback"))
        try:
            return await asyncio.to_thread(weather.fetch, city, None, fallback)
        except Exception as exc:
            logger.error(f"获取天气失败: {type(exc).__name__}: {exc}")
            return f"获取天气失败：{exc}"

    async def _event_loop(self):
        while not self.stop_event.is_set():
            ev = await self.queue.get()
            try:
                await self._dispatch(ev)
            except Exception as exc:
                logger.error(f"事件处理失败 {type(exc).__name__}: {exc}")

    async def _gc_loop(self):
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=GC_INTERVAL_SEC)
                return
            except asyncio.TimeoutError:
                pass
            stale = self.router.gc()
            if stale:
                logger.info(f"清理空闲会话 {len(stale)} 个")
            m = self.metrics
            logger.info(
                f"运行指标 收到={m['recv']} 回复={m['reply']} 推送={m['push']} "
                f"失败={m['fail']} 完成任务={m['tasks']} 活跃会话={len(self.router.map)}"
            )

    async def _stream_guard_loop(self):
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=STREAM_GUARD_INTERVAL_SEC
                )
                return
            except asyncio.TimeoutError:
                pass
            await self._guard_once()

    async def _guard_once(self):
        now = time.time()
        if self.cfg.get("stream_enabled", True):
            for key in list(self.stream_started):
                started = self.stream_started.get(key, now)
                if now - started < STREAM_TTL_SEC:
                    continue
                logger.warn(f"流式接近 10 分钟上限，转为主动推送 key={key}")
                await self._handover_stream(key)
        await self._progress_once(now)
        for key in list(self.running):
            last = self.last_event.get(key, now)
            if now - last < TASK_WATCHDOG_SEC:
                continue
            logger.warn(f"任务长时间无输出，强制收尾 key={key}（已 {int(now - last)} 秒无事件）")
            await self._finish_stream(key, "长时间没有输出，已结束本次推流。可发 /status 查看。")

    def _progress_text(self, key, elapsed, quiet):
        """按真实数据描述当前状态，不猜测进度。"""
        lines = [f"仍在处理，已用时 {max(1, int(elapsed // 60))} 分钟。"]
        if quiet >= 60:
            lines.append(f"最近 {int(quiet // 60)} 分钟没有新输出。")
        chars = len(self._plain(key))
        lines.append(f"已产出 {chars} 字。")
        pending = [rid for rid, p in PENDING_PERMISSION.items() if p.get("key") == key]
        if pending:
            lines.append(f"有 {len(pending)} 条权限请求在等你确认。")
        lines.append("任务结束会自动推送结果，发 /status 可查当前状态。")
        return "\n".join(lines)

    async def _progress_once(self, now):
        """长任务静默时定期报一次真实状态。

        首次在任务静默满 3 分钟时推送，之后按 stream_progress_minutes 复述。
        配置为 0 表示不发进度。
        """
        minutes = self.cfg.get("stream_progress_minutes") or 0
        if minutes <= 0:
            return
        repeat = minutes * 60
        for key in list(self.running):
            started = self.task_started.get(key) or self.stream_started.get(key) or now
            last = self.last_event.get(key, started)
            elapsed = now - started
            quiet = now - last
            if elapsed < PROGRESS_FIRST_SEC or quiet < PROGRESS_FIRST_SEC:
                continue
            last_notice = self.progress_at.get(key) or 0
            if last_notice and now - last_notice < repeat:
                continue
            self.progress_at[key] = now
            await self._send_push(key, self._progress_text(key, elapsed, quiet))

    async def _handover_stream(self, key):
        entry = self.router.get(key)
        if not entry:
            self._end_stream(key)
            return
        text = self._plain(key)
        note = "内容较长，原流式消息到此收尾，正文改用独立消息继续推送。"
        # 只把提示写进流式消息。原实现把整段正文塞进去再用 render 截断，
        # 同时把已推长度记成全文长度，被截掉的中间段就再也发不出来了。
        try:
            await self._safe_stream(
                self._frame_by_key.get(key), entry["stream_id"], note, finish=True
            )
        except Exception as exc:
            logger.warn(f"流式交接失败: {exc}")
        self.router.clear_stream(key)
        self.push_mode.add(key)
        self.stream_started.pop(key, None)
        self.pushed_len[key] = 0
        if text:
            await self._push_text(key, text)
        self.pushed_len[key] = len(text)

    @staticmethod
    async def _heartbeat_once(stub):
        for key, chatid, chat_type in heartbeat_targets(stub.router):
            try:
                await stub.link.send_markdown(chatid, chat_type, heartbeat_text())
                shown = getattr(stub, "menu_shown", None)
                if shown is not None:
                    shown[key] = time.time()
            except Exception as exc:
                logger.warn(f"心跳推送失败 {chatid}: {exc}")

    @staticmethod
    async def _heartbeat_loop(stub):
        base = heartbeat_interval(stub.cfg)
        if base <= 0:
            return
        stop = getattr(stub, "stop_event", None)
        while stop is None or not stop.is_set():
            # 间隔只由配置决定。原实现在任务运行中把间隔压到 60 秒，
            # 会让菜单变成每分钟刷一次，与用户设定的节奏冲突，已去掉。
            interval = base
            if stop is not None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval)
            await Bridge._heartbeat_once(stub)

    async def _notify_interrupted(self):
        for key, entry in list(self.router.map.items()):
            if not entry.get("in_flight"):
                continue
            self.router.set_in_flight(key, False)
            chatid, chat_type = _chat_of(key)
            notice = "上次任务在机器人重启中中断了，请重新发一次。"
            logger.chat("发", key, "notice", notice)
            try:
                await self.link.send_markdown(chatid, chat_type, notice)
            except Exception as exc:
                logger.warn(f"重启中断通知发送失败 key={key}: {exc}")

    async def _dispatch(self, ev):
        if is_text_started(ev):
            key = self._key_by_session(session_id_of(ev))
            if key is None:
                return
            self._mark_event(key)
            self.parts.setdefault(key, {})[text_ref(ev)] = {"type": "text", "text": ""}
        elif is_text_delta(ev):
            key = self._key_by_session(session_id_of(ev))
            if key is None:
                return
            delta = delta_text(ev)
            if not delta:
                return
            self._mark_event(key)
            slots = self.parts.setdefault(key, {})
            slot = slots.setdefault(text_ref(ev), {"type": "text", "text": ""})
            slot["text"] += delta
            await self._flush(key, finish=False)
        elif is_text_ended(ev):
            key = self._key_by_session(session_id_of(ev))
            if key is None:
                return
            full = ended_text(ev)
            if full is None:
                return
            self._mark_event(key)
            slots = self.parts.setdefault(key, {})
            slots[text_ref(ev)] = {"type": "text", "text": full}
            await self._flush(key, finish=False)
        elif is_execution_started(ev):
            key = self._key_by_session(session_id_of(ev))
            if key is None:
                return
            self._mark_event(key)
            self.running.add(key)
        elif is_interrupted(ev):
            key = self._key_by_session(session_id_of(ev))
            if key is None:
                return
            logger.info(f"任务被中断 key={key}")
            await self._finish_stream(key, "任务已中断。")
        elif is_idle(ev) or is_succeeded(ev):
            key = self._key_by_session(session_id_of(ev))
            if key is None:
                return
            self.metrics["tasks"] += 1
            await self._flush(key, finish=True)
            self._end_stream(key)
        elif is_error(ev):
            key = self._key_by_session(session_id_of(ev))
            if key is None:
                return
            message = error_text(ev) or "任务出错"
            logger.error(
                f"会话出错 key={key}: {message} | 原始事件: "
                f"{json.dumps(ev, ensure_ascii=False)[:1500]}"
            )
            await self._finish_with_error(key, message)
        elif is_permission_asked(ev):
            await self._handle_permission(ev)

    def _plain(self, key):
        slots = self.parts.get(key) or {}
        return "".join(
            slot["text"] for slot in slots.values() if slot.get("type") == "text"
        )

    async def _flush(self, key, finish=False):
        entry = self.router.get(key)
        if not entry:
            return
        text = self._plain(key)
        pushing = key in self.push_mode or not entry["stream_id"]

        if not finish:
            if not self.cfg.get("stream_enabled", True):
                return
            th = self._throttle_for(key)
            if not th.ready(len(text)):
                return
            th.mark(len(text))
            if not self._allow_send(key):
                return
            if pushing:
                delta = text[self.pushed_len.get(key, 0):]
                if not delta:
                    return
                await self._send_push(key, renderer.render(delta, self.cfg["render_max_chars"]))
                self.pushed_len[key] = len(text)
                return
            body = renderer.render(text, self.cfg["render_max_chars"])
            try:
                await self._safe_stream(
                    self._frame_by_key.get(key), entry["stream_id"], body, finish=False
                )
                logger.chat("发", key, "stream", body)
            except Exception as exc:
                logger.warn(f"流式推送失败: {exc}")
            return

        if pushing:
            remaining = text[self.pushed_len.get(key, 0):]
            if remaining:
                await self._push_text(key, remaining)
            else:
                await self._send_push(key, renderer.render("任务已完成。", self.cfg["render_max_chars"]))
            return

        full = renderer.render_raw(text)
        if not full:
            full = "（无输出）"
        pieces = throttle.chunk_text(full, self.cfg["render_max_chars"])
        head, rest = pieces[0], pieces[1:]
        try:
            await self._safe_stream(
                self._frame_by_key.get(key), entry["stream_id"], head, finish=True
            )
            logger.chat("发", key, "finish", head)
        except Exception as exc:
            logger.warn(f"收尾推送失败: {exc}")
            return
        if rest:
            total = len(rest) + 1
            for idx, piece in enumerate(rest, start=2):
                await self._send_push(key, f"**({idx}/{total})**\n{piece}")

    async def _finish_with_error(self, key, message):
        entry = self.router.get(key)
        if not entry or not entry["stream_id"]:
            self._end_stream(key)
            return
        text = self._plain(key)
        body = renderer.render(
            f"{text}\n\n**任务中断**\n{message}\n\n完整原文见电脑端 logs/bridge.log"
            if text
            else f"**任务中断**\n{message}\n\n完整原文见电脑端 logs/bridge.log",
            self.cfg["render_max_chars"],
        )
        logger.chat("发", key, "error", body)
        try:
            await self._safe_stream(
                self._frame_by_key.get(key), entry["stream_id"], body, finish=True
            )
        except Exception as exc:
            logger.warn(f"中断提示推送失败: {exc}")
        await self._cancel_pending_for(key)
        self._end_stream(key)

    def _key_by_session(self, sid):
        if not sid:
            return None
        for key, entry in self.router.map.items():
            if entry["session_id"] == sid:
                return key
        return None

    async def _handle_permission(self, ev):
        req = permission_of(ev)
        req_id = req.get("id")
        sid = req.get("sessionID")
        if not req_id or not sid:
            logger.warn(f"权限事件字段缺失: {ev}")
            return
        key = self._key_by_session(sid)
        if key is None:
            return
        if req_id in PENDING_PERMISSION:
            return
        desc = req.get("message") or req.get("action") or "需要确认"
        resources = req.get("resources") or []
        if isinstance(resources, list) and resources:
            shown = "、".join(str(r) for r in resources[:5])
            if len(resources) > 5:
                shown += f" 等 {len(resources)} 项"
            desc = f"{desc}\n对象：{shown}"
        card = {
            "card_type": "button_interaction",
            "main_title": {"title": "需要确认", "desc": desc[:400]},
            "button_list": [
                {"text": "允许一次", "style": 1, "key": "allow"},
                {"text": "拒绝", "style": 2, "key": "deny"},
            ],
            "task_id": req_id,
        }
        chatid, chat_type = _chat_of(key)
        try:
            await self.link.send_card(chatid, chat_type, card)
        except Exception as exc:
            logger.error(f"确认卡片发送失败: {exc}")
            return
        logger.chat("发", key, "card", desc)
        PENDING_PERMISSION[req_id] = {
            "session_id": sid,
            "request_id": req_id,
            "key": key,
            "timer": None,
        }
        PENDING_PERMISSION[req_id]["timer"] = asyncio.create_task(
            self._permission_timeout(req_id)
        )
        logger.info(f"权限请求 {req_id} 已推送确认卡片")

    async def _permission_timeout(self, req_id):
        await asyncio.sleep(self.cfg["confirm_timeout"])
        pending = PENDING_PERMISSION.pop(req_id, None)
        if not pending:
            return
        logger.warn(f"权限请求 {req_id} 超时未响应，自动拒绝")
        try:
            await self.client.reply_permission(pending["session_id"], req_id, "reject")
        except Exception as exc:
            logger.error(f"超时拒绝回复失败: {exc}")
        entry = self.router.get(pending["key"])
        if entry:
            chatid, chat_type = _chat_of(pending["key"])
            notice = "确认超时，已自动拒绝本次操作。"
            logger.chat("发", pending["key"], "notice", notice)
            try:
                await self.link.send_markdown(chatid, chat_type, notice)
            except Exception as exc:
                logger.warn(f"超时通知发送失败: {exc}")

    async def _on_card_event(self, frame):
        props = frame.get("body") or {}
        event = props.get("event") or {}
        payload = event.get("template_card_event") or {}
        event_key = payload.get("event_key") or event.get("event_key") or ""
        task_id = (
            payload.get("task_id")
            or event.get("task_id")
            or props.get("task_id")
            or ""
        )
        pending = PENDING_PERMISSION.get(task_id)
        if not pending:
            logger.warn(
                f"收到卡片事件但无待确认项: event_key={event_key!r} task_id={task_id!r} | "
                f"原始 body: {json.dumps(props, ensure_ascii=False)[:600]}"
            )
            chatid = props.get("chatid") or (props.get("from") or {}).get("userid") or ""
            chat_type = 2 if (props.get("chattype") or "single") == "group" else 1
            if chatid:
                try:
                    await self.link.send_markdown(
                        chatid, chat_type, "这次点击没有匹配到待确认的权限请求，可能已超时或被处理过。"
                    )
                except Exception as exc:
                    logger.warn(f"点击反馈发送失败: {exc}")
            return
        logger.chat("收", pending["key"], "card_event", event_key)
        decision = DECISION_BY_KEY.get(event_key)
        if decision is None:
            decision = "reject"
            logger.warn(f"未知卡片按键 {event_key}，按拒绝处理")
        timer = pending.get("timer")
        if timer:
            timer.cancel()
        replied = False
        try:
            await self.client.reply_permission(pending["session_id"], task_id, decision)
            replied = True
            logger.info(f"权限请求 {task_id} 已回复 {decision}")
        except Exception as exc:
            logger.error(f"权限回复失败 key={pending['key']} {task_id}: {exc}")
            PENDING_PERMISSION[task_id] = pending
            timer2 = asyncio.create_task(self._permission_timeout(task_id))
            pending["timer"] = timer2
        if replied:
            PENDING_PERMISSION.pop(task_id, None)
            status = "已允许一次" if decision == "once" else "已拒绝"
        else:
            status = "处理失败，请再点一次"
        try:
            await self.link.update_card(
                frame,
                {
                    "card_type": "text_notice",
                    "main_title": {"title": "已处理" if replied else "处理失败", "desc": status},
                    "task_id": task_id,
                },
            )
        except Exception as exc:
            logger.warn(f"卡片状态更新失败: {exc}")

    async def _cancel_pending_for(self, key):
        for req_id, pending in list(PENDING_PERMISSION.items()):
            if pending.get("key") != key:
                continue
            timer = pending.get("timer")
            if timer:
                timer.cancel()
            PENDING_PERMISSION.pop(req_id, None)
            try:
                await self.client.reply_permission(pending["session_id"], req_id, "reject")
                logger.info(f"待确认权限 {req_id} 已随新指令自动拒绝")
            except Exception as exc:
                logger.warn(f"自动拒绝权限 {req_id} 失败: {exc}")

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except (NotImplementedError, RuntimeError):
                pass
        await self.link.start()
        await self._notify_interrupted()
        reader = asyncio.create_task(
            self.client.stream_events(self.queue, self.stop_event)
        )
        consumer = asyncio.create_task(self._event_loop())
        gc_worker = asyncio.create_task(self._gc_loop())
        guard = asyncio.create_task(self._stream_guard_loop())
        heartbeat = asyncio.create_task(self._heartbeat_loop(self))
        workers = (reader, consumer, gc_worker, guard, heartbeat)
        for task in workers:
            task.add_done_callback(self._log_task_error)
        logger.info("bridge 已启动")
        await self.stop_event.wait()
        logger.info("收到停止信号，正在退出")
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.link.stop()
        logger.info("bridge 已退出")

    def _request_stop(self):
        self.stop_event.set()

    def _log_task_error(self, task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"后台任务异常退出 {type(exc).__name__}: {exc}")


def main():
    cfg = config.load()
    logger.info(
        f"配置载入 base={cfg['base_url']} default_dir={cfg['default_dir']} "
        f"允许目录数={len(cfg['allowed_dirs'])}"
    )
    lock, pid = acquire_singleton_lock()
    if lock is None:
        holder = f"，当前持有者进程号 {pid}" if pid else ""
        logger.warn(f"已有一个 bridge 实例在运行{holder}，单连接规则禁止双开，本次正常退出")
        raise SystemExit(0)
    bridge = Bridge(cfg)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        logger.info("bridge 已手动停止")


if __name__ == "__main__":
    main()

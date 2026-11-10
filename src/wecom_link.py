import websockets
from aibot import WSClient, WSClientOptions

from . import logger

_WsConnect = websockets.connect


def _connect_direct(uri, **kwargs):
    kwargs.setdefault("proxy", None)
    return _WsConnect(uri, **kwargs)


def force_direct_connection():
    # 企业微信长连接必须直连。Clash Verge 的系统代理里开了 SOCKS，
    # websockets 默认 proxy=True 会读到 socks5h://127.0.0.1:7897，而 venv 没装 python-socks，
    # 于是每次建连直接抛 ImportError。SDK 内部硬编码调用 websockets.connect 且不传 proxy，只能在这里注入。
    if websockets.connect is not _connect_direct:
        websockets.connect = _connect_direct


class WeComLink:
    def __init__(self, bot_id, secret, robot_name=""):
        force_direct_connection()
        self.robot_name = robot_name
        self.client = WSClient(
            WSClientOptions(
                bot_id=bot_id,
                secret=secret,
                max_reconnect_attempts=-1,
                reconnect_interval=1000,
                heartbeat_interval=30000,
                logger=logger.SdkLogger(),
            )
        )
        self._bind_lifecycle()

    def _bind_lifecycle(self):
        c = self.client

        @c.on("connected")
        def _connected():
            logger.info("WebSocket 已连接")

        @c.on("authenticated")
        def _authed():
            logger.info("订阅成功，开始接收消息")

        @c.on("disconnected")
        def _disc(reason):
            logger.warn(f"连接断开: {reason}")

        @c.on("reconnecting")
        def _reconn(attempt):
            logger.info(f"第 {attempt} 次重连")

        @c.on("error")
        def _err(exc):
            logger.error(f"连接错误: {exc}")

    async def start(self):
        await self.client.connect()

    def stop(self):
        self.client.disconnect()

    def is_connected(self):
        return self.client.is_connected

    def new_stream_id(self):
        from aibot import generate_req_id

        return generate_req_id("stream")

    async def reply_stream(self, frame, stream_id, content, finish=False):
        return await self.client.reply_stream(frame, stream_id, content, finish)

    async def send_markdown(self, chatid, chat_type, content):
        body = {
            "chatid": chatid,
            "chat_type": chat_type,
            "msgtype": "markdown",
            "markdown": {"content": content},
        }
        return await self.client.send_message(chatid, body)

    async def send_card(self, chatid, chat_type, card):
        body = {
            "chatid": chatid,
            "chat_type": chat_type,
            "msgtype": "template_card",
            "template_card": card,
        }
        return await self.client.send_message(chatid, body)

    async def update_card(self, frame, card):
        return await self.client.update_template_card(frame, card)

    async def reply_welcome(self, frame, content):
        body = {"msgtype": "text", "text": {"content": content}}
        return await self.client.reply_welcome(frame, body)
import asyncio
import json

import httpx

from . import logger

API = "/api"

RECONNECT_MIN_SEC = 1
RECONNECT_MAX_SEC = 30

IDLE_EVENTS = {"session.idle"}
ERROR_EVENTS = {"session.error", "session.execution.failed"}
PERMISSION_EVENTS = {"permission.asked"}


class OpenCodeClient:
    def __init__(self, base_url, headers, timeout=60.0):
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.timeout = timeout

    def _client(self, extra=None):
        h = dict(self.headers)
        h.update(extra or {})
        return httpx.AsyncClient(
            base_url=self.base_url, headers=h, timeout=self.timeout, trust_env=False
        )

    async def create_session(self, directory, title=None):
        payload = {"location": {"directory": directory}}
        if title:
            payload["title"] = title
        async with self._client() as c:
            resp = await c.post(f"{API}/session", json=payload)
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("data"), dict):
                return body["data"]
            return body

    async def delete_session(self, session_id):
        async with self._client() as c:
            resp = await c.delete(f"{API}/session/{session_id}")
            resp.raise_for_status()
            return True

    async def send_prompt(self, session_id, text):
        payload = {"text": text}
        async with self._client() as c:
            resp = await c.post(f"{API}/session/{session_id}/prompt", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def interrupt(self, session_id):
        async with self._client() as c:
            resp = await c.post(f"{API}/session/{session_id}/interrupt")
            resp.raise_for_status()
            return True

    async def reply_permission(self, session_id, request_id, decision, message=None):
        payload = {"decision": decision}
        if message:
            payload["message"] = message
        async with self._client() as c:
            resp = await c.post(
                f"{API}/session/{session_id}/permission/{request_id}/reply",
                json=payload,
            )
            resp.raise_for_status()
            return True

    async def default_model(self):
        async with self._client() as c:
            resp = await c.get(f"{API}/model/default")
            resp.raise_for_status()
            body = resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            return f"{data.get('providerID')}/{data.get('id')}"
        return "未解析出默认模型"

    async def list_sessions(self, limit=20, order="desc", parent_id=None):
        params = {"limit": str(limit), "order": order}
        if parent_id is not None:
            params["parentID"] = parent_id
        async with self._client() as c:
            resp = await c.get(f"{API}/session", params=params)
            resp.raise_for_status()
            body = resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, list) else []

    async def active_sessions(self):
        async with self._client() as c:
            resp = await c.get(f"{API}/session/active")
            resp.raise_for_status()
            body = resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            return set(data.keys())
        return set()

    async def _consume_events(self, queue, stop_event):
        headers = dict(self.headers)
        headers["Accept"] = "text/event-stream"
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=None, trust_env=False
        ) as c:
            async with c.stream("GET", f"{API}/event") as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")[:300]
                    raise RuntimeError(f"事件订阅失败 HTTP {resp.status_code}: {body}")
                logger.info("事件订阅已建立")
                async for line in resp.aiter_lines():
                    if stop_event.is_set():
                        return
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        await queue.put(json.loads(raw))
                    except json.JSONDecodeError:
                        logger.warn(f"事件非 JSON 已丢弃: {raw[:150]}")

    async def stream_events(self, queue, stop_event):
        delay = RECONNECT_MIN_SEC
        while not stop_event.is_set():
            try:
                await self._consume_events(queue, stop_event)
                delay = RECONNECT_MIN_SEC
                if stop_event.is_set():
                    return
                logger.warn("事件订阅被服务端关闭，准备重连")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warn(
                    f"事件订阅失败 {type(exc).__name__}: {exc}，{delay} 秒后重连"
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, RECONNECT_MAX_SEC)


def event_type(event):
    return event.get("type") or ""


def is_idle(event):
    return event_type(event) in IDLE_EVENTS


def is_error(event):
    return event_type(event) in ERROR_EVENTS


def error_text(event):
    props = _props(event)
    for key in ("error", "message"):
        value = props.get(key)
        if isinstance(value, dict):
            text = value.get("message") or value.get("name")
            if text:
                return str(text)
        if isinstance(value, str) and value:
            return value
    return ""


def is_permission_asked(event):
    return event_type(event) in PERMISSION_EVENTS


def _props(event):
    payload = event.get("data") or {}
    if not isinstance(payload, dict):
        return {}
    props = payload.get("properties")
    if isinstance(props, dict):
        return props
    return payload


def is_text_started(event):
    return event_type(event) == "session.text.started"


def is_text_delta(event):
    return event_type(event) == "session.text.delta"


def is_text_ended(event):
    return event_type(event) == "session.text.ended"


def is_succeeded(event):
    return event_type(event) == "session.execution.succeeded"


def is_execution_started(event):
    return event_type(event) == "session.execution.started"


def is_interrupted(event):
    return event_type(event) == "session.execution.interrupted"


def text_ref(event):
    props = _props(event)
    message_id = props.get("assistantMessageID") or ""
    ordinal = props.get("ordinal", "")
    return f"{message_id}:{ordinal}"


def delta_text(event):
    props = _props(event)
    value = props.get("delta")
    return value if isinstance(value, str) else ""


def ended_text(event):
    props = _props(event)
    value = props.get("text")
    return value if isinstance(value, str) else None


def session_id_of(event):
    props = _props(event)
    for key in ("sessionID", "sessionId"):
        value = props.get(key)
        if isinstance(value, str):
            return value
    info = props.get("info")
    if isinstance(info, dict) and isinstance(info.get("id"), str):
        return info["id"]
    return ""


def permission_of(event):
    props = _props(event)
    for key in ("request", "permission"):
        value = props.get(key)
        if isinstance(value, dict) and value.get("id"):
            return value
    if props.get("id", "").startswith("per") and props.get("sessionID"):
        return props
    return {}

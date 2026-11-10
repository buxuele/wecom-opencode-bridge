import json
import os
import sys
from datetime import datetime
from pathlib import Path

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

_current = LEVELS.get(os.getenv("BRIDGE_LOG_LEVEL", "INFO").upper(), 20)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
RUN_LOG_NAME = "bridge.log"
CHAT_LOG_NAME = "chat.jsonl"
MAX_BYTES = 5 * 1024 * 1024
CHAT_CONTENT_LIMIT = 2000

_handles = {}


def _stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rotate_if_needed(path, name):
    try:
        if path.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    handle = _handles.pop(name, None)
    if handle:
        handle.close()
    try:
        path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def _write(name, line):
    path = LOG_DIR / name
    if name not in _handles:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _handles[name] = open(path, "a", encoding="utf-8")
        except OSError:
            return
    _rotate_if_needed(path, name)
    handle = _handles.get(name)
    if handle is None:
        # 刚轮转过，句柄被关掉了。这里必须重开，否则触发轮转的那一行会丢。
        try:
            _handles[name] = open(path, "a", encoding="utf-8")
            handle = _handles[name]
        except OSError:
            return
    try:
        handle.write(line + "\n")
        handle.flush()
    except OSError:
        pass


def _emit(level, msg):
    if LEVELS[level] < _current:
        return
    line = f"{_stamp()} [{level}] {msg}"
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    _write(RUN_LOG_NAME, line)


def debug(msg):
    _emit("DEBUG", msg)


def info(msg):
    _emit("INFO", msg)


def warn(msg):
    _emit("WARN", msg)


def error(msg):
    _emit("ERROR", msg)


def chat(direction, key, kind, content):
    text = "" if content is None else str(content)
    truncated = False
    if len(text) > CHAT_CONTENT_LIMIT:
        text = text[:CHAT_CONTENT_LIMIT]
        truncated = True
    record = {
        "ts": _stamp(),
        "dir": direction,
        "key": key,
        "kind": kind,
        "truncated": truncated,
        "content": text,
    }
    _write(CHAT_LOG_NAME, json.dumps(record, ensure_ascii=False))


class SdkLogger:
    def __init__(self):
        self._n = 0

    def _tag(self):
        self._n += 1
        return f"[sdk#{self._n}]"

    def debug(self, message, *args):
        if LEVELS["DEBUG"] < _current:
            return
        _emit("DEBUG", f"{self._tag()} {message}")

    def info(self, message, *args):
        _emit("INFO", f"{self._tag()} {message}")

    def warn(self, message, *args):
        _emit("WARN", f"{self._tag()} {message}")

    def error(self, message, *args):
        _emit("ERROR", f"{self._tag()} {message}")

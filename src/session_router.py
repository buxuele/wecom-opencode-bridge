import asyncio
import json
import time
from pathlib import Path

from . import logger

IDLE_TTL_SEC = 24 * 3600


class DirNotAllowed(Exception):
    pass


def resolve_dir(path, allowed_dirs):
    real = str(Path(path).expanduser().resolve())
    if not allowed_dirs:
        raise DirNotAllowed(
            "未配置目录白名单，拒绝切换目录。请在 .env 的 WECOM_ALLOWED_DIRS 里写允许的目录，JSON 数组格式"
        )
    if real not in allowed_dirs:
        raise DirNotAllowed(
            f"目录不在白名单内: {path}。白名单见 .env 的 WECOM_ALLOWED_DIRS"
        )
    return real


class SessionRouter:
    def __init__(self, default_dir, group_dirs, allowed_dirs, state_path=None):
        self.default_dir = default_dir
        self.group_dirs = group_dirs or {}
        self.allowed_dirs = allowed_dirs or []
        self.map = {}
        self.locks = {}
        self.state_path = Path(state_path) if state_path else None
        if self.state_path:
            self.load()

    def load(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if isinstance(exc, json.JSONDecodeError):
                logger.warn(f"会话状态文件解析失败，忽略: {exc}")
            return
        if not isinstance(data, dict):
            return
        for key, entry in data.items():
            if not isinstance(entry, dict) or not entry.get("directory"):
                continue
            self.map[key] = {
                "session_id": entry.get("session_id"),
                "directory": entry.get("directory"),
                "stream_id": None,
                "last_active": time.time(),
                "in_flight": bool(entry.get("in_flight")),
            }
        if self.map:
            logger.info(f"已恢复 {len(self.map)} 个会话映射")

    def save(self):
        if not self.state_path:
            return
        data = {
            key: {
                "session_id": entry.get("session_id"),
                "directory": entry.get("directory"),
                "in_flight": bool(entry.get("in_flight")),
            }
            for key, entry in self.map.items()
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError as exc:
            logger.warn(f"会话状态写入失败: {exc}")

    def key_for(self, chattype, chatid, userid):
        if chattype == "group":
            return "group:" + str(chatid)
        return "single:" + str(userid)

    def dir_for(self, chattype, chatid):
        if chattype != "group":
            return self.default_dir
        found = self.group_dirs.get(chatid) or self.group_dirs.get(str(chatid))
        if found:
            return found
        raise DirNotAllowed(
            f"群 {chatid} 未配置项目目录，请在 .env 的 WECOM_GROUP_DIRS 中添加"
        )

    def get(self, key):
        return self.map.get(key)

    def set_session(self, key, session_id, directory):
        self.map[key] = {
            "session_id": session_id,
            "directory": directory,
            "stream_id": None,
            "last_active": time.time(),
            "in_flight": False,
        }
        self.save()
        return self.map[key]

    def set_in_flight(self, key, flag):
        entry = self.map.get(key)
        if entry is None or bool(entry.get("in_flight")) == bool(flag):
            return
        entry["in_flight"] = bool(flag)
        self.save()

    def set_stream(self, key, stream_id):
        if key in self.map:
            self.map[key]["stream_id"] = stream_id
            self.map[key]["last_active"] = time.time()

    def clear_stream(self, key):
        if key in self.map:
            self.map[key]["stream_id"] = None

    def touch(self, key):
        if key in self.map:
            self.map[key]["last_active"] = time.time()

    def switch_dir(self, key, new_dir):
        real = resolve_dir(new_dir, self.allowed_dirs)
        entry = self.map.get(key)
        if entry is None:
            self.map[key] = {
                "session_id": None,
                "directory": real,
                "stream_id": None,
                "last_active": time.time(),
                "in_flight": False,
            }
        else:
            entry["directory"] = real
            entry["session_id"] = None
            entry["stream_id"] = None
            entry["last_active"] = time.time()
            entry["in_flight"] = False
        self.save()
        return real

    def lock_for(self, key):
        if key not in self.locks:
            self.locks[key] = asyncio.Lock()
        return self.locks[key]

    def gc(self):
        cutoff = time.time() - IDLE_TTL_SEC
        stale = [k for k, v in self.map.items() if v["last_active"] < cutoff]
        for k in stale:
            del self.map[k]
            self.locks.pop(k, None)
        if stale:
            self.save()
        return stale

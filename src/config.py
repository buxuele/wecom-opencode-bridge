import json
import os
from pathlib import Path

from dotenv import load_dotenv

from . import opencode_service

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(ENV_PATH)

DEFAULT_USERNAME = "opencode"
DEFAULT_MIN_CHARS = 300
DEFAULT_MIN_INTERVAL = 2
DEFAULT_RENDER_MAX = 3000
DEFAULT_HEARTBEAT = 600
DEFAULT_CONFIRM_TIMEOUT = 300
DEFAULT_PROGRESS_MINUTES = 5


class ConfigError(Exception):
    pass


def _require(name):
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"缺少必填配置 {name}，请在 .env 中填写")
    return value


def _json_env(name, default):
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{name} 不是合法 JSON: {exc}") from exc


def _int_env(name, default, minimum=None):
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，当前值 {raw}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} 必须大于等于 {minimum}，当前值 {value}")
    return value


def _bool_env(name, default=False):
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _list_env(name, default=None):
    raw = os.getenv(name)
    fallback = default if default is not None else []
    if not raw or not raw.strip():
        return fallback
    raw = raw.strip()
    if raw.startswith("["):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{name} 不是合法 JSON: {exc}") from exc
        if not isinstance(value, list):
            raise ConfigError(f"{name} 必须是 JSON 数组")
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in raw.split(",") if part.strip()]


def _check_dir(name, path):
    real = Path(path).expanduser()
    if not real.is_absolute():
        raise ConfigError(f"{name} 必须是绝对路径，当前值 {path}")
    if not real.is_dir():
        raise ConfigError(f"{name} 指向的目录不存在: {path}")
    return str(real.resolve())


def load():
    default_dir = _check_dir("WECOM_DEFAULT_DIR", _require("WECOM_DEFAULT_DIR"))
    group_dirs = _json_env("WECOM_GROUP_DIRS", {})
    if not isinstance(group_dirs, dict):
        raise ConfigError("WECOM_GROUP_DIRS 必须是 JSON 对象")
    checked_groups = {}
    for chatid, path in group_dirs.items():
        checked_groups[str(chatid)] = _check_dir(
            f"WECOM_GROUP_DIRS[{chatid}]", path
        )
    allowed = _json_env("WECOM_ALLOWED_DIRS", [])
    if not isinstance(allowed, list):
        raise ConfigError("WECOM_ALLOWED_DIRS 必须是 JSON 数组")
    checked_allowed = [
        _check_dir("WECOM_ALLOWED_DIRS", str(p)) for p in allowed
    ]
    raw_base = (os.getenv("OPENCODE_BASE_URL") or "").strip()
    username = (os.getenv("OPENCODE_USERNAME") or "").strip() or DEFAULT_USERNAME
    password = os.getenv("OPENCODE_PASSWORD") or ""
    try:
        endpoint = opencode_service.resolve(raw_base, username, password)
    except opencode_service.ServiceUnavailable as exc:
        raise ConfigError(str(exc)) from exc
    if not endpoint["auto"]:
        if not endpoint["base_url"]:
            raise ConfigError(
                "OPENCODE_BASE_URL 为空；留空或写 auto 表示自动发现 opencode 共享服务"
            )
        if not endpoint["password"]:
            raise ConfigError(
                "已显式指定 OPENCODE_BASE_URL，必须在 .env 填写 OPENCODE_PASSWORD"
            )
    return {
        "bot_id": _require("WECOM_BOT_ID"),
        "bot_secret": _require("WECOM_BOT_SECRET"),
        "base_url": endpoint["base_url"],
        "username": endpoint["username"],
        "password": endpoint["password"],
        "opencode_auto": endpoint["auto"],
        "default_dir": default_dir,
        "group_dirs": checked_groups,
        "allowed_dirs": checked_allowed,
        "robot_name": os.getenv("WECOM_ROBOT_NAME") or "",
        "owner_ids": _list_env("WECOM_OWNER_IDS"),
        "state_path": str(PROJECT_ROOT / ".sessions.json"),
        "stream_min_chars": _int_env("STREAM_MIN_CHARS", DEFAULT_MIN_CHARS, minimum=1),
        "stream_min_interval": _int_env(
            "STREAM_MIN_INTERVAL", DEFAULT_MIN_INTERVAL, minimum=0
        ),
        "stream_enabled": _bool_env("STREAM_ENABLED", True),
        "stream_progress_minutes": _int_env(
            "STREAM_PROGRESS_MINUTES", DEFAULT_PROGRESS_MINUTES, minimum=0
        ),
        "render_max_chars": _int_env("RENDER_MAX_CHARS", DEFAULT_RENDER_MAX, minimum=200),
        "heartbeat_interval": _int_env(
            "HEARTBEAT_INTERVAL_SEC", DEFAULT_HEARTBEAT, minimum=0
        ),
        "confirm_timeout": _int_env(
            "CONFIRM_TIMEOUT_SEC", DEFAULT_CONFIRM_TIMEOUT, minimum=1
        ),
        "weather_city": (os.getenv("WECOM_WEATHER_CITY") or "").strip(),
        "weather_ip_fallback": _bool_env("WECOM_WEATHER_IP_FALLBACK", False),
    }


def auth_headers(cfg):
    import base64

    raw = f"{cfg['username']}:{cfg['password']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

"""发现并确保 opencode 共享后台服务可用。

opencode v2 采用客户端/服务端架构，同一用户只有一个共享后台服务，
它的运行时地址与密码登记在 ~/.local/state/opencode/service.json。
bridge 只做客户端，绝不自行拉起第二个 opencode serve，
否则会与系统服务争用端口、同一个 SQLite 库与会话状态。

命令行用法：
    python -m src.opencode_service ensure   确保服务就绪，打印地址
    python -m src.opencode_service status   只查询，不启动
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from base64 import b64encode
from pathlib import Path

DEFAULT_USERNAME = "opencode"
PROBE_PATH = "/openapi.json"
PROBE_TIMEOUT_SEC = 2.0
START_TIMEOUT_SEC = 45
AUTO_VALUES = {"", "auto", "service", "managed"}


class ServiceUnavailable(Exception):
    pass


def state_path():
    override = os.getenv("OPENCODE_SERVICE_STATE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state" / "opencode" / "service.json"


def config_path():
    override = os.getenv("OPENCODE_SERVICE_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "opencode" / "service.json"


def is_auto(base_url):
    return (base_url or "").strip().lower() in AUTO_VALUES


def _read_state():
    path = state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceUnavailable(f"无法读取服务登记文件 {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ServiceUnavailable(f"服务登记文件 {path} 格式不对，期望 JSON 对象")
    url = str(raw.get("url") or "").rstrip("/")
    if not url:
        return None
    return {
        "url": url,
        "username": DEFAULT_USERNAME,
        "password": str(raw.get("password") or ""),
        "pid": raw.get("pid"),
        "version": raw.get("version") or "",
        "state_path": str(path),
    }


def _configured_password():
    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ""
    if isinstance(raw, dict):
        return str(raw.get("password") or "")
    return ""


def _pid_alive(pid):
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def probe(url, username=DEFAULT_USERNAME, password="", timeout=PROBE_TIMEOUT_SEC):
    token = b64encode(f"{username}:{password}".encode()).decode()
    request = urllib.request.Request(url.rstrip("/") + PROBE_PATH)
    request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def discover():
    state = _read_state()
    if state is None:
        return None
    if state["pid"] is not None and not _pid_alive(state["pid"]):
        return None
    if not state["password"]:
        state["password"] = _configured_password()
    return state


def ensure(timeout=START_TIMEOUT_SEC):
    state = discover()
    if state and probe(state["url"], state["username"], state["password"]):
        return state

    binary = shutil.which("opencode")
    if not binary:
        raise ServiceUnavailable(
            "PATH 中找不到 opencode 命令，无法启动共享服务"
        )

    try:
        result = subprocess.run(
            [binary, "service", "start"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceUnavailable(
            f"opencode service start 超过 {timeout} 秒没有返回，手动执行 opencode service restart"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ServiceUnavailable(
            "opencode service start 失败，退出码 "
            f"{result.returncode}：{detail}\n"
            "服务卡住或端口被占时，手动执行 opencode service restart 后再试"
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = discover()
        if state and probe(state["url"], state["username"], state["password"]):
            return state
        time.sleep(0.5)

    raise ServiceUnavailable(
        f"等待 {timeout} 秒后共享服务仍未就绪，看 ~/.local/share/opencode/log/opencode.log"
    )


def resolve(base_url="", username="", password=""):
    user = (username or "").strip() or DEFAULT_USERNAME
    if is_auto(base_url):
        state = ensure()
        return {
            "base_url": state["url"],
            "username": user,
            "password": state["password"],
            "auto": True,
            "pid": state.get("pid"),
        }
    return {
        "base_url": (base_url or "").strip().rstrip("/"),
        "username": user,
        "password": password or "",
        "auto": False,
        "pid": None,
    }


def _load_dotenv():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(env_path)


def _main(argv):
    command = argv[1] if len(argv) > 1 else "status"
    _load_dotenv()
    try:
        if command == "ensure":
            state = ensure()
            print(state["url"])
            return 0
        if command == "check":
            endpoint = resolve(
                os.getenv("OPENCODE_BASE_URL", ""),
                os.getenv("OPENCODE_USERNAME", ""),
                os.getenv("OPENCODE_PASSWORD", ""),
            )
            if not probe(
                endpoint["base_url"], endpoint["username"], endpoint["password"]
            ):
                raise ServiceUnavailable(
                    f"指定的 opencode 地址 {endpoint['base_url']} 无响应；"
                    "本脚本不会自行拉起第二个 opencode serve，请检查该服务"
                )
            print(endpoint["base_url"])
            return 0
        if command == "status":
            state = discover()
            if not state:
                print("共享服务未运行")
                return 1
            health = "健康" if probe(state["url"], state["username"], state["password"]) else "无响应"
            print(f"{state['url']} {health}")
            return 0 if health == "健康" else 1
        print(f"未知命令 {command}，可用 check、ensure 或 status", file=sys.stderr)
        return 2
    except ServiceUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import opencode_service

FAILURES = []


def check(name, actual, predicate, detail=""):
    ok = predicate(actual)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Authorization") == "Basic b3BlbmNvZGU6c2VydmljZS1wYXNz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
        else:
            self.send_response(401)
            self.end_headers()

    def log_message(self, *args):
        pass


def main():
    check("空串视为 auto", opencode_service.is_auto(""), lambda v: v is True)
    check("auto 视为 auto", opencode_service.is_auto("auto"), lambda v: v is True)
    check("大写 AUTO 视为 auto", opencode_service.is_auto("AUTO"), lambda v: v is True)
    check("显式地址不算 auto", opencode_service.is_auto("http://127.0.0.1:4096"), lambda v: v is False)

    explicit = opencode_service.resolve("http://10.0.0.5:4096/", "user", "pass")
    check("显式地址原样返回", explicit["base_url"], lambda v: v == "http://10.0.0.5:4096")
    check("显式地址不标记 auto", explicit["auto"], lambda v: v is False)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    temp = Path(tempfile.mkdtemp(prefix="opencode_svc_"))
    state = temp / "service.json"
    state.write_text(
        json.dumps(
            {
                "url": f"http://127.0.0.1:{port}",
                "password": "service-pass",
                "pid": os.getpid(),
                "version": "2.0.32",
            }
        ),
        encoding="utf-8",
    )
    os.environ["OPENCODE_SERVICE_STATE"] = str(state)

    found = opencode_service.discover()
    check("能发现运行中的服务", found and found["url"], lambda v: v == f"http://127.0.0.1:{port}")
    check("密码取自登记文件", found["password"], lambda v: v == "service-pass")

    original_run = opencode_service.subprocess.run

    def forbidden_run(*args, **kwargs):
        raise AssertionError("服务健康时不应再执行 opencode service start")

    opencode_service.subprocess.run = forbidden_run
    endpoint = opencode_service.resolve("auto", "", "")
    opencode_service.subprocess.run = original_run
    check("auto 解析到共享服务", endpoint["base_url"], lambda v: v == f"http://127.0.0.1:{port}")
    check("auto 标记为真", endpoint["auto"], lambda v: v is True)
    check("auto 密码来自服务", endpoint["password"], lambda v: v == "service-pass")

    state.write_text(
        json.dumps({"url": f"http://127.0.0.1:{port}", "password": "service-pass", "pid": 999999}),
        encoding="utf-8",
    )
    check("进程号已死视为未运行", opencode_service.discover(), lambda v: v is None)

    state.unlink()
    original_which = opencode_service.shutil.which
    opencode_service.shutil.which = lambda name: None
    try:
        opencode_service.resolve("auto", "", "")
        check("缺少登记文件时报错", "未抛出", lambda v: False)
    except opencode_service.ServiceUnavailable:
        check("缺少登记文件时报错", "抛出", lambda v: True)
    opencode_service.shutil.which = original_which

    os.environ.pop("OPENCODE_SERVICE_STATE", None)
    server.shutdown()

    if FAILURES:
        print(f"{len(FAILURES)} 项失败: {', '.join(FAILURES)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

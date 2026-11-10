#!/bin/zsh
# 固化验证脚本：自找端口、自管进程生命周期、退出时按进程号精确清理。
set -u

PROJECT_ROOT="$(cd "$(dirname "${0:A}")/.." && pwd)"
PY="$PROJECT_ROOT/.venv/bin/python"
LOG_DIR="$(mktemp -d)"
SERVE_PID=""

cleanup() {
  if [[ -n "$SERVE_PID" ]] && kill -0 "$SERVE_PID" 2>/dev/null; then
    kill "$SERVE_PID" 2>/dev/null
    wait "$SERVE_PID" 2>/dev/null
    echo "已按进程号 $SERVE_PID 关闭临时 opencode serve"
  fi
  rm -rf "$LOG_DIR"
}
trap cleanup EXIT INT TERM

if [[ ! -x "$PY" ]]; then
  echo "缺少虚拟环境，请先执行 python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

FAIL=0

echo "=== 1. 纯逻辑用例 ==="
for t in test_renderer test_throttle test_session_router test_bridge_commands test_logger test_bridge_stream test_heartbeat test_quick_menu test_activity test_weather test_daily_report test_service_resolve; do
  echo "--- $t"
  if ! "$PY" "$PROJECT_ROOT/tests/$t.py"; then
    FAIL=1
  fi
done

echo
echo "=== 2. 找空闲端口 ==="
PORT=$(
  "$PY" - <<'EOF'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
EOF
)
if [[ -z "$PORT" ]]; then
  echo "无法分配端口"
  exit 1
fi
echo "使用端口 $PORT"

echo
echo "=== 3. 语法自检 ==="
if "$PY" - <<EOF
import ast, pathlib, sys
root = pathlib.Path("$PROJECT_ROOT")
bad = []
for f in sorted(root.rglob("*.py")):
    if ".venv" in f.parts:
        continue
    try:
        ast.parse(f.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        bad.append(f"{f.name}:{exc.lineno} {exc.msg}")
if bad:
    print("\n".join(bad))
    sys.exit(1)
print("全部 Python 文件语法正确")
EOF
then
  :
else
  FAIL=1
fi

echo
echo "=== 4. OpenCode 接口连通与契约 ==="
OPENCODE_SERVER_PASSWORD=testonly123 \
  opencode serve --port "$PORT" --hostname 127.0.0.1 > "$LOG_DIR/serve.log" 2>&1 &
SERVE_PID=$!

READY=0
for i in {1..30}; do
  if curl -s -o /dev/null "http://127.0.0.1:$PORT/openapi.json" -u opencode:testonly123; then
    READY=1
    break
  fi
  sleep 1
done

if [[ "$READY" != "1" ]]; then
  echo "opencode serve 未就绪，日志："
  cat "$LOG_DIR/serve.log"
  FAIL=1
else
  echo "服务已就绪"

  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/openapi.json" -u opencode:testonly123)
  if [[ "$CODE" == "200" ]]; then
    echo "[PASS] openapi.json 可访问"
  else
    echo "[FAIL] openapi.json 返回 $CODE"
    FAIL=1
  fi

  NOAUTH=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/openapi.json")
  if [[ "$NOAUTH" == "401" ]]; then
    echo "[PASS] 无认证被拒绝"
  else
    echo "[FAIL] 无认证返回 $NOAUTH，密码认证可能没生效"
    FAIL=1
  fi

  if "$PY" - <<EOF
import json, urllib.request, base64, sys
req = urllib.request.Request("http://127.0.0.1:$PORT/openapi.json")
req.add_header("Authorization", "Basic " + base64.b64encode(b"opencode:testonly123").decode())
spec = json.loads(urllib.request.urlopen(req).read())
paths = spec["paths"]
required = {
    "/api/session": ("post",),
    "/api/session/{sessionID}/prompt": ("post",),
    "/api/event": ("get",),
    "/api/session/{sessionID}/interrupt": ("post",),
    "/api/session/{sessionID}/permission": ("get",),
    "/api/session/{sessionID}/permission/{requestID}/reply": ("post",),
}
missing = []
for p, methods in required.items():
    if p not in paths:
        missing.append(f"缺路径 {p}")
        continue
    for m in methods:
        if m not in paths[p]:
            missing.append(f"缺方法 {m} {p}")
if missing:
    print("\n".join(missing))
    sys.exit(1)
reply = paths["/api/session/{sessionID}/permission/{requestID}/reply"]["post"]
schema = reply["requestBody"]["content"]["application/json"]["schema"]
enum = schema["properties"]["decision"]["\$ref"].split("/")[-1]
values = spec["components"]["schemas"][enum]["enum"]
if "reject" not in values or "once" not in values:
    print("权限决策枚举缺少 reject 或 once:", values)
    sys.exit(1)

def body_props(path, method):
    node = spec["paths"][path][method]
    ref = node["requestBody"]["content"]["application/json"]["schema"].get("\$ref")
    if ref:
        return spec["components"]["schemas"][ref.split("/")[-1]]["properties"]
    return node["requestBody"]["content"]["application/json"]["schema"]["properties"]

create_props = body_props("/api/session", "post")
if "location" not in create_props:
    print("POST /api/session 请求体缺少 location 字段，建会话的 directory 无处安放")
    sys.exit(1)
prompt_props = body_props("/api/session/{sessionID}/prompt", "post")
if "text" not in prompt_props:
    print("POST /api/session/{sessionID}/prompt 请求体缺少 text 字段")
    sys.exit(1)
if "decision" not in schema["properties"]:
    print("权限回复请求体缺少 decision 字段")
    sys.exit(1)
print("路径、请求体字段与权限决策枚举核对通过:", values)
EOF
  then
    :
  else
    FAIL=1
  fi
fi

echo
if [[ "$FAIL" == "0" ]]; then
  echo "全部验证通过"
  exit 0
fi
echo "存在失败项"
exit 1
#!/bin/zsh
# 启动脚本：环境检测、依赖安装、opencode 共享服务检查、拉起 bridge。
# bridge 只做客户端，服务生命周期归 opencode 自己管理。
# 本脚本不再自行拉起第二个 opencode serve，避免与系统服务抢端口、争用同一个数据库。
set -u

PROJECT_ROOT="$(cd "$(dirname "${0:A}")" && pwd)"
cd "$PROJECT_ROOT"

VENV="$PROJECT_ROOT/.venv"
PY="$VENV/bin/python"
ENV_FILE="$PROJECT_ROOT/.env"

log() { echo "[just_run] $*"; }
fail() { echo "[just_run] 失败: $*" >&2; exit 1; }

# 1. 虚拟环境
if [[ ! -x "$PY" ]]; then
  log "创建虚拟环境"
  python3 -m venv "$VENV" || fail "venv 创建失败"
fi

# 2. 依赖
if [[ ! -f "$VENV/.deps_ok" ]] || [[ requirements.txt -nt "$VENV/.deps_ok" ]]; then
  log "安装依赖"
  "$PY" -m pip install -q -r requirements.txt || fail "依赖安装失败"
  touch "$VENV/.deps_ok"
fi

# 3. 配置
[[ -f "$ENV_FILE" ]] || fail "缺少 .env，请先 cp .env.example .env 并填写"
chmod 600 "$ENV_FILE"

for key in WECOM_BOT_ID WECOM_BOT_SECRET WECOM_DEFAULT_DIR; do
  grep -qE "^$key=.+" "$ENV_FILE" || fail ".env 中 $key 未填写"
done

# 4. opencode 共享服务
# OPENCODE_BASE_URL 留空或写 auto 时自动发现共享服务，必要时用 opencode service start 拉起；
# 写了显式地址时只探测，绝不另起一个 serve。
log "检查 opencode 共享服务"
if ! ENDPOINT=$("$PY" -m src.opencode_service check); then
  fail "opencode 共享服务未就绪，看 ~/.local/share/opencode/log/opencode.log；卡住时执行 opencode service restart"
fi
log "opencode 共享服务已就绪: $ENDPOINT"

# 5. 拉起 bridge
# caffeinate 顶住系统休眠：Mac 空闲锁屏后会进入 Maintenance Sleep，期间进程被冻结、
# 企业微信长连接被掐断，锁屏期间的消息收不到，唤醒后还要几十秒才检测到断线并重连。
# -i 阻止空闲系统休眠，-m 阻止磁盘休眠，-s 在交流电下阻止系统休眠；不加 -d，屏幕照常锁。
# -w $$ 让 caffeinate 只做旁观者。下面用 exec 让 bridge 顶替脚本进程，
# launchd 的 SIGTERM 就能直接送到 bridge，秒级优雅退出，不用等满 ExitTimeOut。
log "启动 bridge（caffeinate 监视本进程，阻止系统休眠）"
caffeinate -ims -w $$ >/dev/null 2>&1 &
exec "$PY" bridge.py

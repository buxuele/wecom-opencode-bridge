#!/bin/zsh
# 把 bridge 注册成 LaunchAgent，实现开机自启与崩溃自动重启。
# 用法：./scripts/install_launchd.sh          安装并加载
#       ./scripts/install_launchd.sh uninstall 卸载
#       ./scripts/install_launchd.sh status    查看运行状态
set -eu

LABEL="ai.opencode.wecom.bridge"
PROJECT_ROOT="$(cd "$(dirname "${0:A}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SCRIPT="$PROJECT_ROOT/just_run.sh"

uninstall() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "已卸载 $LABEL，plist 已删除"
}

status() {
  echo "plist: $PLIST"
  if [[ -f "$PLIST" ]]; then
    /usr/libexec/PlistBuddy -c "Print :ProgramArguments:1" "$PLIST" 2>/dev/null || true
  else
    echo "plist 不存在，尚未安装"
  fi
  echo "launchd:"
  launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|runs =|last exit code" || echo "  未加载"
  echo "opencode 共享服务:"
  opencode service status 2>&1 | sed 's/^/  /' || true
}

if [[ "${1:-}" == "uninstall" ]]; then
  uninstall
  exit 0
fi

if [[ "${1:-}" == "status" ]]; then
  status
  exit 0
fi

[[ -x "$SCRIPT" ]] || { echo "找不到可执行脚本 $SCRIPT" >&2; exit 1; }
OPENCODE_BIN="$(command -v opencode || true)"
[[ -n "$OPENCODE_BIN" ]] || { echo "PATH 中找不到 opencode 命令" >&2; exit 1; }

# PATH 去重，并把 opencode 所在目录放到最前，保证 launchd 里也能找到它。
OPENCODE_DIR="${OPENCODE_BIN:h}"
CLEAN_PATH="$OPENCODE_DIR"
for part in ${(s.:.)PATH}; do
  [[ "$part" == "$OPENCODE_DIR" ]] && continue
  [[ -z "$part" ]] && continue
  CLEAN_PATH="$CLEAN_PATH:$part"
done

mkdir -p "$PROJECT_ROOT/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>$SCRIPT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_ROOT</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$CLEAN_PATH</string>
    </dict>
    <key>LimitLoadToSessionType</key>
    <array>
        <string>Aqua</string>
        <string>Background</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>ExitTimeOut</key>
    <integer>60</integer>
    <key>StandardOutPath</key>
    <string>$PROJECT_ROOT/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_ROOT/logs/launchd.err.log</string>
</dict>
</plist>
PLIST_EOF

chmod 644 "$PLIST"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "已安装并加载 $LABEL"
echo "plist: $PLIST"
sleep 2
status

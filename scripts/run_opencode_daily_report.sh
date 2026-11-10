#!/bin/zsh
# 无头生成 OpenCode 每日工作记录，供 launchd 或手动调用。
# 不指定模型时用 opencode 当前默认模型，想省钱就用环境变量 OPENCODE_REPORT_MODEL 指一个带 free 的模型。

WORKDIR="$(cd "$(dirname "${0:A}")/.." && pwd)"
PROJECT_DOCS="$WORKDIR/docs/opencode工作记录"
LOG="$WORKDIR/logs/opencode_daily_report.log"

mkdir -p "$PROJECT_DOCS"
cd "$WORKDIR" || exit 1

PROMPT="$(zsh "$WORKDIR/scripts/build_daily_report_prompt.sh")"

args=(run --auto --title "每日工作记录 $(date '+%Y-%m-%d')")
if [[ -n "${OPENCODE_REPORT_MODEL:-}" ]]; then
  args+=(--model "$OPENCODE_REPORT_MODEL")
fi

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') 开始 ==="
  /Users/fanchuang/.opencode/bin/opencode "${args[@]}" "$PROMPT"
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') 退出码 $? ==="
} >> "$LOG" 2>&1

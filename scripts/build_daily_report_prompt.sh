#!/bin/zsh
# 打印「生成今日 OpenCode 工作记录」的完整提示词，内含今天从数据库读出的原始数据。
# 交互命令 日报 与无头脚本 run_opencode_daily_report.sh 共用这一份，避免两处漂移。

WORKDIR="$(cd "$(dirname "${0:A}")/.." && pwd)"
PY="$WORKDIR/.venv/bin/python"
GEN="$WORKDIR/scripts/opencode_daily_report.py"
DATE="$(date '+%Y-%m-%d')"
OUT="$WORKDIR/docs/opencode工作记录/${DATE}.md"

cat <<EOF
下面是本机 OpenCode 数据库里今天的工作原始数据，由脚本 opencode_daily_report.py 直接读出，未经加工。

$( "$PY" "$GEN" )

请你基于上面这份数据做两件事。

1. 把正文里「（待补）」这一行替换成「今日概览」，写三到五条编号，归纳今天主要在做什么事。只许引用数据里真实出现的会话标题、目录、动作与时间，不许编造没有发生的事，不确定就不写。正文其余内容原样保留，不要改动、不要省略、不要重排。
2. 把改好的完整内容写入 ${OUT}。

写完后只回复两行：文件路径，以及一句话说明今天干了什么。不要重复正文。
EOF

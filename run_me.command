#!/bin/zsh
# 双击入口。只做两件事：调用 just_run.sh，保持终端不退出。
cd "$(dirname "${0:A}")"
./just_run.sh
echo
echo "进程已退出，按回车关闭窗口。"
read -r
exit
#!/usr/bin/env bash
# 守护 v4：掉线只记录、不自动登录、不重启 QQ。
# 反复登录是风控的主要触发源；恢复登录必须由人工扫码确认。
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs run
LOG=logs/supervisor.log
say(){ echo "[$(date '+%F %T')] $*" >> "$LOG"; }
say "supervisor v4 启动 (pid $$)：掉线只记录，不自动登录"

port_ok(){ ss -ltn 2>/dev/null | grep -q ':3001'; }

LAST_NAG=0
while true; do
  if port_ok; then
    if [ -f run/need_login.flag ]; then
      rm -f run/need_login.flag run/need_verify.flag run/qq_gone.flag
      say "登录已恢复（人工扫码成功）"
    fi
    LAST_NAG=0
  else
    if [ ! -f run/need_login.flag ]; then
      date '+%F %T' > run/need_login.flag
      cp qq/opt/QQ/resources/app/app_launcher/napcat/cache/qrcode.png run/qrcode.png 2>/dev/null
      say "检测到掉线/未登录：不重启 QQ、不尝试登录，只记录，等人工扫码（run/qrcode.png）"
      LAST_NAG=$(date +%s)
    fi

    if pgrep -f "^$PWD/qq/opt/QQ/qq( |\$)" >/dev/null 2>&1; then
      rm -f run/qq_gone.flag
    elif [ ! -f run/qq_gone.flag ]; then
      date '+%F %T' > run/qq_gone.flag
      say "QQ 进程不存在：按策略不自动拉起（重新拉起=重新登录，会加重风控）"
    fi

    now=$(date +%s)
    if [ "$LAST_NAG" -eq 0 ]; then
      LAST_NAG=$now
    elif [ $((now - LAST_NAG)) -ge 600 ]; then
      minutes=$(( (now - $(date -d "$(cat run/need_login.flag)" +%s)) / 60 ))
      say "仍未登录（约 ${minutes} 分钟）：继续等待人工扫码，不自动登录"
      LAST_NAG=$now
    fi

    # 只保证机器人进程在（它自己会重连，不涉及 QQ 登录）
    if ! { [ -f run/bot.pid ] && kill -0 "$(cat run/bot.pid)" 2>/dev/null; }; then
      say "机器人进程不在，拉起（不涉及 QQ 登录）"
      ./scripts/start-bot.sh >> "$LOG" 2>&1
    fi
  fi
  sleep 30
done

#!/usr/bin/env bash
cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)
[ -f run/kickwatch.pid ] && kill "$(cat run/kickwatch.pid)" 2>/dev/null && echo "[kickwatch] stopped" && rm -f run/kickwatch.pid
[ -f run/supervisor.pid ] && kill "$(cat run/supervisor.pid)" 2>/dev/null && echo "[supervisor] stopped" && rm -f run/supervisor.pid
[ -f run/bot.pid ] && kill "$(cat run/bot.pid)" 2>/dev/null && echo "[bot] stopped" && rm -f run/bot.pid
pkill -9 -f "^${ROOT}/qq/opt/QQ/qq( |\$)" 2>/dev/null && echo "[qq] stopped"
[ -f run/xvfb.pid ] && kill "$(cat run/xvfb.pid)" 2>/dev/null && echo "[xvfb] stopped" && rm -f run/xvfb.pid
exit 0

#!/usr/bin/env bash
# 启动 DeepSeek 机器人本体（连 NapCat 的 OneBot WS）
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs run data
if [ -f run/bot.pid ] && kill -0 "$(cat run/bot.pid)" 2>/dev/null; then
  echo "[bot] already running pid=$(cat run/bot.pid)"; exit 0
fi
setsid nohup .venv/bin/python bot/bot.py >> logs/bot.log 2>&1 &
echo $! > run/bot.pid
sleep 3
kill -0 "$(cat run/bot.pid)" 2>/dev/null && echo "[bot] started pid=$(cat run/bot.pid)" || { echo "[bot] FAILED"; tail -20 logs/bot.log; exit 1; }

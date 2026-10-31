#!/usr/bin/env bash
# 一键启动：Xvfb → QQ(NapCat) → 机器人 → 守护进程
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs run data
./scripts/start-qq.sh          # 默认快速登录（本地 token），失败自动回退二维码
sleep 20
./scripts/start-bot.sh
if [ -f run/supervisor.pid ] && kill -0 "$(cat run/supervisor.pid)" 2>/dev/null; then
  echo "[supervisor] already running pid=$(cat run/supervisor.pid)"
else
  setsid nohup ./scripts/supervisor.sh >/dev/null 2>&1 &
  echo $! > run/supervisor.pid
  echo "[supervisor] started pid=$(cat run/supervisor.pid)"
fi
if [ -f run/kickwatch.pid ] && kill -0 "$(cat run/kickwatch.pid)" 2>/dev/null; then
  echo "[kickwatch] already running pid=$(cat run/kickwatch.pid)"
else
  setsid nohup ./scripts/kickwatch.sh >/dev/null 2>&1 &
  sleep 1
  echo "[kickwatch] started pid=$(cat run/kickwatch.pid 2>/dev/null)"
fi
./scripts/status.sh

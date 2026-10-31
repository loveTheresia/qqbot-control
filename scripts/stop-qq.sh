#!/usr/bin/env bash
# 优雅停止：SIGTERM → 等待落盘 → 超时才 SIGKILL（-9 会丢登录态，导致下次必须重登）
cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)
PAT="^${ROOT}/qq/opt/QQ/qq( |\$)"
pids=$(pgrep -f "$PAT" 2>/dev/null)
[ -z "$pids" ] && { rm -f run/qq.pid; exit 0; }
kill -TERM $pids 2>/dev/null
for i in $(seq 1 20); do
  pgrep -f "$PAT" >/dev/null 2>&1 || { echo "[qq] 已优雅退出（登录态已保存）"; rm -f run/qq.pid; exit 0; }
  sleep 1
done
echo "[qq] 20 秒未退出，强制结束（可能丢失登录态）"
kill -9 $pids 2>/dev/null
rm -f run/qq.pid

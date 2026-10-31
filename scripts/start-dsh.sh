#!/bin/bash
# 一起启动 DSH web 和孤独的鱼工作台；DSH 退出时自动停工作台。
set -euo pipefail
cd "$(dirname "$0")/.."
if ss -ltn 2>/dev/null | grep -q ':8099 '; then
  echo "[孤独的鱼] 8099 已在运行，跳过启动"
else
  .venv/bin/python scripts/workbench.py --port 8099 --no-follow-dsh &
  WB=$!
  trap 'kill "$WB" 2>/dev/null || true' EXIT INT TERM
  sleep 1
fi
dsh web "$@"

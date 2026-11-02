#!/bin/bash
# 只在未登录时使用：重启 QQ，刷新二维码。
set -u
cd "$(dirname "$0")/.."
before=$(stat -c %Y run/qrcode-big.png 2>/dev/null || echo 0)
./scripts/stop-qq.sh >/dev/null 2>&1 || true
sleep 2
./scripts/start-qq.sh --qr >/dev/null 2>&1 || true
for i in $(seq 1 30); do
  sleep 1
  now=$(stat -c %Y run/qrcode-big.png 2>/dev/null || echo 0)
  if [ "$now" -gt "$before" ]; then exit 0; fi
done
exit 1

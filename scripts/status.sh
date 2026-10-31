#!/usr/bin/env bash
cd "$(dirname "$0")/.." || exit 1
say(){ printf '  %-12s %s\n' "$1" "$2"; }
echo "=== 进程 ==="
for p in xvfb qq bot supervisor kickwatch; do
  if [ -f run/$p.pid ] && kill -0 "$(cat run/$p.pid)" 2>/dev/null; then
    say "$p:" "RUNNING (pid $(cat run/$p.pid), $(ps -o rss= -p "$(cat run/$p.pid)" | awk '{printf "%.0fMB", $1/1024}'))"
  else say "$p:" "STOPPED"; fi
done
echo "=== QQ 登录 ==="
if ss -ltn 2>/dev/null | grep -q ':3001'; then
  TOKEN=$(grep -E '^TOKEN=' config/.env | cut -d= -f2)
  curl -s -m 5 "http://127.0.0.1:3000/get_login_info" -H "Authorization: Bearer $TOKEN" \
   | python3 -c "import json,sys;d=json.load(sys.stdin);print('  在线:',d['data']['user_id'],d['data']['nickname'])" 2>/dev/null || echo "  OneBot 端口在但 API 无响应"
else
  if [ -f run/need_login.flag ]; then
    echo "  ❌ 未登录，需要扫码（$(cat run/need_login.flag) 起）  二维码: run/qrcode.png"
  else
    echo "  ❌ 未登录（启动中或掉线）"
  fi
fi
echo "=== 掉线记录 ==="
echo "  策略：只记录，不自动登录、不重启 QQ（人工扫码恢复）"
if [ -f run/kick.alert ]; then
  echo "  当前处于掉线状态："
  sed 's/^/    /' run/kick.alert
elif [ -f run/kickwatch.state ] && [ "$(cut -d' ' -f1 run/kickwatch.state)" = DOWN ]; then
  echo "  监控中：未登录（自 $(date -d "@$(cut -d' ' -f2 run/kickwatch.state)" '+%F %T') 起）"
  grep -a 'KICK' logs/kicks.log 2>/dev/null | tail -2 | sed 's/^/  /'
elif [ -f logs/kicks.log ]; then
  grep -a 'KICK' logs/kicks.log | tail -3 | sed 's/^/  /'
else
  echo "  暂无记录"
fi
echo "=== 内存 ==="
free -m | sed -n '2p'

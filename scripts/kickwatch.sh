#!/usr/bin/env bash
# 登录看护：记录每次会话时长/掉线原因，掉线期间持续把最新二维码同步到 run/
# 只做观测和兜底，不重启 QQ、不改变 supervisor 的行为。
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs run

PORT="${KICKWATCH_PORT:-3001}"
INTERVAL="${KICKWATCH_INTERVAL:-15}"
STATE=run/kickwatch.state
ALERT=run/kick.alert
KLOG=logs/kicks.log
QR_SRC=qq/opt/QQ/resources/app/app_launcher/napcat/cache/qrcode.png
QR_DST=run/qrcode.png
QR_BIG=run/qrcode-big.png

say(){ echo "[$(date '+%F %T')] $*" >> "$KLOG"; }

port_ok(){ ss -ltn 2>/dev/null | grep -q ":${PORT} "; }

sync_qr(){
  [ -f "$QR_SRC" ] || return 0
  if [ ! -f "$QR_DST" ] || [ "$QR_SRC" -nt "$QR_DST" ]; then
    cp -f "$QR_SRC" "$QR_DST" 2>/dev/null || return 0
    python3 scripts/qr_upscale.py "$QR_DST" "$QR_BIG" 6 >/dev/null 2>&1 || true
  fi
}

reason(){
  # 取最近一条能说明原因的日志，去掉颜色码
  local line
  line=$(grep -aE 'KickedOffLine|登录已失效|需要验证码|Login Error' logs/qq.log 2>/dev/null | tail -1)
  [ -z "$line" ] && line=$(tail -n 40 logs/qq.log 2>/dev/null | grep -aE '二维码|登录' | tail -1)
  printf '%s' "$line" | sed -e 's/\x1b\[[0-9;]*m//g' -e 's/^ *//' | cut -c1-200
}

human_dur(){
  local s=$1
  printf '%dh%02dm' $((s/3600)) $(((s%3600)/60))
}

notify(){
  # 统一走 scripts/notify.py：支持企业微信/Server酱/PushPlus/Bark/邮件/自定义 webhook。
  # 没配置渠道时只记一行“未配置”，不影响其它功能。
  local out rc
  out=$(python3 scripts/notify.py "$1" 2>&1); rc=$?
  if [ "$rc" -eq 0 ]; then
    say "通知已发出：$(printf '%s' "$out" | tr '\n' ';' | cut -c1-300)"
  else
    say "通知未发出（rc=$rc）：$(printf '%s' "$out" | tr '\n' ';' | cut -c1-300)"
  fi
}

# 单实例（注意：这一步必须在写 pid / 装 trap 之前）
if [ -f run/kickwatch.pid ] && kill -0 "$(cat run/kickwatch.pid)" 2>/dev/null; then
  echo "[kickwatch] already running pid=$(cat run/kickwatch.pid)"; exit 0
fi
echo $$ > run/kickwatch.pid
cleanup(){ rm -f run/kickwatch.pid; }
trap cleanup EXIT
trap 'cleanup; exit 0' INT TERM HUP

if port_ok; then state=UP; else state=DOWN; fi
since=$(date +%s)
printf '%s %s\n' "$state" "$since" > "$STATE"
say "kickwatch 启动：当前状态 $state（端口 $PORT，间隔 ${INTERVAL}s）"
[ "$state" = DOWN ] && sync_qr

while true; do
  sleep "$INTERVAL" &
  wait $!
  now=$(date +%s)
  if port_ok; then
    if [ "$state" = DOWN ]; then
      gap=$((now - since))
      since=$now; state=UP
      printf 'UP %s\n' "$since" > "$STATE"
      rm -f "$ALERT"
      say "LOGIN 已登录（离线约 $(human_dur "$gap")）"
    fi
  else
    sync_qr
    if [ "$state" = UP ]; then
      dur=$((now - since))
      r=$(reason)
      since=$now; state=DOWN
      printf 'DOWN %s\n' "$since" > "$STATE"
      {
        echo "time=$(date '+%F %T')"
        echo "event=KICK"
        echo "session_seconds=$dur"
        echo "session_human=$(human_dur "$dur")"
        echo "reason=$r"
        echo "qr=run/qrcode.png"
      } > "$ALERT"
      say "KICK 掉线：本次会话 $(human_dur "$dur")｜原因：${r:-未知}"
      notify "QQ 掉线：本次会话 $(human_dur "$dur")，原因：${r:-未知}，二维码 run/qrcode.png"
    fi
  fi
done

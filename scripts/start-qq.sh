#!/usr/bin/env bash
# 启动 QQ：
#   默认       快速登录（只用本地 token，不碰密码）；失败自动回退二维码
#   --qr       强制只出二维码
#   --password 允许密码回退（可能触发短信验证，慎用）
#   --dry-run  只打印将要执行的命令，不启动
set -u
cd "$(dirname "$0")/.." && source scripts/env.sh
mkdir -p logs run

if [ -f run/xvfb.pid ] && kill -0 "$(cat run/xvfb.pid)" 2>/dev/null; then :; else
  nohup "$QQ_HOME/sysroot/usr/bin/Xvfb.patched" :99 -screen 0 1280x800x24 -nolisten tcp > logs/xvfb.log 2>&1 &
  echo $! > run/xvfb.pid; sleep 2
fi

# 读取登录凭据
if [ -f "$QQ_HOME/config/.env" ]; then
  set -a; . "$QQ_HOME/config/.env"; set +a
fi

MODE=quick
ALLOW_PW=0
DRY=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --qr) MODE=qr ;;
    --password) ALLOW_PW=1 ;;
    --dry-run) DRY=1 ;;
    *) ARGS+=("$a") ;;
  esac
done

if [ "$MODE" = quick ] && [ "$ALLOW_PW" -eq 0 ] && [ -f run/need_login.flag ]; then
  MODE=qr
  echo "[qq] 检测到掉线标记：跳过快速登录，直接出二维码（避免无效重试）"
fi

if [ "$MODE" = qr ]; then
  unset NAPCAT_QUICK_ACCOUNT NAPCAT_QUICK_PASSWORD NAPCAT_QUICK_PASSWORD_MD5
  ARGS=()
  echo "[qq] --qr：只出二维码，不尝试快速/密码登录"
else
  export NAPCAT_QUICK_ACCOUNT="${NAPCAT_QUICK_ACCOUNT:-}"
  if [ "$ALLOW_PW" -eq 0 ]; then
    # 只用本地 token 快速登录；token 失效时由 NapCat 回退二维码，不碰密码
    unset NAPCAT_QUICK_PASSWORD NAPCAT_QUICK_PASSWORD_MD5
    echo "[qq] 快速登录（仅本地 token；失败回退二维码，不用密码）"
  else
    echo "[qq] 快速登录（允许密码回退，可能要求短信验证）"
  fi
  HAS_Q=0
  for a in "${ARGS[@]}"; do
    case "$a" in -q|--qq) HAS_Q=1 ;; esac
  done
  if [ "$HAS_Q" -eq 0 ]; then
    ARGS=(-q "$NAPCAT_QUICK_ACCOUNT" "${ARGS[@]}")
  fi
fi

CMD=("$QQ_HOME/qq/opt/QQ/qq" --no-sandbox --disable-gpu --in-process-gpu --disable-dev-shm-usage "${ARGS[@]}")

if [ "$DRY" -eq 1 ]; then
  printf '[qq] 凭据：account=%s password=%s\n' "${NAPCAT_QUICK_ACCOUNT:-none}" "$([ -n "${NAPCAT_QUICK_PASSWORD:-}" ] && echo set || echo unset)"
  printf '[qq] dry-run:'; printf ' %q' "${CMD[@]}"; echo
  exit 0
fi

if pgrep -f "^${QQ_HOME}/qq/opt/QQ/qq( |\$)" >/dev/null 2>&1; then
  echo "[qq] already running, skip restart"; exit 0
fi

cd "$QQ_HOME/qq/opt/QQ"
setsid nohup "${CMD[@]}" > "$QQ_HOME/logs/qq.log" 2>&1 &
echo $! > "$QQ_HOME/run/qq.pid"
sleep 6
echo "[qq] pgid=$(ps -o pgid= -p "$(cat "$QQ_HOME/run/qq.pid")" 2>/dev/null | tr -d ' ') pid=$(cat "$QQ_HOME/run/qq.pid")"

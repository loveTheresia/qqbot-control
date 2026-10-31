#!/usr/bin/env bash
# 开关 NapCat 的 bypass（反检测）六项：hook/window/module/process/container/js
# 用法：
#   ./scripts/set-bypass.sh show [live|alt]   # 查看
#   ./scripts/set-bypass.sh on   [live|alt]   # 全开
#   ./scripts/set-bypass.sh off  [live|alt]   # 全关
# 注意：改完要重启 QQ 才生效；重启只出二维码，不会自动登录。
set -euo pipefail
cd "$(dirname "$0")/.."
mode=${1:-show}
target=${2:-live}
case "$mode" in on|off|show) ;; *) echo "用法：$0 on|off|show [live|alt]"; exit 1;; esac
case "$target" in
  live) base="qq/opt/QQ/resources/app/app_launcher/napcat/config" ;;
  alt)  base="alt/qq/opt/QQ/resources/app/app_launcher/napcat/config" ;;
  *) echo "target 只能是 live 或 alt"; exit 1 ;;
esac

files=()
for f in "$base"/napcat.json "$base"/napcat_*.json; do
  [ -f "$f" ] && files+=("$f")
done
if [ ${#files[@]} -eq 0 ]; then
  echo "找不到 $base 下的 napcat 配置"
  exit 1
fi

python3 - "$mode" "$target" "${files[@]}" <<'PY'
import json, os, shutil, sys, time

mode, target = sys.argv[1], sys.argv[2]
files = sys.argv[3:]
keys = ["hook", "window", "module", "process", "container", "js"]

for p in files:
    d = json.load(open(p))
    b = d.setdefault("bypass", {})
    if mode == "show":
        print(f"{p}: " + " ".join(f"{k}={b.get(k, False)}" for k in keys))
        continue
    if os.path.exists(p) and not os.path.exists(p + ".bak"):
        shutil.copy2(p, p + ".bak")
    for k in keys:
        b[k] = (mode == "on")
    d["bypass"] = b
    json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
    print(f"{p}: bypass -> {mode}")

if mode == "on" and target == "live":
    print("已全开。需要重启 QQ 才生效（./scripts/stop-qq.sh && ./scripts/start-qq.sh；只出二维码，不自动登录）")
PY

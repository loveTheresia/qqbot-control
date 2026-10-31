#!/usr/bin/env bash
# 免 root 便携运行环境（QQ/NapCat 用）
export QQ_HOME=/home/opt/QQBot
export SYSROOT=$QQ_HOME/sysroot
export LD_LIBRARY_PATH=$(find $SYSROOT/usr/lib -maxdepth 3 -type d | tr '\n' ':')$SYSROOT/lib/x86_64-linux-gnu
export XKB_CONFIG_ROOT=$SYSROOT/usr/share/X11/xkb
export FONTCONFIG_PATH=/etc/fonts
export FONTCONFIG_FILE=/etc/fonts/fonts.conf
export HOME=$QQ_HOME/home
export XDG_CONFIG_HOME=$QQ_HOME/home/.config
export XDG_CACHE_HOME=$QQ_HOME/home/.cache
export XDG_DATA_HOME=$QQ_HOME/home/.local/share
mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME"
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-$QQ_HOME/run}
export DISPLAY=:99
mkdir -p "$XDG_RUNTIME_DIR" /tmp/xkb
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null
[ -e /tmp/xkb/xkbcomp ] || ln -sf $SYSROOT/usr/bin/xkbcomp /tmp/xkb/xkbcomp

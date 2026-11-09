#!/bin/bash
# SafeHalo macOS Launchd 开机自启服务一键安装脚本
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="$(which python3)"

TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_PLIST="$TARGET_DIR/com.safehalo.service.plist"

mkdir -p "$TARGET_DIR"

echo "=== 安装 SafeHalo macOS Launchd 开机自启服务 ==="

# 卸载旧服务 (若存在)
if launchctl list | grep -q "com.safehalo.service"; then
    launchctl unload "$TARGET_PLIST" 2>/dev/null || true
fi

# 替换动态路径
sed -e "s|/Users/yifanwang/workspace/SafeHalo|$APP_DIR|g" \
    -e "s|/usr/bin/python3|$PYTHON_BIN|g" \
    "$SCRIPT_DIR/com.safehalo.service.plist" > "$TARGET_PLIST"

# 载入服务
launchctl load "$TARGET_PLIST"

echo ""
echo "✅ SafeHalo macOS 开机自启服务已加载成功！"
echo "当 Mac 掉电开机、重启或登录后，SafeHalo 将全自动在后台运行。"
echo "管理命令:"
echo "  查看进程: launchctl list | grep safehalo"
echo "  查看日志: tail -f \"$APP_DIR/logs/launchd_stdout.log\""
echo "  卸载自启: launchctl unload \"$TARGET_PLIST\""

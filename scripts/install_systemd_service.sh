#!/bin/bash
# SafeHalo Linux Systemd 开机自启服务一键安装脚本
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="$(which python3 || echo '/usr/bin/python3')"

echo "=== 安装 SafeHalo Systemd 开机自启服务 ==="

# 1. 生成适配当前目录的 unit 文件
TARGET_UNIT="/etc/systemd/system/safehalo.service"

sed -e "s|/opt/SafeHalo|$APP_DIR|g" \
    -e "s|/usr/bin/python3|$PYTHON_BIN|g" \
    "$SCRIPT_DIR/safehalo.service" > /tmp/safehalo.service

sudo mv /tmp/safehalo.service "$TARGET_UNIT"
sudo chmod 644 "$TARGET_UNIT"

# 2. 重载并启用服务
sudo systemctl daemon-reload
sudo systemctl enable safehalo
sudo systemctl restart safehalo

echo ""
echo "✅ SafeHalo 已成功安装为系统服务！"
echo "当系统掉电重启或崩溃后，将在 5 秒内自动重启恢复。"
echo "管理命令:"
echo "  查看状态: sudo systemctl status safehalo"
echo "  查看日志: journalctl -u safehalo -f"
echo "  重启服务: sudo systemctl restart safehalo"
echo "  停止服务: sudo systemctl stop safehalo"

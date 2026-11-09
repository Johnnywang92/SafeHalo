#!/bin/sh
# SafeHalo OpenWrt 开机自启服务一键安装脚本
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== 安装 SafeHalo OpenWrt 开机自启服务 ==="

# 1. 复制 init 脚本至 /etc/init.d/
cp "$SCRIPT_DIR/openwrt_safehalo.init" /etc/init.d/safehalo
chmod +x /etc/init.d/safehalo

# 2. 动态修正工作目录路径
sed -i "s|APP_DIR=/root/SafeHalo|APP_DIR=$APP_DIR|g" /etc/init.d/safehalo

# 3. 启用开机自启 (创建 /etc/rc.d/S99safehalo 软链接)
/etc/init.d/safehalo enable

# 4. 立即启动服务
/etc/init.d/safehalo restart

echo ""
echo "✅ SafeHalo 已成功设为 OpenWrt 开机自启！"
echo "当路由器意外断电恢复开机后，SafeHalo 将在 5 秒内自动拉起。"
echo "管理命令:"
echo "  查看状态: /etc/init.d/safehalo status"
echo "  查看日志: logread -f | grep SafeHalo"
echo "  重启服务: /etc/init.d/safehalo restart"
echo "  停止服务: /etc/init.d/safehalo stop"

#!/bin/bash
# ==============================================================================
# SafeHalo 无感无线电波跌倒守护系统 · 极简一键安装与启动助手 (install.sh)
#
# 支持环境:
# 1. OpenWrt 路由器 (mipsel / mips / aarch64 / x86_64 一键自启部署)
# 2. Linux 服务器 / 树莓派 (systemd 开机自启服务)
# 3. macOS 本地开发与体验 (launchd 开机自启 / 本地控制台极速启动)
# ==============================================================================

set -e

# 定位工程根目录
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

RUST_RELEASE_BIN="$PROJECT_DIR/experiments/rust-daemon/target/release/safehalo-daemon"
RUST_DEBUG_BIN="$PROJECT_DIR/experiments/rust-daemon/target/debug/safehalo-daemon"

# 颜色定义
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# 提取本机局域网 IP
get_local_ip() {
    ip=""
    if [ -f /etc/openwrt_release ]; then
        ip=$(uci get network.lan.ipaddr 2>/dev/null || true)
        if [ -z "$ip" ] && command -v ip >/dev/null 2>&1; then
            ip=$(ip -4 addr show br-lan 2>/dev/null | grep -oE 'inet [0-9.]+' | cut -d' ' -f2 || true)
        fi
    fi
    if [ -z "$ip" ] && command -v ip >/dev/null 2>&1; then
        ip=$(ip route get 1.1.1.1 2>/dev/null | grep -oE 'src [0-9.]+' | cut -d' ' -f2 || true)
    fi
    if [ -z "$ip" ] && command -v ifconfig >/dev/null 2>&1; then
        ip=$(ifconfig 2>/dev/null | grep -E 'inet (192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))' | head -n1 | awk '{print $2}' | tr -d 'addr:' || true)
    fi
    echo "${ip:-127.0.0.1}"
}

# 自动在默认浏览器打开 URL
open_browser() {
    url="$1"
    if [ -n "$DISPLAY" ] || [ "$(uname)" = "Darwin" ]; then
        if command -v open >/dev/null 2>&1; then
            open "$url" 2>/dev/null || true
        elif command -v xdg-open >/dev/null 2>&1; then
            xdg-open "$url" 2>/dev/null || true
        fi
    fi
}

# 1. OpenWrt 环境自动安装
install_openwrt() {
    echo -e "${BLUE}${BOLD}>>> 检测到当前系统为 OpenWrt 路由器，正在启动路由器专用免依赖部署...${NC}"
    if [ -f "$PROJECT_DIR/experiments/rust-daemon/deploy/install.sh" ]; then
        sh "$PROJECT_DIR/experiments/rust-daemon/deploy/install.sh" "$PROJECT_DIR/experiments/rust-daemon/deploy"
    else
        echo -e "${RED}❌ 未找到 OpenWrt 部署套件: experiments/rust-daemon/deploy/install.sh${NC}"
        exit 1
    fi

    local_ip=$(get_local_ip)
    echo ""
    echo -e "${GREEN}========================================================================${NC}"
    echo -e "${GREEN}${BOLD}  🎉 SafeHalo 守护引擎在 OpenWrt 路由器上安装并启动成功！${NC}"
    echo -e "${GREEN}========================================================================${NC}"
    echo -e "  服务状态: ${BOLD}已设为开机自动启动 (procd 守护)${NC}"
    echo -e "  管理命令: /etc/init.d/safehalo {start|stop|restart|status}"
    echo -e "  Web 控制台:"
    echo -e "    👉  ${BOLD}http://${local_ip}:8091${NC} (路由器后台管理)"
    echo -e "    👉  http://127.0.0.1:8091"
    echo -e "${GREEN}========================================================================${NC}"
    echo ""
}

# 2. 极速体验模式 (本地前台运行)
run_quickstart() {
    echo ""
    echo -e "${BLUE}${BOLD}>>> 正在启动 SafeHalo 极速体验模式...${NC}"

    # 1. 自动寻找可用的探针串口
    port=""
    for p in /dev/ttyUSB* /dev/ttyACM* /dev/cu.usbserial* /dev/cu.usbmodem* /dev/cu.wchusb*; do
        if [ -e "$p" ]; then
            port="$p"
            break
        fi
    done

    # 2. 确定运行引擎
    daemon_bin=""
    if [ -f "$RUST_RELEASE_BIN" ]; then
        daemon_bin="$RUST_RELEASE_BIN"
    elif [ -f "$RUST_DEBUG_BIN" ]; then
        daemon_bin="$RUST_DEBUG_BIN"
    elif command -v cargo >/dev/null 2>&1; then
        echo -e "${YELLOW}正在编译高效 Rust 守护服务 (仅首次需要)...${NC}"
        cargo build --release --manifest-path experiments/rust-daemon/Cargo.toml
        daemon_bin="$RUST_RELEASE_BIN"
    fi

    local_ip=$(get_local_ip)
    web_url="http://127.0.0.1:8091"

    if [ -n "$daemon_bin" ]; then
        echo -e "${GREEN}✅ 选用超轻量 Rust 守护引擎: $daemon_bin${NC}"
        if [ -n "$port" ]; then
            echo -e "🔌 自动检测到射频探针串口: ${BOLD}$port${NC} (波特率 921600)"
            serial_arg="--serial $port --baud 921600"
        else
            echo -e "${YELLOW}ℹ️ 未检测到已插入的物理串口探针，将以仿真重放模式启动控制台...${NC}"
            serial_arg="--replay experiments/rust-daemon/fixtures/generated/main.txt --replay-rate 30"
        fi

        echo ""
        echo -e "${GREEN}========================================================================${NC}"
        echo -e "${GREEN}${BOLD}  🚀 SafeHalo 控制台服务已就绪！${NC}"
        echo -e "  请在电脑或手机浏览器打开以下地址开始配置守护:"
        echo -e "  👉  ${BOLD}$web_url${NC} (本机)"
        echo -e "  👉  http://${local_ip}:8091 (局域网)"
        echo -e "${GREEN}========================================================================${NC}"
        echo -e "按 ${BOLD}Ctrl+C${NC} 可随时平稳退出服务。"
        echo ""

        # 尝试自动弹开浏览器
        (sleep 1.2 && open_browser "$web_url") &

        exec "$daemon_bin" --http "0.0.0.0:8091" $serial_arg --storage-dir "$PROJECT_DIR/daemon-storage"
    else
        # 回退到 Python 引擎
        echo -e "${YELLOW}未发现 Rust 二进制，回退选用 Python 原生生产服务...${NC}"
        web_url="http://127.0.0.1:8080"
        (sleep 1.2 && open_browser "$web_url") &
        exec python3 "$PROJECT_DIR/safehalo_service.py"
    fi
}

# 3. 安装为系统开机自启服务
install_system_service() {
    os_name="$(uname)"
    if [ -f /etc/openwrt_release ]; then
        install_openwrt
    elif [ "$os_name" = "Darwin" ]; then
        echo -e "${BLUE}>>> 正在为 macOS 安装 Launchd 开机自启服务...${NC}"
        bash "$PROJECT_DIR/scripts/install_macos_launchd.sh"
    elif command -v systemctl >/dev/null 2>&1; then
        echo -e "${BLUE}>>> 正在为 Linux 安装 Systemd 开机自启服务...${NC}"
        sudo bash "$PROJECT_DIR/scripts/install_systemd_service.sh"
    else
        echo -e "${RED}❌ 未识别的系统服务管理器，请使用极速体验模式运行。${NC}"
    fi
}

# 4. 远程 SSH 一键推送到 OpenWrt 路由器
remote_install_openwrt() {
    router_host="$1"
    if [ -z "$router_host" ]; then
        read -p "请输入 OpenWrt 路由器的 IP 地址 (默认 192.168.1.1): " router_host
        router_host="${router_host:-192.168.1.1}"
    fi

    echo -e "${BLUE}${BOLD}>>> 正在远程推送部署至 OpenWrt 路由器 [$router_host]...${NC}"
    echo "提示: 如需输入路由器密码，请在提示时输入 root 密码。"

    # 测试 SSH 连通性
    if ! ssh -o ConnectTimeout=5 "root@$router_host" "echo ok" >/dev/null 2>&1; then
        echo -e "${RED}❌ 无法通过 SSH 连接到 root@$router_host。请确认路由器 IP 与 SSH 服务已开启。${NC}"
        exit 1
    fi

    # 打包必要部署文件并推送到路由器 /tmp
    echo "📦 打包部署文件..."
    tar -czf /tmp/safehalo_deploy.tar.gz \
        -C "$PROJECT_DIR" \
        experiments/rust-daemon/deploy \
        experiments/rust-daemon/target 2>/dev/null || tar -czf /tmp/safehalo_deploy.tar.gz -C "$PROJECT_DIR" experiments/rust-daemon/deploy

    scp /tmp/safehalo_deploy.tar.gz "root@$router_host:/tmp/"
    rm -f /tmp/safehalo_deploy.tar.gz

    echo "🚀 在路由器上执行一键安装..."
    ssh "root@$router_host" "mkdir -p /tmp/sh_inst && tar -xzf /tmp/safehalo_deploy.tar.gz -C /tmp/sh_inst && sh /tmp/sh_inst/experiments/rust-daemon/deploy/install.sh /tmp/sh_inst/experiments/rust-daemon/deploy && rm -rf /tmp/sh_inst /tmp/safehalo_deploy.tar.gz"

    echo ""
    echo -e "${GREEN}========================================================================${NC}"
    echo -e "${GREEN}${BOLD}  🎉 SafeHalo 远程安装成功！${NC}"
    echo -e "  请直接在浏览器访问以下地址开始配置守护:"
    echo -e "  👉  ${BOLD}http://${router_host}:8091${NC}"
    echo -e "${GREEN}========================================================================${NC}"
}

# 5. 交互式菜单展示
show_menu() {
    os_desc="$(uname -s) ($(uname -m))"
    if [ -f /etc/openwrt_release ]; then
        os_desc="OpenWrt Linux ($(uname -m))"
    fi

    clear 2>/dev/null || true
    echo -e "${BLUE}========================================================================${NC}"
    echo -e "${BLUE}${BOLD}  🛡️  SafeHalo 无感无线电波跌倒守护系统 · 极简一键安装与启动向导${NC}"
    echo -e "${BLUE}========================================================================${NC}"
    echo -e "当前系统环境: ${BOLD}$os_desc${NC}"
    echo ""
    echo -e "请选择操作 [输入数字 1-6]:"
    echo -e "  ${GREEN}${BOLD}[1] 🚀 极速体验模式${NC} (一键自动启动，并在浏览器打开 Web 控制台)"
    echo -e "  ${BOLD}[2] 🔧 安装为系统后台常驻服务${NC} (开机自启: procd / systemd / launchd)"
    echo -e "  ${BOLD}[3] 📡 硬件探针现场体检${NC} (一键排查串口、波特率、52子载波与提频)"
    echo -e "  ${BOLD}[4] ⚡ ESP32-S3 探针固件烧录助手${NC} (一键检测芯片并刷写固件)"
    echo -e "  ${BOLD}[5] 🌐 远程部署到 OpenWrt 路由器${NC} (通过 SSH 一键推送到路由器)"
    echo -e "  ${BOLD}[6] 🔄 一键安全回滚${NC} (恢复上一版二进制程序)"
    echo -e "  ${BOLD}[0] 退出${NC}"
    echo -e "${BLUE}========================================================================${NC}"
    read -p "请输入选项 [默认 1]: " choice
    choice="${choice:-1}"

    case "$choice" in
        1) run_quickstart ;;
        2) install_system_service ;;
        3) python3 "$PROJECT_DIR/scripts/hardware_probe_doctor.py" ;;
        4) bash "$PROJECT_DIR/firmware/flash_firmware.sh" ;;
        5) remote_install_openwrt ;;
        6)
            if [ -f "$PROJECT_DIR/experiments/rust-daemon/deploy/install.sh" ]; then
                sh "$PROJECT_DIR/experiments/rust-daemon/deploy/install.sh" --rollback
            fi
            ;;
        0) echo "已退出。"; exit 0 ;;
        *) echo "无效选项: $choice"; exit 1 ;;
    esac
}

# ==================== 主入口参数分流 ====================

# 如果在 OpenWrt 路由器环境且没有指定参数，直接进行 OpenWrt 一键安装
if [ -f /etc/openwrt_release ] && [ $# -eq 0 ]; then
    install_openwrt
    exit 0
fi

# 参数处理
case "$1" in
    -q|--quick|quick)
        run_quickstart
        ;;
    -s|--service|service)
        install_system_service
        ;;
    -d|--doctor|doctor)
        python3 "$PROJECT_DIR/scripts/hardware_probe_doctor.py"
        ;;
    -f|--flash|flash)
        shift
        bash "$PROJECT_DIR/firmware/flash_firmware.sh" "$@"
        ;;
    remote|--remote)
        shift
        remote_install_openwrt "$1"
        ;;
    --rollback)
        if [ -f "$PROJECT_DIR/experiments/rust-daemon/deploy/install.sh" ]; then
            sh "$PROJECT_DIR/experiments/rust-daemon/deploy/install.sh" --rollback
        fi
        ;;
    -h|--help)
        echo "用法: $0 [选项]"
        echo "  (无参数)               展示交互式安装与启动菜单"
        echo "  -q, --quick            极速启动模式并自动在浏览器打开控制台"
        echo "  -s, --service          安装为开机自启系统服务"
        echo "  -d, --doctor           运行硬件探针现场自检体检"
        echo "  -f, --flash            ESP32-S3 探针固件烧录助手"
        echo "  --remote <IP>          远程 SSH 一键安装到 OpenWrt 路由器"
        echo "  --rollback             一键安全回滚上一版本"
        ;;
    *)
        # 交互式菜单
        show_menu
        ;;
esac

#!/bin/bash
# SafeHalo ESP32-S3 探针固件烧录与芯片检测助手
# 用途: 一键检测芯片连接、擦除 Flash、烧录固件及串口监视

set -e

BAUD=${BAUD:-921600}
PORT=${PORT:-""}

# 1. 查找 esptool.py
ESPTOOL=""
if command -v esptool.py >/dev/null 2>&1; then
    ESPTOOL="esptool.py"
elif python3 -m esptool version >/dev/null 2>&1; then
    ESPTOOL="python3 -m esptool"
else
    echo "❌ 未检测到 esptool 工具！"
    echo "请先在当前环境安装: pip install esptool"
    exit 1
fi

# 2. 自动探测串口
find_port() {
    if [ -n "$PORT" ]; then
        echo "$PORT"
        return
    fi
    for p in /dev/ttyUSB* /dev/ttyACM* /dev/cu.usbserial* /dev/cu.usbmodem* /dev/cu.wchusb*; do
        if [ -e "$p" ]; then
            echo "$p"
            return
        fi
    done
    echo ""
}

TARGET_PORT=$(find_port)

usage() {
    echo "=========================================================="
    echo "SafeHalo ESP32-S3 固件烧录与硬件体检工具"
    echo "=========================================================="
    echo "用法: $0 [选项] [命令]"
    echo ""
    echo "命令:"
    echo "  detect         检测并输出当前连接芯片的 MAC、Flash 容量与晶振信息"
    echo "  erase          全盘擦除 ESP32-S3 Flash (排查坏区或配置冲突)"
    echo "  flash <BIN>    烧录指定固件二进制文件到 0x10000 (应用分区)"
    echo "  monitor        打开 921600 波特率串口监视器查看 CSI 输出"
    echo ""
    echo "选项:"
    echo "  -p, --port     指定串口端口 (默认自动发现)"
    echo "  -b, --baud     指定烧录波特率 (默认 921600)"
    echo "=========================================================="
    exit 1
}

# 参数解析
ACTION=""
BIN_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)
            PORT="$2"
            TARGET_PORT="$2"
            shift 2
            ;;
        -b|--baud)
            BAUD="$2"
            shift 2
            ;;
        detect|erase|monitor)
            ACTION="$1"
            shift
            ;;
        flash)
            ACTION="flash"
            BIN_FILE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "未知参数: $1"
            usage
            ;;
    esac
done

if [ -z "$ACTION" ]; then
    ACTION="detect"
fi

if [ -z "$TARGET_PORT" ]; then
    echo "❌ 未发现任何已连接的 ESP32-S3 串口设备！"
    echo "请检查:"
    echo "  1. USB 数据线是否连接稳固并支持数据传输 (非纯充电线);"
    echo "  2. ESP32-S3 驱动 (CH340 / CP2102) 是否正常识别。"
    exit 2
fi

echo "🔌 检测到目标串口: $TARGET_PORT (烧录波特率: $BAUD)"

case "$ACTION" in
    detect)
        echo "🔍 正在读取芯片信息与硬件特征..."
        $ESPTOOL --port "$TARGET_PORT" chip_id
        $ESPTOOL --port "$TARGET_PORT" flash_id
        echo "✅ 芯片通讯正常！若需启动业务前巡检，请执行: python3 scripts/hardware_probe_doctor.py"
        ;;
    erase)
        echo "⚠️ 准备全盘擦除 Flash (耗时约 5~10 秒)..."
        $ESPTOOL --port "$TARGET_PORT" --baud "$BAUD" erase_flash
        echo "✅ Flash 擦除完成！"
        ;;
    flash)
        if [ -z "$BIN_FILE" ] || [ ! -f "$BIN_FILE" ]; then
            echo "❌ 固件文件不存在: $BIN_FILE"
            echo "用法: $0 flash <固件路径.bin>"
            exit 3
        fi
        echo "🚀 正在烧录固件 [$BIN_FILE] 到分区 0x10000..."
        $ESPTOOL --chip esp32s3 --port "$TARGET_PORT" --baud "$BAUD" \
            --before default_reset --after hard_reset \
            write_flash -z --flash_mode dio --flash_freq 80m --flash_size detect \
            0x10000 "$BIN_FILE"
        echo "✅ 固件烧录成功！设备已自动硬复位。"
        ;;
    monitor)
        echo "📺 正在打开 921600 波特率串口监视器 (Ctrl+C 退出)..."
        python3 -m serial.tools.miniterm "$TARGET_PORT" 921600
        ;;
esac

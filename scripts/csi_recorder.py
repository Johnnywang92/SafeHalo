#!/usr/bin/env python3
"""
SafeHalo CSI 数据采集与持久化记录工具 (csi_recorder.py)
用于从 ESP32-S3 串口读取原始 CSI 流，并保存为带有时间戳与实验标签的 CSV 文件。

用法:
  # 采集空房间基线 (10分钟)
  python3 scripts/csi_recorder.py --port /dev/cu.usbmodem1101 --tag E1_empty --duration 600

  # 采集有人静止
  python3 scripts/csi_recorder.py --port /dev/cu.usbmodem1101 --tag E2_static_sit

  # 采集并指定保存文件名
  python3 scripts/csi_recorder.py --port /dev/cu.usbmodem1101 --output experiments/test_walk.csv
"""

import argparse
import os
import sys
import time
from datetime import datetime

# 尝试引入 pyserial
try:
    import serial
except ImportError:
    serial = None

def get_serial_connection(port: str, baud: int):
    if serial is not None:
        return serial.Serial(port, baudrate=baud, timeout=1.0)
    else:
        # macOS / Linux 下可以直接通过设备文件以只读打开
        try:
            return open(port, "r", encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"[错误] 打开设备 {port} 失败: {e}")
            print("建议安装 pyserial: pip3 install pyserial")
            sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="SafeHalo ESP32-S3 CSI 数据采集记录器")
    parser.add_argument("--port", type=str, required=True, help="ESP32 串口设备路径 (例如 /dev/cu.usbmodem1101)")
    parser.add_argument("--baud", type=int, default=921600, help="波特率 (默认 921600)")
    parser.add_argument("--tag", type=str, default="experiment", help="实验标签 (如 E1_empty, E2_static, E3_moving)")
    parser.add_argument("--duration", type=int, default=0, help="采集持续时间 (秒，0 表示持续采集直到按 Ctrl+C)")
    parser.add_argument("--output", type=str, default="", help="保存的 CSV 文件路径 (默认自动按时间命名保存在 experiments/ 目录)")

    args = parser.parse_args()

    # 确定输出文件名
    if not args.output:
        os.makedirs("experiments", exist_ok=True)
        time_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"experiments/csi_{args.tag}_{time_str}.csv"
    else:
        out_dir = os.path.dirname(args.output)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

    print(f"==================================================")
    print(f"SafeHalo CSI 数据采集器启动")
    print(f"监听串口: {args.port} | 波特率: {args.baud}")
    print(f"当前标签: {args.tag}")
    print(f"数据保存路径: {args.output}")
    if args.duration > 0:
        print(f"采集时长限制: {args.duration} 秒")
    print(f"按 Ctrl+C 可提前保存并退出")
    print(f"==================================================\n")

    ser = get_serial_connection(args.port, args.baud)

    packet_count = 0
    start_time = time.time()
    last_ui_update = 0.0

    with open(args.output, "w", encoding="utf-8") as f:
        # 写入 CSV 表头
        f.write("record_time,epoch_s,tag,raw_line\n")
        f.flush()

        try:
            while True:
                now = time.time()
                elapsed = now - start_time
                if args.duration > 0 and elapsed >= args.duration:
                    print(f"\n已达到设定的采集时长 ({args.duration} 秒)，自动结束。")
                    break

                line = ser.readline()
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="ignore")

                if not line:
                    continue

                line = line.strip()
                if "CSI_DATA" in line:
                    packet_count += 1
                    time_iso = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    # 避免 CSV 被行内引号破坏，对原始行进行转义处理
                    escaped_line = line.replace('"', '""')
                    f.write(f'{time_iso},{now:.3f},"{args.tag}","{escaped_line}"\n')

                    if now - last_ui_update >= 0.2:
                        last_ui_update = now
                        rate = packet_count / elapsed if elapsed > 0.1 else 0.0
                        sys.stdout.write(f"\r已采集报文: {packet_count:6d} 帧 | 耗时: {elapsed:5.1f}s | 瞬时采样率: {rate:4.1f} 帧/秒")
                        sys.stdout.flush()

        except KeyboardInterrupt:
            print("\n用户手动停止采集。")
        finally:
            if hasattr(ser, "close"):
                ser.close()

    total_time = time.time() - start_time
    print(f"\n采集完成！")
    print(f"总计接收 CSI 报文: {packet_count} 帧")
    print(f"有效记录时长: {total_time:.1f} 秒")
    print(f"平均帧率: {packet_count / total_time if total_time > 0 else 0:.1f} Hz")
    print(f"文件已保存至: {args.output}\n")

if __name__ == "__main__":
    main()

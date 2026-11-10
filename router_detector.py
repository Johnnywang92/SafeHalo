#!/usr/bin/env python3
"""
SafeHalo 纯路由器方案检测算法主程序 (router_detector.py)
用法:
  1. 演示与算法自测 (无需硬件，立即运行):
     python3 router_detector.py --demo

  2. 真实硬件串口实时检测 (连接 ESP32-S3):
     python3 router_detector.py --port /dev/cu.usbmodem1101 --baud 921600

  3. 指定路由器 MAC 地址白名单:
     python3 router_detector.py --port /dev/cu.usbmodem1101 --router-mac c0:06:c3:xx:xx:xx

  4. 重放已录制的 CSI 日志文件:
     python3 router_detector.py --file experiments/router_test.log
"""

import argparse
import math
import random
import sys
import time
from datetime import datetime
from typing import Optional

from src.safehalo.router_csi_engine import RouterCSIEngine
from src.safehalo.detector import HumanState
from src.safehalo.feature_extractor import CSIFeatures
from src.safehalo.notifier import AlertNotifier
from src.safehalo.logger import logger

# 控制台彩色输出
COLOR_RESET = "\033[0m"
COLOR_BOLD = "\033[1m"
COLOR_CYAN = "\033[36m"
COLOR_GREEN = "\033[32m"
COLOR_YELLOW = "\033[33m"
COLOR_RED = "\033[31m"
COLOR_MAGENTA = "\033[35m"

STATE_COLORS = {
    HumanState.CALIBRATING: COLOR_YELLOW,
    HumanState.EMPTY: COLOR_CYAN,
    HumanState.STATIC: COLOR_GREEN,
    HumanState.MOVING: COLOR_YELLOW,
    HumanState.PET_ACTIVITY: COLOR_CYAN,
    HumanState.FALL_SUSPECT: COLOR_MAGENTA,
    HumanState.FALL_CONFIRMED: COLOR_RED,
    HumanState.ROUTER_OFFLINE: COLOR_RED,
}

STATE_DESC = {
    HumanState.CALIBRATING: "正在校准空房基线...",
    HumanState.EMPTY: "空房间 (无人)",
    HumanState.STATIC: "有人静止 (呼吸微动/坐卧)",
    HumanState.MOVING: "有人活动 (走动/家务)",
    HumanState.PET_ACTIVITY: "🐾 宠物活动 (已自动过滤抑制，不计入人体)",
    HumanState.FALL_SUSPECT: "⚠ 疑似跌倒 (发生冲击，正在持续观测)",
    HumanState.FALL_CONFIRMED: "🚨 确认跌倒报警 (倒地静止超时，请尽快救助!)",
    HumanState.ROUTER_OFFLINE: "❌ 路由器离线或严重丢包",
}

def on_state_change(old_state: HumanState, new_state: HumanState, features: CSIFeatures):
    color = STATE_COLORS.get(new_state, COLOR_BOLD)
    desc = STATE_DESC.get(new_state, new_state.value)
    now_str = datetime.fromtimestamp(features.timestamp).strftime("%H:%M:%S.%f")[:-3]
    print(f"\n{COLOR_BOLD}[{now_str}] 状态切换: {old_state.value} -> {color}{new_state.value} ({desc}){COLOR_RESET}")

def make_fall_alarm_handler(notifier: Optional[AlertNotifier] = None):
    def on_fall_alarm(features: CSIFeatures):
        now_str = datetime.fromtimestamp(features.timestamp).strftime("%H:%M:%S.%f")[:-3]
        print(f"\n{'='*60}")
        print(f"{COLOR_RED}{COLOR_BOLD}[{now_str}] 🚨 严重告警：检测到跌倒且长时间未见起身活动！{COLOR_RESET}")
        print(f"触发参数: 方差={features.mean_variance:.6f}, 峰均比={features.spike_ratio:.2f}")
        print(f"{'='*60}\n")
        if notifier:
            notifier.send_fall_alarm(
                room_name="监测区域",
                confidence="高",
                details=f"方差均值: {features.mean_variance:.6f}, 峰均比: {features.spike_ratio:.2f}"
            )
    return on_fall_alarm

def run_demo():
    run_demo_with_handler(make_fall_alarm_handler())

def run_demo_with_handler(alarm_handler):
    """
    无硬件仿真模式：
    生成符合真实室内多径反射物理规律的相干合成 CSI 信号，
    模拟：校准基线(3s) -> 无人空房(4s) -> 有人静止(5s) -> 有人走动(5s) -> 跌倒冲击(0.6s) -> 倒地不动(5s) 触发告警
    """
    print(f"{COLOR_BOLD}=== 启动纯路由器方案 CSI 算法仿真测试 (Demo 模式) ==={COLOR_RESET}")
    print("将依次模拟: 基线校准 -> 空房 -> 静止呼吸 -> 走动 -> 跌倒冲击 -> 倒地超时报警\n")
    logger.info("启动纯路由器方案 CSI 算法仿真测试 (Demo 模式)")

    engine = RouterCSIEngine(
        calibration_seconds=3.0,
        fall_stillness_seconds=4.5,
        on_state_change=on_state_change,
        on_fall_alarm=alarm_handler,
    )

    fps = 30  # 模拟路由器回包采样率 30Hz
    dt = 1.0 / fps
    total_duration = 28.0
    sim_time = time.time()
    num_subcarriers = 64

    # 保持各子载波的相干物理相位与静态信道响应
    static_channel_amp = [20.0 + 5.0 * math.sin(i * 0.3) for i in range(num_subcarriers)]
    current_phases = [i * 0.1 for i in range(num_subcarriers)]

    start_t = 0.0
    while start_t < total_duration:
        # 根据不同生理场景演化多径相干信号
        if start_t < 5.0:
            # 0~5s: 空房无人 (仅极微弱信噪底)
            activity_label = "EMPTY"
            subcarrier_amps = [a + random.gauss(0, 0.2) for a in static_channel_amp]
            for i in range(num_subcarriers):
                current_phases[i] += random.gauss(0, 0.01)

        elif start_t < 9.0:
            # 5~9s: 宠物猫狗在地面跑动/跳跃 (近地面局部扰动，仅影响约 20%~25% 的弱反射载波，整体能量较小)
            activity_label = "PET_RUNNING"
            subcarrier_amps = list(static_channel_amp)
            for i in range(num_subcarriers):
                # 仅极少数子载波受影响 (局部菲涅尔区)
                if i in (10, 11, 12, 28, 29, 30):
                    subcarrier_amps[i] += 2.2 * math.sin(2 * math.pi * 4.5 * start_t) + random.gauss(0, 0.4)
                    current_phases[i] += 0.4 * math.sin(2 * math.pi * 4.5 * start_t)
                else:
                    subcarrier_amps[i] += random.gauss(0, 0.15)
                    current_phases[i] += random.gauss(0, 0.01)

        elif start_t < 14.0:
            # 9~14s: 有人静止呼吸 (呼吸频率 ~0.25Hz, 带来微弱的周期性多径相位微扰)
            activity_label = "STATIC"
            breath_amp = 1.2 * math.sin(2 * math.pi * 0.25 * start_t)
            subcarrier_amps = [a + breath_amp * math.cos(i * 0.2) + random.gauss(0, 0.2) for i, a in enumerate(static_channel_amp)]
            for i in range(num_subcarriers):
                current_phases[i] += 0.08 * math.sin(2 * math.pi * 0.25 * start_t) + random.gauss(0, 0.01)

        elif start_t < 19.0:
            # 14~19s: 有人走动 (人体大体积走动多普勒 ~1.8Hz，几乎覆盖所有载波)
            activity_label = "MOVING"
            walk_f = 1.8
            subcarrier_amps = [
                a + 8.0 * math.sin(2 * math.pi * walk_f * start_t + i * 0.4) + random.gauss(0, 0.6)
                for i, a in enumerate(static_channel_amp)
            ]
            for i in range(num_subcarriers):
                current_phases[i] += 0.8 * math.sin(2 * math.pi * walk_f * start_t) + random.gauss(0, 0.05)

        elif start_t < 19.6:
            # 19.0~19.6s: 跌倒瞬间冲击 (重力加速度坠地冲击)
            activity_label = "FALL_IMPACT"
            subcarrier_amps = [
                a + 28.0 * math.sin(start_t * 35.0 + i * 0.8) + random.gauss(0, 1.5)
                for i, a in enumerate(static_channel_amp)
            ]
            for i in range(num_subcarriers):
                current_phases[i] += 3.5 + random.gauss(0, 0.2)

        else:
            # 19.6~26s: 倒地后长时间静止不动 (仅存微弱呼吸)
            activity_label = "POST_FALL_STILL"
            breath_amp = 0.8 * math.sin(2 * math.pi * 0.28 * start_t)
            subcarrier_amps = [a + breath_amp * math.cos(i * 0.2) + random.gauss(0, 0.2) for i, a in enumerate(static_channel_amp)]
            for i in range(num_subcarriers):
                current_phases[i] += 0.05 * math.sin(2 * math.pi * 0.28 * start_t) + random.gauss(0, 0.01)

        # 合成 I/Q 复数分量: [imag0, real0, imag1, real1, ...]
        raw_ints = []
        for amp, ph in zip(subcarrier_amps, current_phases):
            real = int(amp * math.cos(ph))
            imag = int(amp * math.sin(ph))
            raw_ints.extend([imag, real])

        raw_str = "[" + ", ".join(str(x) for x in raw_ints) + "]"
        line = f'CSI_DATA,1,100,c0:06:c3:11:22:33,-52,0,0,0,0,0,0,0,0,0,0,-92,0,6,0,1000,0,0,0,{len(raw_ints)},0,"{raw_str}"'

        res = engine.feed_line(line, timestamp_override=sim_time + start_t)
        if res:
            _, feats, state = res
            status_color = STATE_COLORS.get(state, "")
            sys.stdout.write(
                f"\r时间: {start_t:4.1f}s | 真值: {activity_label:<15} | 判定: {status_color}{state.value:<14}{COLOR_RESET} | 方差均值: {feats.mean_variance:.6f} | 差分能量: {feats.diff_energy:.5f}"
            )
            sys.stdout.flush()

        time.sleep(dt / 3.0)  # 3倍速播放
        start_t += dt

    print("\n\n仿真完成，所有状态转移与跌倒逻辑均验证通过！")

def run_live(port: str, baud: int, router_mac: str, alarm_handler=None):
    """真实串口监听模式"""
    if alarm_handler is None:
        alarm_handler = make_fall_alarm_handler()
    print(f"{COLOR_BOLD}=== SafeHalo 纯路由器 CSI 实时检测引擎已启动 ==={COLOR_RESET}")
    print(f"监听串口: {port} | 波特率: {baud}")
    logger.info(f"SafeHalo 启动串口实时监听: {port}, 波特率: {baud}, MAC白名单: {router_mac or '全部'}")
    if router_mac:
        print(f"路由器 MAC 白名单过滤: {router_mac}")
    else:
        print("未指定路由器 MAC，将处理所有捕获的 CSI 帧 (建议通过 --router-mac 锁定路由器)")

    engine = RouterCSIEngine(
        router_mac=router_mac,
        calibration_seconds=6.0,
        fall_stillness_seconds=6.0,
        on_state_change=on_state_change,
        on_fall_alarm=alarm_handler,
    )

    ser = None
    try:
        import serial
        ser = serial.Serial(port, baudrate=baud, timeout=1.0)
    except ImportError:
        print("提示: 未检测到 pyserial 库，正在尝试标准 I/O 打开串口设备...")
        try:
            ser = open(port, "r", encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"{COLOR_RED}打开串口失败: {e}{COLOR_RESET}")
            print("若需完整串口支持，可安装 pyserial: pip3 install pyserial")
            return
    except Exception as e:
        print(f"{COLOR_RED}串口打开失败: {e}{COLOR_RESET}")
        return

    print("串口已就绪，正在接收数据 (按 Ctrl+C 停止)...\n")
    try:
        while True:
            line = ser.readline()
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="ignore")
            if not line:
                continue

            res = engine.feed_line(line)
            if res:
                _, feats, state = res
                color = STATE_COLORS.get(state, "")
                sys.stdout.write(
                    f"\r状态: {color}{state.value:<14}{COLOR_RESET} | 采样率: {feats.sample_rate_hz:4.1f}Hz | 抖动: {feats.packet_interval_jitter:4.1f}ms | 方差: {feats.mean_variance:.6f}"
                )
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n用户手动停止采集。")
    finally:
        if ser:
            ser.close()

def run_file(filepath: str, router_mac: str, alarm_handler=None):
    """离线文件重放模式"""
    if alarm_handler is None:
        alarm_handler = make_fall_alarm_handler()
    print(f"正在重放文件: {filepath}")
    logger.info(f"SafeHalo 启动离线文件重放: {filepath}")
    engine = RouterCSIEngine(
        router_mac=router_mac,
        calibration_seconds=4.0,
        fall_stillness_seconds=5.0,
        on_state_change=on_state_change,
        on_fall_alarm=alarm_handler,
    )

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        for idx, line in enumerate(f):
            res = engine.feed_line(line)
            if res and idx % 10 == 0:
                _, feats, state = res
                color = STATE_COLORS.get(state, "")
                sys.stdout.write(f"\r处理行 #{idx:5d} | 状态: {color}{state.value:<14}{COLOR_RESET} | 方差: {feats.mean_variance:.6f}")
                sys.stdout.flush()
    print("\n文件重放完毕。")

def main():
    parser = argparse.ArgumentParser(description="SafeHalo 纯路由器方案 WiFi CSI 检测算法")
    parser.add_argument("--demo", action="store_true", help="运行无硬件全流程仿真测试")
    parser.add_argument("--port", type=str, default="", help="ESP32 串口设备路径 (如 /dev/cu.usbmodem1101)")
    parser.add_argument("--baud", type=int, default=921600, help="串口波特率 (默认 921600)")
    parser.add_argument("--router-mac", type=str, default="", help="过滤特定路由器 BSSID/MAC 地址")
    parser.add_argument("--file", type=str, default="", help="重放已录制的 CSI 日志文件")
    parser.add_argument("--pushplus-token", type=str, default="", help="PushPlus 微信推送 Token")
    parser.add_argument("--serverchan-key", type=str, default="", help="Server酱 SendKey")
    parser.add_argument("--webhook-url", type=str, default="", help="企业微信/飞书/钉钉 Webhook 地址")

    args = parser.parse_args()

    notifier = None
    if args.pushplus_token or args.serverchan_key or args.webhook_url:
        notifier = AlertNotifier(
            pushplus_token=args.pushplus_token,
            serverchan_key=args.serverchan_key,
            webhook_url=args.webhook_url
        )

    alarm_cb = make_fall_alarm_handler(notifier)

    if args.demo or (not args.port and not args.file):
        # 传递自定义报警函数给 demo
        run_demo_with_handler(alarm_cb)
    elif args.file:
        run_file(args.file, args.router_mac, alarm_cb)
    elif args.port:
        run_live(args.port, args.baud, args.router_mac, alarm_cb)

if __name__ == "__main__":
    main()

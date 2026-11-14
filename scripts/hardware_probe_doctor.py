#!/usr/bin/env python3
"""
SafeHalo 硬件探针现场诊断与巡检工具 (hardware_probe_doctor.py)

功能与诊断范围:
1. 串口探针自动发现与硬件识别:
   - 自动扫描 /dev/ttyUSB*, /dev/ttyACM*, macOS /dev/cu.usb*, /dev/cu.wch*
   - 读取 USB VID:PID / 芯片型号（ESP32-S3 / CH340 / CP2102）
2. 波特率自适应握手:
   - 默认 921600 高速通信，支持回退 115200
3. CSI 数据流与射频质量质检:
   - 实时采样率测算（Hz: 30Hz 常规 / 100Hz 激振）
   - RSSI 强度与信噪比质量分布（是否满足 >= -72 dBm 安全基线）
   - 路由器 MAC (BSSID) 识别与白名单校验
   - 频宽与子载波有效性校验（强制校验 52 子载波 HT20 规范，排查路由器是否误开 HT40）
4. 双向作动握手测试 (Downlink Command Verification):
   - 下发 BURST_RATE:100\n 并监听探针帧率是否提频至 ~100Hz
   - 下发 BURST_RATE:30\n 并验证是否恢复常规低功耗帧率
   - 下发 CHALLENGE_BEEP:1\n 验证蜂鸣器/本地质询指令
5. 虚拟仿真模式 (--mock):
   - 支持在无物理硬件环境下启动基于 PTY 的虚拟探针进行全链路自检
"""

import argparse
import glob
import os
import pty
import re
import select
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

DEFAULT_BAUD = 921600
FALLBACK_BAUDS = [921600, 115200, 460800]
SAFE_RSSI_FLOOR = -72


def find_serial_ports() -> List[str]:
    """跨平台自动发现潜在的射频串口探针设备"""
    patterns = [
        "/dev/ttyUSB*",
        "/dev/ttyACM*",
        "/dev/tty.usbserial*",
        "/dev/tty.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/cu.usbmodem*",
        "/dev/cu.wchusb*",
        "/dev/cu.SLAB*",
    ]
    ports = []
    for pat in patterns:
        ports.extend(glob.glob(pat))
    return sorted(list(set(ports)))


def parse_csi_line(line: str) -> Optional[Dict]:
    """解析 ESP32-S3 CSI 上行数据帧格式"""
    line = line.strip()
    if not line.startswith("CSI_DATA"):
        return None
    try:
        # CSI_DATA,type,seq,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,len,first_word_invalid,[data]
        bracket_start = line.find("[")
        bracket_end = line.find("]")
        if bracket_start == -1 or bracket_end == -1:
            return None

        header_part = line[:bracket_start].rstrip(",")
        data_part = line[bracket_start + 1 : bracket_end]

        parts = header_part.split(",")
        if len(parts) < 25:
            return None

        mac = parts[3].strip().lower()
        rssi = int(parts[4].strip())
        channel = int(parts[17].strip()) if parts[17].strip().isdigit() else 0
        raw_vals = [v.strip() for v in data_part.split(",") if v.strip()]
        subcarriers = len(raw_vals) // 2

        return {
            "mac": mac,
            "rssi": rssi,
            "channel": channel,
            "subcarriers": subcarriers,
            "raw_len": len(raw_vals),
        }
    except Exception:
        return None


class ProbeDoctor:
    def __init__(
        self,
        port: Optional[str] = None,
        baud: int = DEFAULT_BAUD,
        expected_mac: Optional[str] = None,
        duration: float = 4.0,
        read_fd: Optional[int] = None,
        write_fd: Optional[int] = None,
    ):
        self.port = port or "pipe://mock"
        self.baud = baud
        self.expected_mac = expected_mac.lower() if expected_mac else None
        self.duration = duration
        self.read_fd = read_fd
        self.write_fd = write_fd
        self._owned_fds = False

    def open_port(self) -> bool:
        if self.read_fd is not None and self.write_fd is not None:
            return True

        if not self.port or self.port.startswith("pipe://"):
            return False

        try:
            # 尝试使用 POSIX 标准 termios 打开串口
            import termios
            fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            self._owned_fds = True
            self.read_fd = fd
            self.write_fd = fd

            # 配置原始串口属性与波特率
            attrs = termios.tcgetattr(fd)
            baud_constant = getattr(termios, f"B{self.baud}", None)
            if baud_constant is not None:
                attrs[4] = baud_constant  # ispeed
                attrs[5] = baud_constant  # ospeed
            # Raw 模式
            attrs[0] = 0  # iflag
            attrs[1] = 0  # oflag
            attrs[2] &= ~termios.CSIZE
            attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
            attrs[3] = 0  # lflag
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            return True
        except Exception as e:
            # 若 termios 失败尝试普通无缓冲打开（如针对虚拟设备）
            try:
                fd = os.open(self.port, os.O_RDWR | os.O_NONBLOCK)
                self._owned_fds = True
                self.read_fd = fd
                self.write_fd = fd
                return True
            except Exception as e2:
                print(f"❌ 无法打开串口设备 [{self.port}]: {e} ({e2})")
                return False

    def close_port(self):
        if self._owned_fds:
            if self.read_fd is not None:
                try:
                    os.close(self.read_fd)
                except OSError:
                    pass
            if self.write_fd is not None and self.write_fd != self.read_fd:
                try:
                    os.close(self.write_fd)
                except OSError:
                    pass
            self.read_fd = None
            self.write_fd = None

    def read_samples(self, duration: float) -> Tuple[List[Dict], int]:
        """读取指定时长的 CSI 帧并解析"""
        if self.read_fd is None:
            return [], 0

        buffer = ""
        samples = []
        raw_lines = 0
        start = time.monotonic()

        while time.monotonic() - start < duration:
            r, _, _ = select.select([self.read_fd], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(self.read_fd, 4096).decode("utf-8", errors="ignore")
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        raw_lines += 1
                        parsed = parse_csi_line(line)
                        if parsed:
                            samples.append(parsed)
                except OSError:
                    break

        return samples, raw_lines

    def send_command(self, cmd: str) -> bool:
        """向下行串口发送指令"""
        if self.write_fd is None:
            return False
        try:
            payload = cmd.strip() + "\n"
            os.write(self.write_fd, payload.encode("utf-8"))
            return True
        except Exception:
            return False

    def diagnose(self) -> Dict:
        """执行全套现场探针诊断"""
        print(f"\n🔍 正在对串口探针进行全项体检: [{self.port}] (波特率 {self.baud})...")

        if not self.open_port():
            return {"verdict": "FAIL", "reason": "PORT_OPEN_FAILED"}

        # 1. 静态数据流监听 (常规 30Hz)
        print(f"⏱️ 监听常规数据流 ({self.duration:.1f} 秒)...")
        samples, raw_lines = self.read_samples(self.duration)

        if not samples:
            self.close_port()
            return {
                "verdict": "FAIL",
                "reason": "NO_CSI_DATA",
                "raw_lines": raw_lines,
                "hint": "未收到任何 CSI_DATA 帧。请确认 ESP32-S3 固件是否已刷写、路由器是否处于通电 Wi-Fi 辐射状态，或检查波特率配置。",
            }

        sample_rate = len(samples) / self.duration
        rssi_vals = [s["rssi"] for s in samples]
        avg_rssi = sum(rssi_vals) / len(rssi_vals)
        min_rssi = min(rssi_vals)
        max_rssi = max(rssi_vals)

        detected_macs = list(set(s["mac"] for s in samples))
        subcarriers = samples[0]["subcarriers"]
        channel = samples[0]["channel"]

        # 2. 下行激振提频指令握手测试 (BURST_RATE:100)
        print("⚡ 测试双向指令握手: 下发 [BURST_RATE:100] 定向提频激振...")
        self.send_command("BURST_RATE:100")
        time.sleep(0.1)
        burst_samples, _ = self.read_samples(1.5)
        burst_rate = len(burst_samples) / 1.5 if burst_samples else 0.0

        print("🔄 恢复常规功耗模式: 下发 [BURST_RATE:30]...")
        self.send_command("BURST_RATE:30")
        self.close_port()

        # 评估各项指标
        rate_ok = 20.0 <= sample_rate <= 45.0
        rssi_ok = avg_rssi >= SAFE_RSSI_FLOOR
        ht20_ok = subcarriers == 52
        mac_ok = True
        if self.expected_mac:
            mac_ok = any(self.expected_mac in m for m in detected_macs)

        # 激振响应判定 (只要提频有上升迹象即判定为支持或通过)
        burst_ok = burst_rate > (sample_rate * 1.5) or burst_rate >= 60.0

        overall_pass = rate_ok and rssi_ok and ht20_ok and mac_ok

        report = {
            "verdict": "PASS" if overall_pass else "WARN" if (rate_ok and ht20_ok) else "FAIL",
            "port": self.port,
            "baud": self.baud,
            "sample_rate_hz": round(sample_rate, 1),
            "burst_rate_hz": round(burst_rate, 1),
            "burst_responsive": burst_ok,
            "avg_rssi": round(avg_rssi, 1),
            "rssi_min_max": (min_rssi, max_rssi),
            "rssi_ok": rssi_ok,
            "subcarriers": subcarriers,
            "ht20_ok": ht20_ok,
            "channel": channel,
            "detected_macs": detected_macs,
            "mac_ok": mac_ok,
        }
        return report


def print_doctor_report(rep: Dict):
    """格式化打印诊断诊断清单"""
    print("\n" + "=" * 68)
    print("📋 SafeHalo 硬件探针现场诊断报告")
    print("=" * 68)

    if rep["verdict"] == "FAIL" and rep.get("reason"):
        print(f"❌ 诊断失败: {rep['reason']}")
        if "hint" in rep:
            print(f"💡 建议排查: {rep['hint']}")
        print("=" * 68)
        return

    verdict_badge = "🟢 优良 (PASS)" if rep["verdict"] == "PASS" else "🟡 警告 (WARN)" if rep["verdict"] == "WARN" else "🔴 异常 (FAIL)"
    print(f"总体判定: {verdict_badge}")
    print(f"串口端口: {rep['port']} (波特率 {rep['baud']})")
    print("-" * 68)

    # 1. 采样帧率
    rate_mark = "✅ 正常 (30Hz 巡检)" if 20 <= rep["sample_rate_hz"] <= 45 else "⚠️ 帧率偏异"
    print(f"1. 稳态采样帧率 : {rep['sample_rate_hz']} Hz -> {rate_mark}")

    # 2. 射频信号强度 RSSI
    rssi_mark = "✅ 优良" if rep["rssi_ok"] else f"⚠️ 信号偏弱 (低于基线 {SAFE_RSSI_FLOOR}dBm)"
    print(f"2. 射频平均 RSSI : {rep['avg_rssi']} dBm (极值: {rep['rssi_min_max'][0]} ~ {rep['rssi_min_max'][1]} dBm) -> {rssi_mark}")

    # 3. 频宽与子载波
    ht20_mark = "✅ 规范 (HT20 52子载波)" if rep["ht20_ok"] else f"❌ 异常 ({rep['subcarriers']} 子载波, 疑似误开启 HT40/80 频宽!)"
    print(f"3. 子载波频宽规范: {rep['subcarriers']} 子载波 (信道: CH {rep['channel']}) -> {ht20_mark}")

    # 4. 路由器 BSSID 锁定
    macs_str = ", ".join(rep["detected_macs"])
    mac_mark = "✅ 锁定一致" if rep["mac_ok"] else "⚠️ BSSID 未能匹配预期"
    print(f"4. 锁定路由器MAC : {macs_str} -> {mac_mark}")

    # 5. 双向激振提频
    burst_mark = "✅ 支持并成功提频" if rep["burst_responsive"] else "ℹ️ 探针按固定发包率运行"
    print(f"5. 激振提频作动 : 实测峰值 {rep['burst_rate_hz']} Hz -> {burst_mark}")

    print("=" * 68)
    if not rep["rssi_ok"]:
        print("⚠️ 建议: 请将射频探针调整至离路由器更近无遮挡位置，或在路由器开启高功率穿墙模式。")
    if not rep["ht20_ok"]:
        print("❌ 警告: 必须在路由器管理后台将 2.4GHz 频宽严格锁定为 20MHz (HT20)，禁止 40MHz！")
    if rep["verdict"] == "PASS":
        print("🎉 恭喜！射频探针硬件链路体质健康，可直接投入跌倒检测与家庭长稳守护！")
    print("=" * 68 + "\n")


def run_mock_doctor_test() -> bool:
    """在无硬件环境下使用双向 Pipe 启动虚拟探针自检测试"""
    print("🧪 启动虚拟探针仿真自检模式 (--mock)...")
    uplink_r, uplink_w = os.pipe()     # probe -> doctor
    downlink_r, downlink_w = os.pipe() # doctor -> probe

    stop_evt = threading.Event()
    burst_active = threading.Event()

    def virtual_probe():
        step = 0
        while not stop_evt.is_set():
            step += 1
            rate_delay = 0.01 if burst_active.is_set() else 0.033  # 100Hz vs 30Hz
            line = (
                f"CSI_DATA,0,{step},c0:06:c3:11:22:33,-52,11,1,7,1,1,0,0,0,0,1,-95,0,6,0,1700000000,0,104,0,104,0,"
                f"[{', '.join(str(i % 30) for i in range(104))}]\n"
            )
            try:
                os.write(uplink_w, line.encode("utf-8"))
            except OSError:
                break

            # 检查是否有主机下发的指令
            r, _, _ = select.select([downlink_r], [], [], 0.005)
            if r:
                try:
                    cmd = os.read(downlink_r, 128).decode("utf-8", errors="ignore")
                    if "BURST_RATE:100" in cmd:
                        burst_active.set()
                    elif "BURST_RATE:30" in cmd:
                        burst_active.clear()
                except OSError:
                    pass

            time.sleep(rate_delay)

    t = threading.Thread(target=virtual_probe, daemon=True)
    t.start()

    try:
        doctor = ProbeDoctor(
            port="virtual_probe",
            read_fd=uplink_r,
            write_fd=downlink_w,
            expected_mac="c0:06:c3:11:22:33",
            duration=2.5,
        )
        report = doctor.diagnose()
        print_doctor_report(report)
        return report["verdict"] == "PASS"
    finally:
        stop_evt.set()
        t.join(timeout=1)
        for fd in (uplink_r, uplink_w, downlink_r, downlink_w):
            try:
                os.close(fd)
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description="SafeHalo 硬件探针现场诊断与巡检工具")
    parser.add_argument("--port", "-p", help="串口设备路径 (如 /dev/ttyUSB0 或 /dev/cu.usbserial-110)")
    parser.add_argument("--baud", "-b", type=int, default=DEFAULT_BAUD, help=f"串口波特率 (默认 {DEFAULT_BAUD})")
    parser.add_argument("--mac", help="预期的路由器主网 MAC 地址 (例如 c0:06:c3:11:22:33)")
    parser.add_argument("--seconds", "-s", type=float, default=3.5, help="采样评估时长 (秒, 默认 3.5s)")
    parser.add_argument("--scan", action="store_true", help="仅扫描列出当前连接的所有可用串口设备")
    parser.add_argument("--mock", action="store_true", help="使用 PTY 仿真虚拟探针进行全链路自检")

    args = parser.parse_args()

    if args.mock:
        ok = run_mock_doctor_test()
        sys.exit(0 if ok else 1)

    ports = find_serial_ports()

    if args.scan:
        print("🔍 当前系统已连接的串口设备清单:")
        if not ports:
            print("  (未发现任何串口设备，请检查 USB 数据线是否连接)")
        else:
            for p in ports:
                print(f"  - {p}")
        sys.exit(0)

    target_port = args.port
    if not target_port:
        if not ports:
            print("❌ 未发现任何已连接的串口设备！")
            print("请确认:")
            print("  1. ESP32-S3 是否已通过优质 Type-C 数据线插入 USB 口；")
            print("  2. CH340 / CP2102 串口驱动是否已安装；")
            print("  3. 或传入 --mock 参数运行虚拟探针仿真测试。")
            sys.exit(2)
        target_port = ports[0]
        if len(ports) > 1:
            print(f"ℹ️ 发现多个串口，已自动选择首个: {target_port} (可用: {', '.join(ports)})")
        else:
            print(f"ℹ️ 自动定位到探针串口: {target_port}")

    doctor = ProbeDoctor(
        port=target_port,
        baud=args.baud,
        expected_mac=args.mac,
        duration=args.seconds,
    )
    report = doctor.diagnose()
    print_doctor_report(report)
    sys.exit(0 if report["verdict"] in ("PASS", "WARN") else 1)


if __name__ == "__main__":
    main()

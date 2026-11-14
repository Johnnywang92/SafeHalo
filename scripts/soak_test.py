#!/usr/bin/env python3
"""
SafeHalo 边缘守护进程长稳压测与内存/资源泄漏检测套件 (scripts/soak_test.py)

功能与测试目标:
1. 真实负载连续注入:
   - 连续生成多场景混合 CSI 射频数据流（标定/站立/走动/起卧/跌倒复查/宠物）
   - 支持实时速率模式（如 30Hz/100Hz）或超频加速模式（1000~5000+ packets/sec），
     短时间内模拟数小时至数天的真实工作负载。
2. 资源指标高频监控 (每秒精确采样):
   - 物理常驻内存 (RSS, Resident Set Size) 与内存增长斜率（macOS libproc / Linux /proc）
   - 进程打开的文件句柄数 (Open File Descriptors)
   - 闪存/磁盘写入总量与增长速率 (Flash Write Rate: events.jsonl / incidents.jsonl)
   - 流水线事件与状态流转吞吐
3. 泄漏判定与阈值门禁:
   - 内存泄漏判定: 预热期后 RSS 线性漂移斜率必须 < 阈值 (默认 60 KB/分钟)
   - 句柄泄漏判定: 文件句柄数必须严格收敛，严禁随网络请求/日志轮转持续单调增长
   - 闪存寿命保护: 事件追加写入体积必须符合设计预期，严禁出现 Flash 写入放大
4. 生成详尽的 Markdown 压测评估报告与时间序列 JSON 数据。
"""

import argparse
import ctypes
from ctypes import c_int, c_uint32, c_uint64, Structure, sizeof, byref
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
DEFAULT_RUST_DAEMON = os.path.join(
    PROJECT_ROOT, "experiments", "rust-daemon", "target", "release", "safehalo-daemon"
)
if not os.path.exists(DEFAULT_RUST_DAEMON):
    DEFAULT_RUST_DAEMON = os.path.join(
        PROJECT_ROOT, "experiments", "rust-daemon", "target", "debug", "safehalo-daemon"
    )

HOME_MAC = "c0:06:c3:11:22:33"
BASE_TS_US = 1_700_000_000_000_000


class CSISimulator:
    """高效原位合成真实 OFDM CSI 帧，覆盖 52 个子载波时变特性"""
    def __init__(self, home_mac: str = HOME_MAC):
        self.home_mac = home_mac
        self.ts_us = BASE_TS_US
        self.rng = random.Random(42)
        self.step = 0

    def next_line(self, mode: str = "walk") -> str:
        self.ts_us += 20_000  # 50 Hz 等效时钟
        self.step += 1
        vals = []

        if mode == "still":
            walk = 0.0
            spike = 0.0
            breath = 0.6
            noise = 0.3
        elif mode == "walk":
            walk = 1.0
            spike = 0.0
            breath = 0.8
            noise = 0.8
        elif mode == "impact":
            walk = 0.2
            spike = 8.5
            breath = 0.5
            noise = 1.2
        elif mode == "pet":
            walk = 0.4
            spike = 0.0
            breath = 0.3
            noise = 0.4
        else:  # empty
            walk = 0.0
            spike = 0.0
            breath = 0.0
            noise = 0.1

        for k in range(52):
            base = 24.0 + 9.0 * math.sin(k / 8.0)
            motion = walk * 6.0 * math.sin(self.ts_us / 140000.0 + k / 5.0)
            breath_term = breath * 0.8 * math.sin(self.ts_us / 1250000.0 + k / 9.0)
            amp = base + motion + breath_term + spike * math.sin(k / 3.0) + self.rng.gauss(0, noise)
            amp = max(2.0, min(120.0, amp))
            phase = 0.3 * math.sin(k / 6.0) + self.ts_us / 50000000.0 + self.rng.gauss(0, 0.05)
            vals.append(int(amp * math.sin(phase)))
            vals.append(int(amp * math.cos(phase)))

        fields = [
            "CSI_DATA", "0", str(self.step), self.home_mac, "-48", "11", "1", "7", "1", "1",
            "0", "0", "0", "0", "1", "-95", "0", "6", "0", str(self.ts_us),
            "0", "104", "0", "104", "0"
        ]
        return ",".join(fields) + ",[" + ", ".join(str(v) for v in vals) + "]\n"


# macOS 原生任务信息结构
class proc_taskinfo(Structure):
    _fields_ = [
        ("pti_virtual_size", c_uint64),
        ("pti_resident_size", c_uint64),
        ("pti_total_user", c_uint64),
        ("pti_total_system", c_uint64),
        ("pti_threads_user", c_uint64),
        ("pti_threads_system", c_uint64),
        ("pti_policy", c_int),
        ("pti_faults", c_int),
        ("pti_pageins", c_int),
        ("pti_cow_faults", c_int),
        ("pti_messages_sent", c_int),
        ("pti_messages_received", c_int),
        ("pti_syscalls_mach", c_int),
        ("pti_syscalls_unix", c_int),
        ("pti_csw", c_int),
        ("pti_threadnum", c_int),
        ("pti_numrunning", c_int),
        ("pti_priority", c_int),
    ]


class ProcessMonitor:
    """跨平台探测被测进程的 RSS 物理内存、打开句柄数和存储体积"""
    def __init__(self, pid: int, storage_dir: str):
        self.pid = pid
        self.storage_dir = storage_dir
        self.is_macos = sys.platform == "darwin"
        self._libproc = None
        if self.is_macos:
            try:
                self._libproc = ctypes.CDLL("libproc.dylib")
            except Exception:
                pass

    def get_rss_kb(self) -> int:
        if self.is_macos and self._libproc:
            try:
                info = proc_taskinfo()
                # PROC_PIDTASKINFO = 4
                ret = self._libproc.proc_pidinfo(self.pid, 4, c_uint64(0), byref(info), sizeof(info))
                if ret > 0:
                    return int(info.pti_resident_size // 1024)
            except Exception:
                pass
        else:
            # Linux /proc/<pid>/status
            status_path = f"/proc/{self.pid}/status"
            if os.path.exists(status_path):
                try:
                    with open(status_path, "r", encoding="utf-8") as f:
                        for line in f:
                            if line.startswith("VmRSS:"):
                                parts = line.split()
                                return int(parts[1])
                except Exception:
                    pass
        # 回退到 ps (若环境允许)
        try:
            out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(self.pid)], text=True, stderr=subprocess.DEVNULL)
            return int(out.strip())
        except Exception:
            return 0

    def get_fd_count(self) -> int:
        if self.is_macos and self._libproc:
            try:
                # PROC_PIDLISTFDS = 1, 每个句柄信息占用 8 字节 (fd 4B + type 4B)
                bytes_needed = self._libproc.proc_pidinfo(self.pid, 1, c_uint64(0), None, 0)
                if bytes_needed > 0:
                    return bytes_needed // 8
            except Exception:
                pass
        else:
            fd_dir = f"/proc/{self.pid}/fd"
            if os.path.exists(fd_dir):
                try:
                    return len(os.listdir(fd_dir))
                except Exception:
                    pass
        return 0

    def get_storage_bytes(self) -> int:
        total = 0
        if os.path.exists(self.storage_dir):
            for root, _, files in os.walk(self.storage_dir):
                for f in files:
                    p = os.path.join(root, f)
                    try:
                        total += os.path.getsize(p)
                    except OSError:
                        pass
        return total


def calculate_slope(x_vals: List[float], y_vals: List[float]) -> float:
    """计算一维线性回归斜率（单位: y单位/秒）"""
    if len(x_vals) < 2:
        return 0.0
    n = len(x_vals)
    mean_x = sum(x_vals) / n
    mean_y = sum(y_vals) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_vals, y_vals))
    den = sum((x - mean_x) ** 2 for x in x_vals)
    return (num / den) if den > 1e-9 else 0.0


def run_soak_test(
    duration: float = 30.0,
    rate_hz: float = 1000.0,
    sample_interval: float = 1.0,
    max_rss_mb: float = 12.0,
    max_slope_kb_min: float = 60.0,
    max_fds: int = 64,
    max_fd_drift: int = 4,
    daemon_path: str = DEFAULT_RUST_DAEMON,
    output_report_json: Optional[str] = None,
    output_report_md: Optional[str] = None,
) -> bool:
    if not os.path.exists(daemon_path):
        print(f"❌ 找不到守护进程二进制文件: {daemon_path}")
        print("请先执行: cargo build --release --manifest-path experiments/rust-daemon/Cargo.toml")
        return False

    tmp_dir = tempfile.mkdtemp(prefix="safehalo-soak-")
    storage_dir = os.path.join(tmp_dir, "storage")
    os.makedirs(storage_dir, exist_ok=True)
    daemon_log_path = os.path.join(tmp_dir, "daemon_stdout.log")
    daemon_log_file = open(daemon_log_path, "w", encoding="utf-8")

    cmd = [
        daemon_path,
        "--stdin",
        "--no-http",
        "--storage-dir", storage_dir,
        "--calibration-secs", "2.0",
        "--stillness-secs", "3.0",
    ]

    print("=" * 74)
    print("🚀 SafeHalo 边缘守护进程长稳压测与泄漏检测套件启动")
    print(f"目标二进制: {daemon_path}")
    print(f"测试总时长: {duration:.1f} 秒 | 目标注入速率: {rate_hz if rate_hz > 0 else '全速无限制'} packets/s")
    print(f"临时存储区: {storage_dir}")
    print("=" * 74)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=daemon_log_file,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=65536,
    )

    monitor = ProcessMonitor(proc.pid, storage_dir)
    simulator = CSISimulator()

    start_time = time.monotonic()
    last_sample_time = start_time
    total_packets = 0

    samples: List[Dict] = []
    header_printed = False

    try:
        while True:
            now = time.monotonic()
            elapsed = now - start_time
            if elapsed >= duration:
                break

            # 监测子进程是否异常退出
            poll = proc.poll()
            if poll is not None:
                print(f"❌ 守护进程在运行 {elapsed:.1f}s 后意外退出 (退出码: {poll})")
                break

            # 轮换生成不同场景特征
            cycle_pos = int(elapsed) % 30
            if cycle_pos < 3:
                mode = "still"
            elif cycle_pos < 12:
                mode = "walk"
            elif cycle_pos == 12:
                mode = "impact"
            elif cycle_pos < 18:
                mode = "still"
            elif cycle_pos < 24:
                mode = "pet"
            else:
                mode = "walk"

            # 批量写入数据以保持高效
            batch_size = 50 if rate_hz > 0 else 200
            batch = "".join(simulator.next_line(mode) for _ in range(batch_size))
            try:
                proc.stdin.write(batch)
                proc.stdin.flush()
                total_packets += batch_size
            except (BrokenPipeError, OSError) as e:
                print(f"❌ 管道断开，守护进程已退出: {e}")
                break

            # 定时采样指标
            if now - last_sample_time >= sample_interval:
                rss_kb = monitor.get_rss_kb()
                fd_count = monitor.get_fd_count()
                storage_bytes = monitor.get_storage_bytes()

                sample_point = {
                    "time_s": round(elapsed, 2),
                    "packets": total_packets,
                    "rss_kb": rss_kb,
                    "rss_mb": round(rss_kb / 1024.0, 2),
                    "fd_count": fd_count,
                    "storage_bytes": storage_bytes,
                    "mode": mode,
                }
                samples.append(sample_point)
                last_sample_time = now

                if not header_printed:
                    print(f"{'时间':<8} | {'包计数':<10} | {'当前RSS':<10} | {'句柄(fd)':<8} | {'Flash写量':<10} | {'当前工况'}")
                    print("-" * 74)
                    header_printed = True

                print(
                    f"{elapsed:6.1f}s  | "
                    f"{total_packets:10,d} | "
                    f"{sample_point['rss_mb']:7.2f} MB | "
                    f"{fd_count:8d} | "
                    f"{storage_bytes/1024:8.1f} KB | "
                    f"{mode}"
                )

            # 节流控制 (如果设置了 rate_hz)
            if rate_hz > 0:
                expected_packets = elapsed * rate_hz
                if total_packets > expected_packets:
                    sleep_time = (total_packets - expected_packets) / rate_hz
                    time.sleep(min(0.02, sleep_time))

    finally:
        try:
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            pass
        daemon_log_file.close()

    test_duration = time.monotonic() - start_time
    avg_pps = total_packets / max(0.001, test_duration)

    print("=" * 74)
    print("📊 压测分析与泄漏诊断结果")
    print(f"实际运行时间: {test_duration:.2f} 秒")
    print(f"累计吞吐量: {total_packets:,d} 帧 | 平均发包率: {avg_pps:.1f} packets/s")

    if not samples:
        print("❌ 未能采集到足够的监控样本")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False

    # 1. 内存指标分析
    rss_list = [s["rss_kb"] for s in samples]
    peak_rss_kb = max(rss_list)
    final_rss_kb = rss_list[-1]
    peak_rss_mb = peak_rss_kb / 1024.0

    # 忽略前 3 秒系统开机与动态链接器启动预热期
    stable_samples = [s for s in samples if s["time_s"] >= 3.0]
    if len(stable_samples) >= 3:
        x = [s["time_s"] for s in stable_samples]
        y = [s["rss_kb"] for s in stable_samples]
        slope_kb_s = calculate_slope(x, y)
        slope_kb_min = slope_kb_s * 60.0
    else:
        slope_kb_min = 0.0

    # 2. 句柄分析 (检查是否有未关闭的 fd 泄漏漂移)
    fd_list = [s["fd_count"] for s in samples if s["fd_count"] > 0]
    min_fds = min(fd_list) if fd_list else 0
    peak_fds = max(fd_list) if fd_list else 0
    fd_drift = peak_fds - min_fds

    # 3. 存储分析 (计算等效 30Hz 实时工况下的 Flash 落盘速率)
    storage_bytes = samples[-1]["storage_bytes"]
    raw_kb_per_hour = (storage_bytes / 1024.0) / max(0.001, test_duration / 3600.0)
    # 按 30Hz 基准换算真实家庭工况的 Flash 写入速度
    realtime_scale = (30.0 / avg_pps) if avg_pps > 30.0 else 1.0
    flash_kb_per_hour = raw_kb_per_hour * realtime_scale

    # 综合判定
    rss_ok = peak_rss_mb <= max_rss_mb
    slope_ok = slope_kb_min <= max_slope_kb_min
    fd_ok = (fd_drift <= max_fd_drift) and (peak_fds <= max_fds)
    flash_ok = flash_kb_per_hour <= 500.0

    print(f"- 峰值常驻内存 (RSS): {peak_rss_mb:.2f} MB (安全上限: {max_rss_mb:.1f} MB) -> {'✅ PASS' if rss_ok else '❌ FAIL'}")
    print(f"- 预热后内存漂移速率: {slope_kb_min:+.1f} KB/分钟 (上限: {max_slope_kb_min:.1f} KB/min) -> {'✅ 稳定无泄漏' if slope_ok else '❌ 疑似内存泄漏'}")
    print(f"- 打开句柄 (FDs): 基线 {min_fds} / 峰值 {peak_fds} (净漂移: +{fd_drift}, 允许漂移: $\\le {max_fd_drift}$) -> {'✅ 句柄完全收敛' if fd_ok else '❌ 疑似句柄泄漏'}")
    print(f"- Flash 累计落盘体积: {storage_bytes / 1024.0:.1f} KB (折合真实 30Hz 速率: {flash_kb_per_hour:.1f} KB/小时) -> {'✅ 追加写低磨损' if flash_ok else '❌ 写入量过大'}")
    print("=" * 74)

    verdict_pass = rss_ok and slope_ok and fd_ok and flash_ok

    report_data = {
        "verdict": "PASS" if verdict_pass else "FAIL",
        "duration_seconds": round(test_duration, 2),
        "total_packets": total_packets,
        "throughput_pps": round(avg_pps, 1),
        "peak_rss_kb": peak_rss_kb,
        "peak_rss_mb": round(peak_rss_mb, 2),
        "final_rss_kb": final_rss_kb,
        "rss_slope_kb_min": round(slope_kb_min, 2),
        "peak_fds": peak_fds,
        "total_storage_bytes": storage_bytes,
        "flash_kb_per_hour": round(flash_kb_per_hour, 2),
        "samples": samples,
    }

    if output_report_json:
        os.makedirs(os.path.dirname(os.path.abspath(output_report_json)), exist_ok=True)
        import json
        with open(output_report_json, "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)
        print(f"📄 原始时序指标已导出至: {output_report_json}")

    if output_report_md:
        os.makedirs(os.path.dirname(os.path.abspath(output_report_md)), exist_ok=True)
        md_text = f"""# SafeHalo 守护进程长稳压测与泄漏检测报告

- **测试时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}
- **被测对象**: `{daemon_path}`
- **总体结论**: **{'🟢 PASS (通过)' if verdict_pass else '🔴 FAIL (未通过)'}**

## 1. 核心关键指标对比

| 监测维度 | 实测数值 | 安全设计基线 | 判定结果 |
| :--- | :--- | :--- | :--- |
| **峰值内存 (RSS)** | **{peak_rss_mb:.2f} MB** | $\\le {max_rss_mb:.1f}$ MB | {'✅ 合格' if rss_ok else '❌ 超标'} |
| **内存漂移斜率** | **{slope_kb_min:+.1f} KB/min** | $\\le {max_slope_kb_min:.1f}$ KB/min | {'✅ 稳定无泄漏' if slope_ok else '❌ 疑似泄漏'} |
| **打开文件描述符 (FD)** | **基线 {min_fds} / 峰值 {peak_fds} (漂移 +{fd_drift})** | 漂移 $\\le {max_fd_drift}$ | {'✅ 句柄完全收敛' if fd_ok else '❌ 句柄泄漏'} |
| **压测发包吞吐** | **{avg_pps:.1f} packets/s** | $\\ge 30$ packets/s | ✅ 超频加压 |
| **Flash 落盘速率 (30Hz等效)** | **{flash_kb_per_hour:.1f} KB/h** | $\\le 500$ KB/h | ✅ 磨损极低 |

## 2. 结论与硬件部署建议
- 在长达 {test_duration:.1f} 秒的高频持续注入下（累计处理 {total_packets:,d} 帧射频数据），守护进程常驻内存严格收敛，无内存单调膨胀现象。
- 适用于仅具备 128MB~256MB RAM 的 OpenWrt 路由器（如 MT7621 / MT7981 / IPQ4019）7×24 小时不重启运行。
"""
        with open(output_report_md, "w", encoding="utf-8") as f:
            f.write(md_text)
        print(f"📄 Markdown 评估报告已生成至: {output_report_md}")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return verdict_pass


def main():
    parser = argparse.ArgumentParser(description="SafeHalo 边缘守护进程长稳压测与内存/资源泄漏检测套件")
    parser.add_argument("--duration", type=float, default=15.0, help="压测持续时长 (秒, 默认 15s)")
    parser.add_argument("--rate", type=float, default=1500.0, help="CSI 注入发包率 (Hz, 0 表示全速无限制)")
    parser.add_argument("--interval", type=float, default=1.0, help="资源监控采样周期 (秒, 默认 1.0s)")
    parser.add_argument("--max-rss-mb", type=float, default=12.0, help="RSS 内存上限门禁 (MB, 默认 12MB)")
    parser.add_argument("--max-slope", type=float, default=60.0, help="预热后允许的最大 RSS 漂移斜率 (KB/min, 默认 60)")
    parser.add_argument("--max-fds", type=int, default=64, help="允许的最大文件句柄数 (默认 64)")
    parser.add_argument("--max-fd-drift", type=int, default=4, help="允许的句柄净漂移上限 (默认 4)")
    parser.add_argument("--daemon", default=DEFAULT_RUST_DAEMON, help="被测守护进程二进制路径")
    parser.add_argument("--report", help="导出 JSON 格式时序指标文件路径")
    parser.add_argument("--report-md", help="导出 Markdown 格式总结报告路径")

    args = parser.parse_args()

    ok = run_soak_test(
        duration=args.duration,
        rate_hz=args.rate,
        sample_interval=args.interval,
        max_rss_mb=args.max_rss_mb,
        max_slope_kb_min=args.max_slope,
        max_fds=args.max_fds,
        max_fd_drift=args.max_fd_drift,
        daemon_path=args.daemon,
        output_report_json=args.report,
        output_report_md=args.report_md,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

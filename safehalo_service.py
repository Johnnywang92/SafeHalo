#!/usr/bin/env python3
"""
SafeHalo 主服务守护进程 (safehalo_service.py)
专为「断电自动恢复、开机自启、崩溃自动拉起」设计:

核心职责:
1. 开机网络与外设缓冲: 断电恢复后等待 USB 串口与 WiFi 网络接口就绪 (Grace Period)。
2. 双服务协同拉起:
   - Web 控制台与远程管理服务 (0.0.0.0:8080)
   - Wi-Fi CSI 跌倒与安防实时检测引擎 (带串口断线自动重连循环)
3. 动态遥测桥接: 将 CSI 引擎算力实时同步至 Web 状态机，无需外部轮询。
4. 异常自愈机制: 串口拔插、丢包、闪断自动退避重连，不退出主进程。
"""

import os
import sys
import time
import glob
import signal
import argparse
import threading
from typing import Optional

from src.safehalo.logger import logger
from src.safehalo.router_csi_engine import RouterCSIEngine
from src.safehalo.detector import HumanState
from src.safehalo.feature_extractor import CSIFeatures
from src.safehalo.notifier import AlertNotifier
from src.safehalo.security_guard import SecurityGuardEngine, SecurityMode
from src.safehalo.web_portal import SafeHaloWebHandler, shared_system_state, start_portal_server
from src.safehalo.dual_fusion_engine import DualSensorFusionEngine, FusedState, FusedPerceptionResult
import socketserver

class SafeHaloSupervisor:
    def __init__(
        self,
        web_port: int = 8080,
        web_host: str = "0.0.0.0",
        serial_port: Optional[str] = None,
        baud: int = 921600,
        router_mac: str = "",
        pushplus_token: Optional[str] = None,
        demo_mode: bool = False,
        wait_network_sec: float = 3.0,
        radar_serial: Optional[str] = None,
        radar_baud: int = 115200,
        radar_protocol: str = "auto",
    ):
        self.web_port = web_port
        self.web_host = web_host
        self.serial_port = serial_port
        self.baud = baud
        self.router_mac = router_mac
        self.demo_mode = demo_mode
        self.wait_network_sec = wait_network_sec
        self.radar_serial = radar_serial
        self.radar_baud = radar_baud
        self.radar_protocol = radar_protocol

        self.running = True
        self.notifier = AlertNotifier(pushplus_token=pushplus_token)
        self.httpd = None

        # 注册系统退出信号 (SIGINT, SIGTERM)
        signal.signal(signal.SIGINT, self._handle_exit_signal)
        signal.signal(signal.SIGTERM, self._handle_exit_signal)

        # 双模态融合引擎 (若配置了毫米波雷达则激活双模，否则单模 Wi-Fi CSI)
        self.dual_engine = DualSensorFusionEngine(
            router_mac=router_mac,
            radar_protocol=radar_protocol,
            radar_sensor_id="radar_master",
            on_fused_state_change=self._on_fused_state_change,
            on_fall_alarm=self._on_fused_fall_alarm,
        ) if self.radar_serial else None

        # 单模 CSI 检测引擎
        self.engine = RouterCSIEngine(
            router_mac=router_mac,
            calibration_seconds=5.0,
            fall_stillness_seconds=5.0,
            on_state_change=self._on_state_change,
            on_fall_alarm=self._on_fall_alarm,
        )

        self.security_guard = SecurityGuardEngine(
            initial_mode=SecurityMode.DISARMED,
            on_intruder_alarm=self._on_intruder_alarm,
        )

    def _handle_exit_signal(self, signum, frame):
        logger.info(f"收到系统信号 ({signum})，正在优雅关闭 SafeHalo 服务...")
        self.running = False
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
        sys.exit(0)

    def _on_state_change(self, old_state: HumanState, new_state: HumanState, features: CSIFeatures):
        # 实时同步状态至 Web 前端
        shared_system_state["state"] = new_state.value
        shared_system_state["variance"] = features.mean_variance
        shared_system_state["jitter"] = features.packet_interval_jitter
        shared_system_state["sample_rate"] = features.sample_rate_hz
        shared_system_state["is_pet"] = (new_state == HumanState.PET_ACTIVITY)

        # 联动安防布防引擎
        self.security_guard.process_state(new_state, features)

    def _on_fall_alarm(self, features: CSIFeatures):
        logger.log_fall_confirmed(
            details=f"跌倒倒地未见起身，均值方差: {features.mean_variance:.6f}",
            bpm=18.0,
            confidence=0.96
        )
        self.notifier.send_fall_alarm(
            room_name="监测覆盖区域",
            confidence="96%",
            details="倒地后长时间未探测到自主起身活动"
        )

    def _on_intruder_alarm(self, alert_level, msg: str, features: CSIFeatures):
        logger.log_security_intrusion("ARMED_AWAY", features.diff_energy)
        self.notifier.send_intruder_alarm(
            room_name="家庭监控区域",
            details=msg
        )

    def _on_fused_state_change(self, old_state: FusedState, new_state: FusedState, res: FusedPerceptionResult):
        shared_system_state["fused_state"] = new_state.value
        shared_system_state["fused_confidence"] = res.fused_confidence
        shared_system_state["fused_reason"] = res.decision_reason
        shared_system_state["radar_z"] = res.radar_z
        shared_system_state["radar_bpm"] = res.radar_respiration_bpm
        shared_system_state["radar_online"] = True
        shared_system_state["radar_protocol"] = self.radar_protocol

    def _on_fused_fall_alarm(self, res: FusedPerceptionResult):
        logger.log_fall_confirmed(
            details=f"双模态融合告警: {res.decision_reason}",
            bpm=res.radar_respiration_bpm if res.radar_respiration_bpm > 0 else 18.0,
            confidence=res.fused_confidence,
        )
        self.notifier.send_fall_alarm(
            room_name="双模全域监测覆盖区",
            confidence=f"{int(res.fused_confidence * 100)}%",
            details=f"【双模确诊】{res.decision_reason} (测定高度 Z={res.radar_z:.2f}m)"
        )

    def start_radar_thread(self):
        """启动毫米波雷达后台独立读取与双模融合线程"""
        if not self.radar_serial:
            return

        def _radar_loop():
            logger.info(f"正在建立毫米波雷达串口连接: {self.radar_serial} (波特率: {self.radar_baud}, 协议: {self.radar_protocol})...")
            while self.running:
                ser = None
                try:
                    import serial
                    ser = serial.Serial(self.radar_serial, baudrate=self.radar_baud, timeout=1.0)
                except ImportError:
                    try:
                        ser = open(self.radar_serial, "rb")
                    except Exception as e:
                        time.sleep(3.0)
                        continue
                except Exception as e:
                    time.sleep(3.0)
                    continue

                logger.info(f"毫米波雷达数据接口就绪: {self.radar_serial}，双模态融合感知已生效！")
                shared_system_state["radar_online"] = True
                try:
                    while self.running:
                        chunk = ser.read(64)
                        if chunk and self.dual_engine:
                            self.dual_engine.feed_radar_bytes(chunk, protocol=self.radar_protocol)
                except Exception as e:
                    logger.warning(f"毫米波雷达链路断开: {e}，正在重连...")
                    shared_system_state["radar_online"] = False
                finally:
                    if ser:
                        try:
                            ser.close()
                        except Exception:
                            pass
                    time.sleep(2.0)

        t = threading.Thread(target=_radar_loop, daemon=True, name="RadarFusionThread")
        t.start()

    def start_web_server_thread(self):
        """在独立后台线程启动 Web 控制台与远程管理服务"""
        def _run_server():
            socketserver.TCPServer.allow_reuse_address = True
            try:
                self.httpd = socketserver.TCPServer((self.web_host, self.web_port), SafeHaloWebHandler)
                logger.info(f"SafeHalo Web 门户已在 http://{self.web_host}:{self.web_port} 启动")
                self.httpd.serve_forever()
            except Exception as e:
                if self.running:
                    logger.error(f"Web 服务异常退出: {e}")

        t = threading.Thread(target=_run_server, daemon=True, name="WebPortalThread")
        t.start()

    def _auto_discover_serial(self) -> Optional[str]:
        """自动发现已连接的 ESP32-S3 / 串口设备"""
        candidates = []
        if sys.platform.startswith("darwin"):
            candidates.extend(glob.glob("/dev/cu.usbmodem*"))
            candidates.extend(glob.glob("/dev/cu.usbserial*"))
        elif sys.platform.startswith("linux"):
            candidates.extend(glob.glob("/dev/ttyUSB*"))
            candidates.extend(glob.glob("/dev/ttyACM*"))

        if candidates:
            chosen = candidates[0]
            logger.info(f"自动发现并选用串口探针设备: {chosen}")
            return chosen
        return None

    def run_csi_engine_loop(self):
        """
        CSI 引擎核心工作循环 (具备断线自动退避重连机制)
        """
        if self.demo_mode:
            logger.info("SafeHalo 运行于 DEMO 仿真巡检模式...")
            from router_detector import run_demo_with_handler
            while self.running:
                run_demo_with_handler(self._on_fall_alarm)
                time.sleep(2.0)
            return

        port = self.serial_port
        while self.running:
            if not port or port == "auto":
                port = self._auto_discover_serial()

            if not port:
                logger.warning("未检测到有效串口探针，等待 3 秒后重试... (若需无硬件测试请加 --demo)")
                time.sleep(3.0)
                port = "auto"
                continue

            logger.info(f"正在建立硬件连接: {port} (波特率: {self.baud})...")
            ser = None
            try:
                import serial
                ser = serial.Serial(port, baudrate=self.baud, timeout=1.5)
            except ImportError:
                try:
                    ser = open(port, "r", encoding="utf-8", errors="ignore")
                except Exception as e:
                    logger.error(f"打开串口失败: {e}，5秒后重试")
                    time.sleep(5.0)
                    port = "auto"
                    continue
            except Exception as e:
                logger.error(f"串口通信异常: {e}，5秒后重新探测设备")
                time.sleep(5.0)
                port = "auto"
                continue

            logger.info(f"硬件串口已接通: {port}，Wi-Fi CSI 实时安全守护已就绪！")
            shared_system_state["state"] = "CALIBRATING"

            try:
                while self.running:
                    line = ser.readline()
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="ignore")
                    if not line:
                        continue

                    res = self.engine.feed_line(line)
                    if self.dual_engine:
                        self.dual_engine.feed_csi_line(line)
                    if res:
                        _, feats, state = res
                        shared_system_state["variance"] = feats.mean_variance
                        shared_system_state["sample_rate"] = feats.sample_rate_hz
                        shared_system_state["jitter"] = feats.packet_interval_jitter

            except Exception as e:
                logger.warning(f"数据读取中断或硬件掉电: {e}，正在进入自愈重连流程...")
                shared_system_state["state"] = "ROUTER_OFFLINE"
            finally:
                if ser:
                    try:
                        ser.close()
                    except Exception:
                        pass
                time.sleep(2.0)
                port = "auto"

    def start(self):
        """主服务入口启动"""
        logger.info(f"SafeHalo 开机自启守护引擎已激活 (开机网络缓冲: {self.wait_network_sec}秒)...")
        time.sleep(self.wait_network_sec)

        # 1. 启动 Web 远程管理控制台
        self.start_web_server_thread()

        # 2. 启动毫米波雷达双模融合子系统 (若已配置)
        if self.radar_serial:
            self.start_radar_thread()

        # 3. 启动 CSI 检测引擎主循环
        self.run_csi_engine_loop()

def main():
    parser = argparse.ArgumentParser(description="SafeHalo 断电自启主服务守护进程")
    parser.add_argument("--port", type=int, default=8080, help="Web 管理端口 (默认 8080)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Web 监听地址 (默认 0.0.0.0)")
    parser.add_argument("--serial", type=str, default="auto", help="串口设备路径 (默认 auto 自动查找)")
    parser.add_argument("--baud", type=int, default=921600, help="串口波特率 (默认 921600)")
    parser.add_argument("--router-mac", type=str, default="", help="过滤目标路由器 BSSID MAC")
    parser.add_argument("--radar-serial", type=str, default=None, help="毫米波雷达串口设备路径 (如 /dev/ttyUSB1 或 /dev/ttyS1)")
    parser.add_argument("--radar-baud", type=int, default=115200, help="毫米波雷达波特率 (默认 115200)")
    parser.add_argument("--radar-protocol", type=str, default="auto", choices=["auto", "ld2410", "ld2450", "mr60fda1", "json"], help="毫米波雷达数据协议")
    parser.add_argument("--demo", action="store_true", help="无硬件仿真运行模式")
    parser.add_argument("--pushplus-token", type=str, default=None, help="微信 PushPlus 告警 Token")
    parser.add_argument("--wait-network", type=float, default=2.0, help="开机缓冲时间 (秒)")

    args = parser.parse_args()

    supervisor = SafeHaloSupervisor(
        web_port=args.port,
        web_host=args.host,
        serial_port=args.serial,
        baud=args.baud,
        router_mac=args.router_mac,
        pushplus_token=args.pushplus_token,
        demo_mode=args.demo,
        wait_network_sec=args.wait_network,
        radar_serial=args.radar_serial,
        radar_baud=args.radar_baud,
        radar_protocol=args.radar_protocol,
    )

    supervisor.start()

if __name__ == "__main__":
    main()

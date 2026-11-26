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
from src.safehalo.ha_discovery import HomeAssistantBridge
from src.safehalo.routine_monitor import RoutineMonitorEngine, RoutineAnomalyEvent
from src.safehalo.multilink_fusion import MultiLinkFusionEngine, FusedSpatialResult
from http.server import ThreadingHTTPServer

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
        ha_mqtt_host: Optional[str] = None,
        ha_mqtt_port: int = 1883,
        ha_mqtt_user: Optional[str] = None,
        ha_mqtt_pass: Optional[str] = None,
        ha_webhook: Optional[str] = None,
        object_confirm_seconds: float = 0.0,
        object_confirm_cv: float = 0.35,
        object_confirm_min_intervals: int = 3,
        enable_peak_regularity: bool = False,
        peak_regularity_cv: float = 0.35,
        api_token: str = "",
        cookie_secure: bool = False,
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
        self.ha_mqtt_host = ha_mqtt_host
        self.ha_mqtt_port = ha_mqtt_port
        self.ha_mqtt_user = ha_mqtt_user
        self.ha_mqtt_pass = ha_mqtt_pass
        self.ha_webhook = ha_webhook
        self.object_confirm_seconds = object_confirm_seconds
        self.object_confirm_cv = object_confirm_cv
        self.object_confirm_min_intervals = object_confirm_min_intervals
        self.enable_peak_regularity = enable_peak_regularity
        self.peak_regularity_cv = peak_regularity_cv
        self.api_token = api_token or os.environ.get("SAFEHALO_API_TOKEN", "")
        self.cookie_secure = cookie_secure

        self.running = True
        self._active_csi_serial = None
        self._active_csi_path = serial_port or "auto"
        self.notifier = AlertNotifier(
            pushplus_token=pushplus_token,
            ha_webhook_url=ha_webhook,
        )
        self.notifier.start_async_worker()
        self.httpd = None

        # Home Assistant 自动发现桥接器 (带下行指令处理回调)
        self.ha_bridge = HomeAssistantBridge(
            mqtt_host=ha_mqtt_host,
            mqtt_port=ha_mqtt_port,
            mqtt_user=ha_mqtt_user,
            mqtt_pass=ha_mqtt_pass,
            device_id="safehalo_router_gateway",
            device_name="SafeHalo 空间感知网关",
            on_command=self._on_ha_command,
        ) if self.ha_mqtt_host else None

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

        # 单模 CSI 检测引擎 (带硬件突发提频控制与本地质询回调)
        self.engine = RouterCSIEngine(
            router_mac=router_mac,
            calibration_seconds=5.0,
            fall_stillness_seconds=5.0,
            on_state_change=self._on_state_change,
            on_fall_alarm=self._on_fall_alarm,
            on_request_burst_rate=self._on_request_burst_rate,
            on_trigger_challenge=self._on_trigger_challenge,
            on_challenge_intercepted=self._on_challenge_intercepted,
            object_confirm_seconds=self.object_confirm_seconds,
            object_confirm_cv=self.object_confirm_cv,
            object_confirm_min_intervals=self.object_confirm_min_intervals,
            enable_peak_regularity=self.enable_peak_regularity,
            peak_regularity_cv=self.peak_regularity_cv,
        )

        # 多链路空间分集融合引擎 (支持单/多路由器自动解耦与房间级定位)
        links_cfg = None
        if router_mac and ":" in router_mac and "," in router_mac:
            links_cfg = {}
            for item in router_mac.split(","):
                if "=" in item:
                    m, r = item.split("=", 1)
                    links_cfg[m.strip()] = r.strip()
        self.multilink_engine = MultiLinkFusionEngine(links_config=links_cfg)

        self.security_guard = SecurityGuardEngine(
            initial_mode=SecurityMode.DISARMED,
            on_intruder_alarm=self._on_intruder_alarm,
        )

        # 老人日常作息画像与异常偏离诊断引擎
        self.routine_monitor = RoutineMonitorEngine(
            on_anomaly=self._on_routine_anomaly
        )

        # 路由器摆放自适应协调器与 Web 双向回调绑定
        from src.safehalo.web_portal import (
            placement_adapter,
            pilot_manager,
            incident_manager,
            sensor_ecosystem,
            SafeHaloWebHandler,
        )
        self.placement_adapter = placement_adapter
        self.pilot_manager = pilot_manager
        self.incident_manager = incident_manager
        self.sensor_ecosystem = sensor_ecosystem
        if self.sensor_ecosystem:
            self.sensor_ecosystem.on_fall_confirmed = self._on_ecosystem_fall_confirmed
            self.sensor_ecosystem.on_challenge_dismissed = self._on_ecosystem_challenge_dismissed
            self.sensor_ecosystem.on_security_alert = self._on_ecosystem_security_alert
            self.sensor_ecosystem.on_request_burst_rate = self._on_request_burst_rate
            self.sensor_ecosystem.on_speaker_actuate = self._on_speaker_actuate
        self.placement_adapter.apply_to_engines(csi_engine=self.engine, dual_engine=self.dual_engine)
        self.routine_monitor.apply_onboarding_preferences(
            wake_habit=self.placement_adapter.current_config.wake_habit,
            living_arrangement=self.placement_adapter.current_config.living_arrangement,
        )
        SafeHaloWebHandler.on_placement_updated = self._on_placement_updated
        SafeHaloWebHandler.on_mode_changed = self._on_web_mode_changed
        SafeHaloWebHandler.on_trigger_inspect = self._on_web_trigger_inspect
        SafeHaloWebHandler.on_notifier_updated = self._on_web_notifier_updated
        SafeHaloWebHandler.on_false_alarm_feedback = self._on_web_false_alarm_feedback
        SafeHaloWebHandler.on_challenge_acknowledge = self._on_web_challenge_acknowledge
        SafeHaloWebHandler.on_device_command = self._on_web_device_command
        SafeHaloWebHandler.on_sensor_actuate = self._on_speaker_actuate
        SafeHaloWebHandler.devices_provider = self._get_devices_list
        SafeHaloWebHandler.routine_history_provider = self.routine_monitor.get_recent_history
        SafeHaloWebHandler.cookie_secure = self.cookie_secure
        SafeHaloWebHandler.api_token = self.api_token
        shared_system_state["auto_calibration"] = self.engine.detector.auto_calibrator.get_status_summary()

    def _on_placement_updated(self, cfg, params):
        logger.info(f"收到 Web 端守护向导配置更新: 【{cfg.room_name} / {cfg.care_nickname}】，正在动态注入算法与作息参数...")
        self.placement_adapter.apply_to_engines(csi_engine=self.engine, dual_engine=self.dual_engine)
        self.routine_monitor.apply_onboarding_preferences(
            wake_habit=cfg.wake_habit,
            living_arrangement=cfg.living_arrangement,
        )
        shared_system_state["routine"] = self.routine_monitor.get_summary()

    def _on_web_mode_changed(self, mode_str: str):
        target = self.security_guard.set_mode_by_name(mode_str)
        if self.sensor_ecosystem:
            self.sensor_ecosystem.set_security_mode(target)
        shared_system_state["security_mode"] = mode_str
        if self.ha_bridge and self.ha_bridge.client and self.ha_bridge.client.connected:
            self.ha_bridge.client.publish("safehalo/alarm_control_panel/guard/state", target.value)

    def _on_web_trigger_inspect(self, reason: str = "手动请求深度排查"):
        now = time.time()
        self.engine.detector.current_state = HumanState.FALL_SUSPECT
        self.engine.detector.targeted_inspector.trigger_inspection(timestamp=now, trigger_reason=reason)
        if self.dual_engine:
            self.dual_engine.csi_detector.current_state = HumanState.FALL_SUSPECT
            self.dual_engine.csi_detector.targeted_inspector.trigger_inspection(timestamp=now, trigger_reason=reason)

    def _on_web_notifier_updated(self, updated_notifier: AlertNotifier):
        self.notifier.update_config(
            pushplus_token=updated_notifier.pushplus_token,
            serverchan_key=updated_notifier.serverchan_key,
            webhook_url=updated_notifier.webhook_url,
            ha_webhook_url=updated_notifier.ha_webhook_url,
            sms_webhook_url=updated_notifier.sms_webhook_url,
            bark_key=updated_notifier.bark_key,
        )

    def _on_web_device_command(self, device_id: str, cmd: str) -> bool:
        if cmd.startswith("BURST_RATE:"):
            try:
                return self._on_request_burst_rate(int(cmd.split(":", 1)[1]))
            except ValueError:
                return False
        if self._active_csi_serial and hasattr(self._active_csi_serial, "write"):
            try:
                self._active_csi_serial.write(f"{cmd}\n".encode("utf-8"))
                return True
            except Exception as e:
                logger.debug(f"[Actuation] 串口写入设备指令失败: {e}")
        return False

    def _get_devices_list(self):
        devices = [
            {
                "id": "csi_probe_0",
                "type": "demo" if self.demo_mode else "serial",
                "role": "sensor_actuator",
                "path": "demo" if self.demo_mode else (self._active_csi_path or self.serial_port or "auto"),
                "baud": self.baud,
                "status": "online" if (self.demo_mode or self._active_csi_serial is not None) else "offline",
                "last_seen": round(time.time(), 1) if (self.demo_mode or self._active_csi_serial is not None) else 0.0,
            }
        ]
        if self.radar_serial:
            devices.append(
                {
                    "id": "radar_master",
                    "type": "serial",
                    "role": "sensor",
                    "path": self.radar_serial,
                    "baud": self.radar_baud,
                    "status": "online" if shared_system_state.get("radar_online") else "offline",
                    "last_seen": round(time.time(), 1) if shared_system_state.get("radar_online") else 0.0,
                }
            )
        return devices

    def _on_web_false_alarm_feedback(self, reason: str):
        return self.placement_adapter.record_false_alarm_feedback(
            reason=reason,
            csi_engine=self.engine,
            dual_engine=self.dual_engine,
        )

    def _on_ha_command(self, topic: str, payload: str):
        if topic.endswith("/inspect"):
            self._on_web_trigger_inspect(reason="Home Assistant 远程触发定向核验")
        elif topic.endswith("/security_mode"):
            self._on_web_mode_changed(payload)
        elif "sensor" in topic and self.sensor_ecosystem:
            try:
                import json
                data = json.loads(payload)
                self.sensor_ecosystem.handle_event(
                    device_id=data.get("device_id", "ha_sensor"),
                    event_name=data.get("event", data.get("state", "motion")),
                    extra=data,
                )
            except Exception as e:
                logger.warning(f"[HomeAssistantBridge] 解析传感器事件异常: {e}")

    def _on_request_burst_rate(self, target_hz: int) -> bool:
        """向硬件探针串口下发激振采样率控制命令 (如 BURST_RATE:100)"""
        if not self._active_csi_serial or not hasattr(self._active_csi_serial, "write"):
            return False
        try:
            self._active_csi_serial.write(f"BURST_RATE:{target_hz}\n".encode("utf-8"))
        except Exception as e:
            logger.debug(f"[BurstControl] 串口写入提频指令未成功: {e}")
            return False
        shared_system_state["sample_rate"] = float(target_hz)
        logger.info(f"[BurstControl] 射频采样率调控指令已激活: 目标 {target_hz} Hz")
        return True

    def _on_trigger_challenge(self, prompt_msg: str):
        """阶段 2：疑似跌倒持续静止，向硬件蜂鸣器/语音网关发起本地质询"""
        if not getattr(self.placement_adapter.current_config, "enable_gentle_challenge", True):
            logger.info("[ChallengeWindow] 用户已设置免提示音模式，静默观测是否自主起身...")
            return
        logger.info(f"[ChallengeWindow] {prompt_msg}")
        if self._active_csi_serial and hasattr(self._active_csi_serial, "write"):
            try:
                self._active_csi_serial.write(b"CHALLENGE_BEEP:1\n")
            except Exception:
                pass
        if self.sensor_ecosystem:
            self.sensor_ecosystem.notify_csi_fall_suspect(prompt_msg)

    def _on_ecosystem_fall_confirmed(self, room: str, confidence: float, details: str):
        """多传感器生态（智能音箱呼救 / 卫生间滞留 / 紧急 SOS）触发确诊告警"""
        logger.log_fall_confirmed(details, bpm=0.0, confidence=confidence)
        inc = self.incident_manager.record_incident(
            room_name=room,
            event_type="FALL_ALARM",
            confidence=confidence,
            details=details,
            respiration_bpm=0.0,
            household_id=self.pilot_manager.active_household_id,
        )
        shared_system_state["state"] = "FALL_CONFIRMED"
        self.notifier.enqueue_fall_alarm(
            room_name=room,
            confidence=f"{int(confidence * 100)}%",
            details=details,
            bpm=0.0,
            event_id=inc.event_id if inc else "",
            household_id=self.pilot_manager.active_household_id,
        )
        if self.ha_bridge and self.ha_bridge.client and self.ha_bridge.client.connected:
            self.ha_bridge.client.publish("safehalo/binary_sensor/fall/state", "ON")

    def _on_ecosystem_challenge_dismissed(self, details: str):
        """老人通过智能音箱语音应答消警"""
        logger.info(f"[ECOSYSTEM] {details}")
        self.engine.detector.targeted_inspector.acknowledge_safe(source="智能音箱语音消警")
        if self.dual_engine:
            self.dual_engine.csi_detector.targeted_inspector.acknowledge_safe(source="智能音箱语音消警")
        shared_system_state["state"] = "RESTING"

    def _on_ecosystem_security_alert(self, alert_type: str, details: str, meta: dict):
        """多传感器生态（门磁外围突破 / PIR 入侵确认）触发安防报警"""
        logger.log_event("SECURITY_INTRUSION", f"{alert_type}: {details}", level="ALARM")
        self.notifier.enqueue_custom_notification(
            title="【SafeHalo】安防入侵警报",
            content=details,
        )

    def _on_speaker_actuate(self, device_id: str, command: str) -> bool:
        """驱动现场智能音箱或串口蜂鸣器执行语音质询/作动"""
        logger.info(f"[ECOSYSTEM] 驱动音箱设备 [{device_id}] 执行作动: {command}")
        if not self._active_csi_serial or not hasattr(self._active_csi_serial, "write"):
            return False
        try:
            self._active_csi_serial.write(b"CHALLENGE_BEEP:1\n")
            return True
        except Exception as e:
            logger.debug(f"[ECOSYSTEM] 串口作动写入失败: {e}")
            return False

    def _on_challenge_intercepted(self, details: str):
        """阶段 2：质询窗口内老人主动微动或按键应答，成功拦截潜在误报"""
        self.pilot_manager.record_challenge_intercept()
        shared_system_state["pilot"] = self.pilot_manager.get_pilot_dashboard()

    def _on_web_challenge_acknowledge(self, source: str):
        self.engine.detector.targeted_inspector.acknowledge_safe(source=source)
        if self.dual_engine:
            self.dual_engine.csi_detector.targeted_inspector.acknowledge_safe(source=source)

    def _handle_exit_signal(self, signum, frame):
        logger.info(f"收到系统信号 ({signum})，正在优雅关闭 SafeHalo 服务...")
        self.running = False
        if self.httpd:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()
        sys.exit(0)

    def _on_state_change(self, old_state: HumanState, new_state: HumanState, features: CSIFeatures):
        # 实时同步状态至 Web 前端
        shared_system_state["state"] = new_state.value
        shared_system_state["variance"] = features.mean_variance
        shared_system_state["jitter"] = features.packet_interval_jitter
        shared_system_state["sample_rate"] = features.sample_rate_hz
        shared_system_state["spr"] = round(self.engine.detector.latest_spr, 4)
        shared_system_state["is_pet"] = (new_state == HumanState.PET_ACTIVITY)

        # 联动安防布防引擎
        self.security_guard.process_state(new_state, features)

        # 联动老人日常作息画像与异常偏离分析
        self.routine_monitor.update_state(
            human_state=new_state,
            is_presence=(new_state not in (HumanState.EMPTY, HumanState.CALIBRATING)),
            is_motion=(new_state == HumanState.MOVING),
        )
        shared_system_state["routine"] = self.routine_monitor.get_summary()

        # 同步至 Home Assistant
        if self.ha_bridge:
            rt_summary = shared_system_state.get("routine", {})
            self.ha_bridge.publish_state(
                human_state=new_state.value,
                is_fall=(new_state == HumanState.FALL_CONFIRMED),
                is_presence=(new_state not in (HumanState.EMPTY, HumanState.CALIBRATING)),
                is_moving=(new_state == HumanState.MOVING),
                is_pet=(new_state == HumanState.PET_ACTIVITY),
                radar_z=shared_system_state.get("radar_z", 0.0),
                respiration_bpm=shared_system_state.get("radar_bpm", 0.0),
                confidence=shared_system_state.get("fused_confidence", 1.0),
                routine_anomaly=rt_summary.get("has_anomaly", False),
                wake_time=rt_summary.get("wake_time", ""),
                night_restless_count=rt_summary.get("night_restless_count", 0),
            )

    def _on_routine_anomaly(self, evt: RoutineAnomalyEvent):
        """老人作息异常偏离通报 (晨间不起床、日间长时间静止无活动、夜间起夜过多)"""
        self.notifier.enqueue_routine_anomaly_alert(
            title=evt.title,
            description=evt.description,
            severity=evt.severity,
        )
        shared_system_state["routine"] = self.routine_monitor.get_summary()

    def _on_fall_alarm(self, features: CSIFeatures, verdict=None):
        room = shared_system_state.get("multilink", {}).get("active_room") or self.placement_adapter.current_config.room_name
        raw_conf = getattr(verdict, "confidence", None) if verdict is not None else None
        confidence = float(raw_conf) if (raw_conf is not None and raw_conf > 0) else 0.90
        raw_bpm = getattr(verdict, "respiration_bpm", None) if verdict is not None else None
        bpm = float(raw_bpm) if raw_bpm is not None else 0.0
        details = getattr(verdict, "details", "") or f"经历剧烈冲击→持续静止→本地质询无应答，均值方差: {features.mean_variance:.6f}"
        logger.log_fall_confirmed(
            details=details,
            bpm=bpm,
            confidence=confidence
        )
        # 阶段 2: 记录告警事件并同步推送微信小程序订阅消息
        inc = self.incident_manager.record_incident(
            room_name=room,
            event_type="FALL_ALARM",
            confidence=confidence,
            respiration_bpm=bpm,
            details=details,
            household_id=self.pilot_manager.active_household_id,
        )
        event_id = inc.event_id if inc else ""
        self.notifier.enqueue_fall_alarm(
            room_name=room,
            confidence=f"{int(confidence * 100)}%",
            details="倒地后经声光质询无应答且长时间未探测到自主起身活动",
            bpm=bpm,
            event_id=event_id,
            household_id=self.pilot_manager.active_household_id,
        )

    def _on_intruder_alarm(self, alert_level, msg: str, features: CSIFeatures):
        mode_name = self.security_guard.mode.value
        room = self.placement_adapter.current_config.room_name or "家庭监控区域"
        logger.log_security_intrusion(mode_name, features.diff_energy)
        self.notifier.enqueue_intruder_alarm(
            room_name=room,
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

        # 联动老人日常作息画像分析
        self.routine_monitor.update_state(
            human_state=HumanState.MOVING if new_state == FusedState.NORMAL_ACTIVITY else (
                HumanState.EMPTY if new_state == FusedState.EMPTY else HumanState.STATIC
            ),
            is_presence=(new_state not in (FusedState.EMPTY, FusedState.CALIBRATING)),
            is_motion=(new_state == FusedState.NORMAL_ACTIVITY),
        )
        shared_system_state["routine"] = self.routine_monitor.get_summary()

        # 同步至 Home Assistant
        if self.ha_bridge:
            rt_summary = shared_system_state.get("routine", {})
            self.ha_bridge.publish_state(
                human_state=new_state.value,
                is_fall=(new_state == FusedState.FALL_CONFIRMED),
                is_presence=(new_state not in (FusedState.EMPTY, FusedState.CALIBRATING)),
                is_moving=(new_state == FusedState.NORMAL_ACTIVITY),
                is_pet=(new_state == FusedState.PET_ACTIVITY),
                radar_z=res.radar_z,
                respiration_bpm=res.radar_respiration_bpm,
                confidence=res.fused_confidence,
                routine_anomaly=rt_summary.get("has_anomaly", False),
                wake_time=rt_summary.get("wake_time", ""),
                night_restless_count=rt_summary.get("night_restless_count", 0),
            )

    def _on_fused_fall_alarm(self, res: FusedPerceptionResult):
        bpm = res.radar_respiration_bpm if res.radar_respiration_bpm > 0 else 0.0
        logger.log_fall_confirmed(
            details=f"双模态融合告警: {res.decision_reason}",
            bpm=bpm,
            confidence=res.fused_confidence,
        )
        room = self.placement_adapter.current_config.room_name or "双模全域监测覆盖区"
        inc = self.incident_manager.record_incident(
            room_name=room,
            event_type="FALL_ALARM",
            confidence=res.fused_confidence,
            respiration_bpm=bpm,
            details=f"【双模确诊】{res.decision_reason} (测定高度 Z={res.radar_z:.2f}m)",
            household_id=self.pilot_manager.active_household_id,
        )
        self.notifier.enqueue_fall_alarm(
            room_name=room,
            confidence=f"{int(res.fused_confidence * 100)}%",
            details=f"【双模确诊】{res.decision_reason} (测定高度 Z={res.radar_z:.2f}m)",
            bpm=bpm,
            event_id=inc.event_id if inc else "",
            household_id=self.pilot_manager.active_household_id,
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
        """在独立后台线程启动多线程 Web 控制台与远程管理服务"""
        def _run_server():
            ThreadingHTTPServer.allow_reuse_address = True
            try:
                self.httpd = ThreadingHTTPServer((self.web_host, self.web_port), SafeHaloWebHandler)
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
            self._active_csi_serial = ser
            self._active_csi_path = port
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
                    ml_res = self.multilink_engine.feed_line(line)
                    if ml_res:
                        shared_system_state["multilink"] = {
                            "active_room": ml_res.active_room,
                            "participating_links": ml_res.participating_links,
                            "fused_energy": round(ml_res.fused_energy, 5),
                            "links": {
                                mac: {
                                    "room": t.room_name,
                                    "state": t.current_state.value,
                                    "packets": t.packet_count,
                                    "diff_energy": round(t.diff_energy, 5),
                                    "last_seen": t.last_seen,
                                }
                                for mac, t in ml_res.link_details.items()
                            },
                        }
                    if res:
                        _, feats, state = res
                        shared_system_state["variance"] = feats.mean_variance
                        shared_system_state["sample_rate"] = feats.sample_rate_hz
                        shared_system_state["jitter"] = feats.packet_interval_jitter
                        shared_system_state["spr"] = round(self.engine.detector.latest_spr, 4)

                    if self.engine.total_packets_received % 80 == 0:
                        shared_system_state["neighbor_filter"] = self.engine.neighbor_filter.get_spectrum_analysis()
                        if self.sensor_ecosystem:
                            self.sensor_ecosystem.check_periodical_linkage()

            except Exception as e:
                logger.warning(f"数据读取中断或硬件掉电: {e}，正在进入自愈重连流程...")
                shared_system_state["state"] = "ROUTER_OFFLINE"
            finally:
                self._active_csi_serial = None
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

        # 2. 启动 Home Assistant MQTT 自动发现网桥 (若已配置)
        if self.ha_bridge:
            self.ha_bridge.start()

        # 3. 启动毫米波雷达双模融合子系统 (若已配置)
        if self.radar_serial:
            self.start_radar_thread()

        # 4. 启动 CSI 检测引擎主循环
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
    parser.add_argument("--ha-mqtt-host", type=str, default=None, help="Home Assistant MQTT Broker IP/地址 (如 192.168.1.100)")
    parser.add_argument("--ha-mqtt-port", type=int, default=1883, help="Home Assistant MQTT 端口 (默认 1883)")
    parser.add_argument("--ha-mqtt-user", type=str, default=None, help="Home Assistant MQTT 用户名")
    parser.add_argument("--ha-mqtt-pass", type=str, default=None, help="Home Assistant MQTT 密码")
    parser.add_argument("--ha-webhook", type=str, default=None, help="Home Assistant 自动化 Webhook 地址")
    parser.add_argument("--demo", action="store_true", help="无硬件仿真运行模式")
    parser.add_argument("--pushplus-token", type=str, default=None, help="微信 PushPlus 告警 Token")
    parser.add_argument("--wait-network", type=float, default=2.0, help="开机缓冲时间 (秒)")
    parser.add_argument("--object-confirm-secs", type=float, default=0.0,
                        help="阶段一判物品后的长窗确认观察时长/秒 (0=关闭, 默认关闭)")
    parser.add_argument("--object-confirm-cv", type=float, default=0.35,
                        help="峰间隔变异系数阈值 (占位值, 待真数据标定)")
    parser.add_argument("--object-confirm-min-intervals", type=int, default=3,
                        help="长窗确认所需的最少峰间隔数 (少于该值不下结论)")
    parser.add_argument("--enable-peak-regularity", action="store_true", default=False,
                        help="(P0-3) 启用阶段一呼吸峰间隔规整度与准周期性抗噪检验 (默认关闭)")
    parser.add_argument("--peak-regularity-cv", type=float, default=0.35,
                        help="阶段一呼吸峰间隔变异系数 (CV) 门限 (默认 0.35)")
    parser.add_argument("--api-token", type=str, default=os.environ.get("SAFEHALO_API_TOKEN", ""),
                        help="机器接口鉴权 Token (可选，也可通过 SAFEHALO_API_TOKEN 环境变量配置)")
    parser.add_argument("--cookie-secure", action="store_true", default=False,
                        help="经 HTTPS 反向代理部署时为会话 Cookie 添加 Secure 标记")

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
        ha_mqtt_host=args.ha_mqtt_host,
        ha_mqtt_port=args.ha_mqtt_port,
        ha_mqtt_user=args.ha_mqtt_user,
        ha_mqtt_pass=args.ha_mqtt_pass,
        ha_webhook=args.ha_webhook,
        object_confirm_seconds=args.object_confirm_secs,
        object_confirm_cv=args.object_confirm_cv,
        object_confirm_min_intervals=args.object_confirm_min_intervals,
        enable_peak_regularity=args.enable_peak_regularity,
        peak_regularity_cv=args.peak_regularity_cv,
        api_token=args.api_token,
        cookie_secure=args.cookie_secure,
    )

    supervisor.start()

if __name__ == "__main__":
    main()

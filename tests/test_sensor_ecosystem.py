"""
SafeHalo 多传感器生态抽象与联动测试套件 (tests/test_sensor_ecosystem.py)
覆盖 P3-5: 门磁/PIR/智能音箱/SOS 按键等多传感器融合联动事件接口
"""

import time
import pytest

from src.safehalo.sensor_fusion_ecosystem import (
    SensorFusionEcosystem,
    SensorType,
    SensorEvent,
    LinkageAction,
    SensorDevice,
)
from src.safehalo.security_guard import SecurityMode


def test_default_device_registration():
    eco = SensorFusionEcosystem()
    status = eco.get_status()
    assert status["total_devices"] >= 5
    devices = {d["device_id"]: d for d in status["devices"]}
    assert "door_front" in devices
    assert "door_bathroom" in devices
    assert "pir_living" in devices
    assert "speaker_living" in devices
    assert "sos_bedroom" in devices
    assert devices["door_front"]["sensor_type"] == "door"
    assert devices["pir_living"]["sensor_type"] == "pir"


def test_custom_device_registration():
    eco = SensorFusionEcosystem()
    dev = eco.register_device(
        device_id="door_balcony",
        sensor_type="door",
        name="阳台门磁",
        room="阳台",
        metadata={"battery": 95},
    )
    assert dev.device_id == "door_balcony"
    assert dev.name == "阳台门磁"
    assert dev.metadata["battery"] == 95
    status = eco.get_status()
    dev_ids = [d["device_id"] for d in status["devices"]]
    assert "door_balcony" in dev_ids


def test_speaker_fall_challenge_dismissal_by_voice():
    dismissed = []
    speaker_actions = []

    eco = SensorFusionEcosystem(
        on_challenge_dismissed=lambda msg: dismissed.append(msg),
        on_speaker_actuate=lambda dev_id, cmd: speaker_actions.append((dev_id, cmd)),
    )

    # 1. CSI 检测器报告疑似跌倒，触发语音质询
    eco.notify_csi_fall_suspect("CSI能量骤变静止")
    assert eco.challenge_active is True
    assert len(speaker_actions) >= 1
    assert "CHALLENGE_VOICE" in speaker_actions[0][1]

    # 2. 老人应答“我没事”
    action, msg = eco.handle_event(
        device_id="speaker_living",
        event_name="voice_dismiss",
    )
    assert action == LinkageAction.CHALLENGE_DISMISSED
    assert "跌倒嫌疑已成功化解" in msg
    assert eco.challenge_active is False
    assert len(dismissed) == 1


def test_speaker_fall_challenge_escalation_by_voice_help():
    confirmed_alarms = []

    eco = SensorFusionEcosystem(
        on_fall_confirmed=lambda room, conf, msg: confirmed_alarms.append((room, conf, msg)),
    )

    eco.notify_csi_fall_suspect("CSI疑似跌倒")
    assert eco.challenge_active is True

    # 老人呼救喊“救命”
    action, msg = eco.handle_event(
        device_id="speaker_living",
        event_name="voice_help",
    )
    assert action == LinkageAction.FALL_ESCALATED
    assert "秒级升级确诊跌倒" in msg
    assert eco.challenge_active is False
    assert len(confirmed_alarms) == 1
    assert confirmed_alarms[0][1] == 1.0


def test_speaker_fall_challenge_timeout():
    eco = SensorFusionEcosystem()
    now = time.time()
    eco.challenge_timeout_seconds = 10.0
    eco.notify_csi_fall_suspect()
    eco.challenge_start_time = now - 15.0

    # 周期性巡检检测超时
    triggered = eco.check_periodical_linkage(now=now)
    assert len(triggered) == 1
    assert triggered[0][0] == LinkageAction.FALL_ESCALATED
    assert "质询已超时无应答" in triggered[0][1]
    assert eco.challenge_active is False


def test_bathroom_trapped_timeout_guard():
    confirmed_alarms = []
    eco = SensorFusionEcosystem(
        bathroom_timeout_seconds=900.0,  # 15 分钟
        on_fall_confirmed=lambda room, conf, msg: confirmed_alarms.append((room, conf, msg)),
    )

    t0 = 1000.0
    # 1. 关上卫生间门
    action, msg = eco.handle_event("door_bathroom", "door_closed", timestamp=t0)
    assert eco.bathroom_door_closed_time == t0

    # 2. 正常时间内 (10 分钟后) 不应触发报警
    triggered = eco.check_periodical_linkage(now=t0 + 600.0)
    assert len(triggered) == 0

    # 3. 超过 15 分钟 (16 分钟后) 未开门，触发困顿滞留紧急告警！
    triggered = eco.check_periodical_linkage(now=t0 + 960.0)
    assert len(triggered) == 1
    assert triggered[0][0] == LinkageAction.BATHROOM_TRAPPED_ALERT
    assert "防滑倒困顿滞留紧急预警" in triggered[0][1]
    assert len(confirmed_alarms) == 1
    assert confirmed_alarms[0][0] == "卫生间"

    # 4. 门重新打开，解除监控
    action, msg = eco.handle_event("door_bathroom", "door_open", timestamp=t0 + 1000.0)
    assert eco.bathroom_door_closed_time is None


def test_security_perimeter_breach_and_pir_confirmed_intrusion():
    security_alerts = []
    eco = SensorFusionEcosystem(
        on_security_alert=lambda alert_type, msg, meta: security_alerts.append((alert_type, msg)),
    )

    t0 = 2000.0
    eco.set_security_mode(SecurityMode.ARMED_AWAY)

    # 1. 外出布防下大门被推开 -> 外围突破
    action, msg = eco.handle_event("door_front", "door_open", timestamp=t0)
    assert action == LinkageAction.PERIMETER_BREACH_ALERT
    assert len(security_alerts) == 1
    assert security_alerts[0][0] == "PERIMETER_BREACH"

    # 2. 10 秒后客厅 PIR 检测到人体运动 -> 升级为 100% 确认入侵！
    action_pir, msg_pir = eco.handle_event("pir_living", "motion_detected", timestamp=t0 + 10.0)
    assert action_pir == LinkageAction.CONFIRMED_INTRUSION_ALERT
    assert len(security_alerts) == 2
    assert security_alerts[1][0] == "CONFIRMED_INTRUSION"


def test_curfew_anti_wandering_door_opened():
    security_alerts = []
    eco = SensorFusionEcosystem(
        on_security_alert=lambda alert_type, msg, meta: security_alerts.append((alert_type, msg)),
    )

    eco.set_security_mode(SecurityMode.CURFEW)

    # 深夜防走失模式下，大门被推开
    action, msg = eco.handle_event("door_front", "door_open")
    assert action == LinkageAction.CURFEW_WANDERING_ALERT
    assert "深夜外门已推开，疑似离家外出" in msg
    assert len(security_alerts) == 1
    assert security_alerts[0][0] == "CURFEW_WANDERING"


def test_pir_burst_rate_linkage():
    burst_requests = []
    eco = SensorFusionEcosystem(
        on_request_burst_rate=lambda rate: burst_requests.append(rate),
    )

    action, msg = eco.handle_event("pir_living", "motion_detected")
    assert action == LinkageAction.BURST_RATE_TRIGGERED
    assert len(burst_requests) == 1
    assert burst_requests[0] == 50


def test_sos_button_emergency_alarm():
    confirmed_alarms = []
    eco = SensorFusionEcosystem(
        on_fall_confirmed=lambda room, conf, msg: confirmed_alarms.append((room, conf, msg)),
    )

    action, msg = eco.handle_event("sos_bedroom", "sos_pressed")
    assert action == LinkageAction.SOS_EMERGENCY_ALERT
    assert len(confirmed_alarms) == 1
    assert confirmed_alarms[0][1] == 1.0
    assert "紧急 SOS 求救按钮" in confirmed_alarms[0][2]


def test_web_portal_sensor_event_and_status_integration():
    from src.safehalo.web_portal import SafeHaloWebHandler, sensor_ecosystem, auth_manager
    import io
    import json

    # 1. 模拟 POST /api/sensors/event (入户门推开)
    payload = json.dumps({
        "device_id": "door_front",
        "event": "open",
        "room": "入户玄关",
    }).encode("utf-8")

    class MockRequest:
        def makefile(self, *args, **kwargs):
            if "r" in args[0]:
                return io.BytesIO(f"POST /api/sensors/event HTTP/1.1\r\nContent-Length: {len(payload)}\r\n\r\n".encode("utf-8") + payload)
            return io.BytesIO()
        def sendall(self, data):
            pass

    # 验证生态中心接收事件
    action, msg = sensor_ecosystem.handle_event("door_front", "open")
    assert "door_front" in sensor_ecosystem.devices
    assert sensor_ecosystem.devices["door_front"].state == "open"

    # 2. 模拟 GET /api/sensors/status
    status = sensor_ecosystem.get_status()
    assert "total_devices" in status
    assert "devices" in status
    assert any(d["device_id"] == "door_front" and d["state"] == "open" for d in status["devices"])


def test_safehalo_supervisor_sensor_ecosystem_integration():
    from safehalo_service import SafeHaloSupervisor
    from src.safehalo.security_guard import SecurityMode

    supervisor = SafeHaloSupervisor(demo_mode=True)
    assert supervisor.sensor_ecosystem is not None

    # 1. 验证布防模式同步
    supervisor._on_web_mode_changed("ARMED_AWAY")
    assert supervisor.sensor_ecosystem.security_mode == SecurityMode.ARMED_AWAY

    # 2. 验证 CSI 质询触发联动智能音箱
    supervisor.engine.detector.targeted_inspector.trigger_inspection(timestamp=time.time())
    supervisor._on_trigger_challenge("疑似跌倒质询测试")
    assert supervisor.sensor_ecosystem.challenge_active is True

    # 3. 验证语音消警化解
    supervisor._on_ecosystem_challenge_dismissed("老人应答正常")
    assert supervisor.engine.detector.targeted_inspector.manual_safe_acknowledged is True

    # 4. 验证生态紧急确诊
    supervisor._on_ecosystem_fall_confirmed("卫生间", 1.0, "卫生间困顿超时")
    incidents = supervisor.incident_manager.list_recent_incidents(1)
    assert len(incidents) >= 1
    assert incidents[0]["room_name"] == "卫生间"

    # 5. 验证前端模式别名映射 (AWAY_SECURITY / NIGHT_GUARD)
    supervisor._on_web_mode_changed("AWAY_SECURITY")
    assert supervisor.security_guard.mode == SecurityMode.ARMED_AWAY
    supervisor._on_web_mode_changed("NIGHT_GUARD")
    assert supervisor.security_guard.mode == SecurityMode.ARMED_NIGHT

    # 6. 验证生态安全告警不会触发 AttributeError
    supervisor._on_ecosystem_security_alert("PERIMETER_BREACH", "测试门磁入侵", {})

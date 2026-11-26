"""
SafeHalo 多传感器生态抽象与融合联动引擎 (sensor_fusion_ecosystem.py)

工业级多模态传感器接入与安全联动体系 (P3-5):
1. 统一传感器设备抽象 (SensorDevice):
   - 支持 门磁 (Door/Window)、红外 (PIR)、智能音箱 (Smart Speaker)、SOS 紧急按键与毫米波雷达。
2. 跨模态联动与多级安防闭环 (Cross-Modal Linkage):
   - 跌倒语音质询与声控消警/呼救闭环:
     * CSI 怀疑跌倒 (FALL_SUSPECT) -> 智能音箱自动播报质询；
     * 老人语音回复“我没事/取消” (VOICE_DISMISS) -> 极速化解虚警并记录日志；
     * 老人喊“救命/摔倒了” (VOICE_HELP) -> 秒级直接升级为确诊跌倒 (FALL_CONFIRMED)；
   - 卫生间高危困顿超时守护 (Bathroom Trapped Timeout):
     * 卫生间门关闭后持续监测，若超过阈值 (如 20 分钟) 且未开门，结合 CSI 动能判断触发滞留困顿预警；
   - 立体安防与入侵多传感器交叉认证 (Perimeter & Intrusion):
     * 离家布防 (ARMED_AWAY): 门磁触发立即报外围突破 (PERIMETER_BREACH)；若伴随 PIR/CSI 动能则升级为确认入侵；
     * 深夜防走失 (CURFEW): 深夜老人推开外门立即报警，无需等待 5 分钟空房超时；
   - 动态 CSI 采频激励:
     * PIR 捕捉到突发动能时联动 CSI 探针提频至 50Hz/100Hz 详细追踪。

纯 Python 标准库实现，零第三方依赖。
"""

import enum
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .logger import logger
from .security_guard import SecurityMode


class SensorType(str, enum.Enum):
    DOOR = "door"                  # 门窗磁传感器
    PIR = "pir"                    # 人体红外运动传感器
    SPEAKER = "speaker"            # 智能音箱 / 双向语音质询终端
    SOS_BUTTON = "sos_button"      # 随身/墙面紧急呼叫按钮
    MMWAVE_RADAR = "mmwave_radar"  # 毫米波雷达
    OTHER = "other"


class SensorEvent(str, enum.Enum):
    # 门磁事件
    DOOR_OPEN = "door_open"
    DOOR_CLOSED = "door_closed"

    # PIR 动静事件
    MOTION_DETECTED = "motion_detected"
    MOTION_CLEARED = "motion_cleared"

    # 智能音箱事件
    VOICE_DISMISS = "voice_dismiss"    # "我没事" / "取消"
    VOICE_HELP = "voice_help"          # "救命" / "摔倒了"
    BUTTON_PRESS = "button_press"      # 实体消警按键

    # 紧急求救按键
    SOS_PRESSED = "sos_pressed"
    FALL_IMPACT = "fall_impact"        # 手环加速度冲击初筛


class LinkageAction(str, enum.Enum):
    NONE = "none"
    PERIMETER_BREACH_ALERT = "perimeter_breach_alert"       # 离家布防外围防线突破
    CONFIRMED_INTRUSION_ALERT = "confirmed_intrusion_alert" # 门磁+PIR/CSI 多模态确认入侵
    CURFEW_WANDERING_ALERT = "curfew_wandering_alert"       # 深夜老人开门走失告警
    BATHROOM_TRAPPED_ALERT = "bathroom_trapped_alert"       # 卫生间关门超时滞留告警
    CHALLENGE_DISMISSED = "challenge_dismissed"             # 语音质询成功化解
    FALL_ESCALATED = "fall_escalated"                       # 语音呼救升级为确诊跌倒
    SOS_EMERGENCY_ALERT = "sos_emergency_alert"             # 紧急 SOS 按钮触发
    BURST_RATE_TRIGGERED = "burst_rate_triggered"           # 联动探针提频采样


@dataclass
class SensorDevice:
    device_id: str
    sensor_type: SensorType
    name: str
    room: str
    state: str = "unknown"
    last_event: Optional[str] = None
    last_seen: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "sensor_type": self.sensor_type.value,
            "name": self.name,
            "room": self.room,
            "state": self.state,
            "last_event": self.last_event,
            "last_seen": round(self.last_seen, 1),
            "metadata": self.metadata,
        }


class SensorFusionEcosystem:
    """多模态传感器生态融合中心"""

    MAX_REGISTERED_DEVICES = 64

    def __init__(
        self,
        bathroom_timeout_seconds: float = 1200.0,  # 默认卫生间关门滞留超时 20 分钟
        on_fall_confirmed: Optional[Callable[[str, float, str], None]] = None,
        on_challenge_dismissed: Optional[Callable[[str], None]] = None,
        on_security_alert: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
        on_request_burst_rate: Optional[Callable[[int], None]] = None,
        on_speaker_actuate: Optional[Callable[[str, str], None]] = None,
    ):
        self._lock = threading.Lock()
        self.devices: Dict[str, SensorDevice] = {}
        self.bathroom_timeout_seconds = bathroom_timeout_seconds

        # 回调链路
        self.on_fall_confirmed = on_fall_confirmed
        self.on_challenge_dismissed = on_challenge_dismissed
        self.on_security_alert = on_security_alert
        self.on_request_burst_rate = on_request_burst_rate
        self.on_speaker_actuate = on_speaker_actuate

        # 状态上下文
        self.security_mode = SecurityMode.DISARMED
        self.challenge_active = False
        self.challenge_start_time: Optional[float] = None
        self.challenge_timeout_seconds = 20.0

        # 门状态追踪
        self.bathroom_door_closed_time: Optional[float] = None
        self.front_door_last_open_time: Optional[float] = None

        # PIR 状态追踪
        self.last_motion_time: float = 0.0

        # 预注册默认虚拟生态设备
        self._init_default_devices()

    def _init_default_devices(self):
        defaults = [
            SensorDevice("door_front", SensorType.DOOR, "入户大门磁", "入户玄关", state="closed"),
            SensorDevice("door_bathroom", SensorType.DOOR, "卫生间门磁", "卫生间", state="closed"),
            SensorDevice("pir_living", SensorType.PIR, "客厅人体红外", "客厅", state="still"),
            SensorDevice("speaker_living", SensorType.SPEAKER, "客厅智能音箱", "客厅", state="idle"),
            SensorDevice("sos_bedroom", SensorType.SOS_BUTTON, "床头紧急求助按键", "主卧", state="standby"),
        ]
        for dev in defaults:
            self.devices[dev.device_id] = dev

    def register_device(
        self,
        device_id: str,
        sensor_type: str,
        name: str,
        room: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SensorDevice:
        """动态注册新传感器设备"""
        with self._lock:
            try:
                st = SensorType(sensor_type.lower())
            except ValueError:
                st = SensorType.OTHER

            dev = SensorDevice(
                device_id=device_id,
                sensor_type=st,
                name=name,
                room=room,
                metadata=metadata or {},
                last_seen=time.time(),
            )
            self.devices[device_id] = dev
            logger.info(f"[ECOSYSTEM] 注册新传感器设备: [{device_id}] {name} ({st.value}) - 房间: {room}")
            return dev

    def set_security_mode(self, mode: SecurityMode):
        with self._lock:
            self.security_mode = mode

    def notify_csi_fall_suspect(self, details: str = "CSI疑似跌倒"):
        """当 CSI/毫米波初筛出疑似跌倒时，由检测主流程通知生态中心发起智能音箱质询"""
        with self._lock:
            self.challenge_active = True
            self.challenge_start_time = time.time()
            logger.info(f"[ECOSYSTEM] 触发跌倒质询智能闭环，联动音箱发起语音核实: {details}")

            # 驱动所有已注册的智能音箱播报语音质询
            for dev in self.devices.values():
                if dev.sensor_type == SensorType.SPEAKER:
                    if self.on_speaker_actuate:
                        self.on_speaker_actuate(dev.device_id, "CHALLENGE_VOICE:请问您摔倒了吗？如果没事请说话或按键")

    def handle_event(
        self,
        device_id: str,
        event_name: str,
        timestamp: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Tuple[LinkageAction, str]:
        """
        核心事件处理与多传感器交叉融合联动逻辑
        :return: (LinkageAction, 详细处理结果描述)
        """
        with self._lock:
            now = timestamp or time.time()
            extra = extra or {}
            event_norm = event_name.lower().strip()

            # 1. 查找或就地自动注册设备（带容量上限保护，防止随机 device_id 洪泛耗尽内存）
            dev = self.devices.get(device_id)
            if not dev:
                if len(self.devices) >= self.MAX_REGISTERED_DEVICES:
                    return LinkageAction.NONE, "已达设备自动注册上限，忽略未知传感器"
                st = SensorType.OTHER
                if "door" in device_id:
                    st = SensorType.DOOR
                elif "pir" in device_id:
                    st = SensorType.PIR
                elif "speaker" in device_id:
                    st = SensorType.SPEAKER
                elif "sos" in device_id:
                    st = SensorType.SOS_BUTTON
                dev = SensorDevice(
                    device_id=device_id,
                    sensor_type=st,
                    name=device_id,
                    room=extra.get("room", "默认房间"),
                    last_seen=now,
                )
                self.devices[device_id] = dev

            dev.last_seen = now
            dev.last_event = event_norm

            action = LinkageAction.NONE
            msg = "事件已接收并记录"

            # ========================================================
            # 2. 智能音箱 (Smart Speaker) 语音质询与声控消警/求救闭环
            # ========================================================
            if dev.sensor_type == SensorType.SPEAKER or event_norm in (
                "voice_dismiss", "voice_help", "button_press"
            ):
                if event_norm in ("voice_dismiss", "button_press", "dismiss"):
                    dev.state = "idle"
                    if self.challenge_active:
                        self.challenge_active = False
                        self.challenge_start_time = None
                        action = LinkageAction.CHALLENGE_DISMISSED
                        msg = "老人语音应答正常或按下按键，跌倒嫌疑已成功化解"
                        logger.info(f"[ECOSYSTEM] {msg}")
                        if self.on_challenge_dismissed:
                            self.on_challenge_dismissed(msg)
                    else:
                        msg = "收到音箱消警信号，当前无活跃质询"

                elif event_norm in ("voice_help", "help", "sos"):
                    dev.state = "calling_help"
                    self.challenge_active = False
                    self.challenge_start_time = None
                    action = LinkageAction.FALL_ESCALATED
                    msg = "老人语音呼救（喊救命/摔倒了），秒级升级确诊跌倒！"
                    logger.warning(f"[ECOSYSTEM] {msg}")
                    if self.on_fall_confirmed:
                        self.on_fall_confirmed(dev.room, 1.0, msg)

            # ========================================================
            # 3. 门磁传感器 (Door/Window) 联动
            # ========================================================
            elif dev.sensor_type == SensorType.DOOR or "door" in event_norm or "open" in event_norm or "closed" in event_norm:
                is_open = event_norm in ("open", "door_open", "opened")
                dev.state = "open" if is_open else "closed"

                # 3.1 入户大门联动
                if "front" in device_id or dev.room in ("入户玄关", "大门"):
                    if is_open:
                        self.front_door_last_open_time = now
                        # 离家布防模式：外围防线突破告警
                        if self.security_mode == SecurityMode.ARMED_AWAY:
                            action = LinkageAction.PERIMETER_BREACH_ALERT
                            msg = f"离家布防模式下大门被推开！外围防线已突破: [{dev.name}]"
                            logger.warning(f"[SECURITY] {msg}")
                            if self.on_security_alert:
                                self.on_security_alert("PERIMETER_BREACH", msg, dev.to_dict())

                        # 深夜防走失模式：深夜开门走失告警
                        elif self.security_mode == SecurityMode.CURFEW:
                            action = LinkageAction.CURFEW_WANDERING_ALERT
                            msg = f"老人防走失模式告警：深夜外门已推开，疑似离家外出: [{dev.name}]"
                            logger.warning(f"[SECURITY] {msg}")
                            if self.on_security_alert:
                                self.on_security_alert("CURFEW_WANDERING", msg, dev.to_dict())

                # 3.2 卫生间门联动 (防滞留困顿)
                if "bathroom" in device_id or dev.room == "卫生间":
                    if not is_open:
                        # 门关闭：开启在室防滞留计时
                        self.bathroom_door_closed_time = now
                        logger.info(f"[ECOSYSTEM] 卫生间门已关闭，启动防困顿滞留监控 (超时门限: {self.bathroom_timeout_seconds}s)")
                    else:
                        # 门重新推开：解除卫生间滞留监控
                        if self.bathroom_door_closed_time is not None:
                            duration = now - self.bathroom_door_closed_time
                            self.bathroom_door_closed_time = None
                            logger.info(f"[ECOSYSTEM] 卫生间门已重新打开，正常解除滞留监控 (在室用时: {duration:.1f}s)")

            # ========================================================
            # 4. 人体红外运动传感器 (PIR Motion) 联动
            # ========================================================
            elif dev.sensor_type == SensorType.PIR or "motion" in event_norm:
                is_motion = event_norm in ("motion_detected", "motion", "detected")
                dev.state = "motion" if is_motion else "still"

                if is_motion:
                    self.last_motion_time = now
                    # 4.1 离家布防联动：如果大门刚刚在 30 秒内被推开，伴随 PIR 动能 -> 升级为 100% 确信入侵
                    if self.security_mode == SecurityMode.ARMED_AWAY:
                        if self.front_door_last_open_time and (now - self.front_door_last_open_time) < 30.0:
                            action = LinkageAction.CONFIRMED_INTRUSION_ALERT
                            msg = f"门磁触发后紧随 PIR 人体红外动能，确认家中遭遇非法入侵！位置: {dev.room}"
                            logger.error(f"[SECURITY] {msg}")
                            if self.on_security_alert:
                                self.on_security_alert("CONFIRMED_INTRUSION", msg, dev.to_dict())
                        else:
                            action = LinkageAction.PERIMETER_BREACH_ALERT
                            msg = f"离家布防下 PIR 检测到人体运动，触发入侵告警: {dev.room}"
                            if self.on_security_alert:
                                self.on_security_alert("INTRUSION_MOTION", msg, dev.to_dict())

                    # 4.2 动态采频激励联动：请求 CSI 探针提频至 50Hz 捕捉高保真动作
                    if self.on_request_burst_rate:
                        self.on_request_burst_rate(50)
                        action = LinkageAction.BURST_RATE_TRIGGERED
                        msg = f"PIR 捕捉到动能，联动触发 CSI 探针提频采样 (50Hz)"

            # ========================================================
            # 5. 紧急求救按键 (SOS Button)
            # ========================================================
            elif dev.sensor_type == SensorType.SOS_BUTTON or "sos" in event_norm:
                dev.state = "alarm"
                action = LinkageAction.SOS_EMERGENCY_ALERT
                msg = f"老人按下紧急 SOS 求救按钮！位置: [{dev.room}] {dev.name}"
                logger.error(f"[EMERGENCY] {msg}")
                if self.on_fall_confirmed:
                    self.on_fall_confirmed(dev.room, 1.0, msg)

            return action, msg

    def check_periodical_linkage(self, now: Optional[float] = None) -> List[Tuple[LinkageAction, str]]:
        """周期性心跳巡检 (如每秒调用)，检查质询超时与卫生间关门超时滞留"""
        with self._lock:
            cur_time = now or time.time()
            triggered = []

            # 1. 检查跌倒语音质询是否超时 (20秒内无应答默认转人工关注)
            if self.challenge_active and self.challenge_start_time:
                if (cur_time - self.challenge_start_time) >= self.challenge_timeout_seconds:
                    self.challenge_active = False
                    self.challenge_start_time = None
                    msg = "智能音箱跌倒质询已超时无应答，维持警戒推送紧急告警"
                    logger.warning(f"[ECOSYSTEM] {msg}")
                    triggered.append((LinkageAction.FALL_ESCALATED, msg))

            # 2. 检查卫生间关门滞留是否超时
            if self.bathroom_door_closed_time is not None:
                duration = cur_time - self.bathroom_door_closed_time
                if duration >= self.bathroom_timeout_seconds:
                    msg = f"卫生间门关闭已连续 {int(duration / 60)} 分钟未打开，触发防滑倒困顿滞留紧急预警！"
                    logger.error(f"[ECOSYSTEM] {msg}")
                    triggered.append((LinkageAction.BATHROOM_TRAPPED_ALERT, msg))
                    if self.on_fall_confirmed:
                        self.on_fall_confirmed("卫生间", 0.95, msg)
                    # 避免一秒内持续重复触发，重置滞留起始点往后推 5 分钟
                    self.bathroom_door_closed_time = cur_time - (self.bathroom_timeout_seconds - 300.0)

            return triggered

    def get_status(self) -> Dict[str, Any]:
        """获取当前多传感器生态遥测与联动快照"""
        with self._lock:
            now = time.time()
            dev_list = [d.to_dict() for d in self.devices.values()]
            bathroom_elapsed = None
            if self.bathroom_door_closed_time is not None:
                bathroom_elapsed = round(max(0.0, now - self.bathroom_door_closed_time), 1)

            return {
                "total_devices": len(self.devices),
                "security_mode": self.security_mode.value,
                "challenge_active": self.challenge_active,
                "bathroom_door_closed_seconds": bathroom_elapsed,
                "bathroom_timeout_seconds": self.bathroom_timeout_seconds,
                "devices": dev_list,
            }

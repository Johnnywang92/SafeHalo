"""
SafeHalo 智能安防与入侵防盗引擎 (security_guard.py)
复用同一套 Wi-Fi CSI 硬件与信号处理链路，实现全屋隐蔽防盗。

相较于传统红外(PIR)与摄像头的核心优势:
1. 穿墙与无死角: Wi-Fi 信号穿透木门、隔断和黑暗，即便小偷躲在窗帘后或断电关灯也能感知
2. 无隐私侵犯: 不使用摄像头，卧室、卫生间等极私密区域也能全天候安防布防
3. 自带宠物免疫: 排除猫狗活动，极低误报率
4. 一机两用: 老人在家时为「跌倒守护」，老人离家或夜间睡眠时为「隐形防盗雷达」
"""

import time
from enum import Enum
from typing import Callable, Optional
from .detector import HumanState
from .feature_extractor import CSIFeatures
from .logger import logger

class SecurityMode(Enum):
    DISARMED = "DISARMED"       # 撤防模式 / 居家跌倒守护 (FALL_GUARD)
    ARMED_AWAY = "ARMED_AWAY"   # 离家布防模式 (家中应无人，检测到任何人体活动即报入侵)
    ARMED_NIGHT = "ARMED_NIGHT" # 夜间布防模式 (大门/客厅防盗，过滤静态睡眠呼吸，仅对大幅度走动闯入报警)
    CURFEW = "CURFEW"           # 老人夜间防走失模式 (监测深夜离家超时未归)

class IntrusionAlertLevel(Enum):
    INFO = "INFO"               # 正常人员出入
    SUSPECT = "SUSPECT"         # 可疑微动 (有人潜伏/静止)
    ALARM = "ALARM"             # 确凿非法闯入或防走失告警

class SecurityGuardEngine:
    def __init__(
        self,
        initial_mode: SecurityMode = SecurityMode.ARMED_AWAY,
        on_intruder_alarm: Optional[Callable[[IntrusionAlertLevel, str, CSIFeatures], None]] = None,
        grace_period_seconds: float = 15.0, # 离家出门缓冲时间 (秒)
        curfew_empty_timeout_seconds: float = 300.0, # 防走失模式下允许空房的最大秒数 (默认5分钟)
    ):
        """
        :param initial_mode: 初始安防模式
        :param on_intruder_alarm: 入侵报警触发回调
        :param grace_period_seconds: 出门布防倒计时缓冲
        :param curfew_empty_timeout_seconds: 老人防走失模式的空房离家超时阈值
        """
        self.mode = initial_mode
        self.on_intruder_alarm = on_intruder_alarm
        self.arm_time: Optional[float] = None
        self.grace_period_seconds = grace_period_seconds
        self.curfew_empty_timeout_seconds = curfew_empty_timeout_seconds

        # 连续判定计数，防瞬态误判
        self.consecutive_human_frames = 0
        self.last_alarm_time: Optional[float] = None
        self.empty_start_time: Optional[float] = None

    def set_mode(self, new_mode: SecurityMode):
        self.mode = new_mode
        self.arm_time = None
        self.consecutive_human_frames = 0
        self.empty_start_time = None
        logger.log_event("SECURITY_MODE", f"安防布防模式已切换为: {self.mode.value}", level="INFO")

    def set_mode_by_name(self, mode_str: str) -> SecurityMode:
        """支持 Web 端与 Home Assistant 传入的字符串模式名称映射"""
        norm = (mode_str or "").strip().upper()
        mapping = {
            "FALL_GUARD": SecurityMode.DISARMED,
            "DISARMED": SecurityMode.DISARMED,
            "DISARM": SecurityMode.DISARMED,
            "ARMED_AWAY": SecurityMode.ARMED_AWAY,
            "ARM_AWAY": SecurityMode.ARMED_AWAY,
            "AWAY": SecurityMode.ARMED_AWAY,
            "ARMED_NIGHT": SecurityMode.ARMED_NIGHT,
            "ARM_NIGHT": SecurityMode.ARMED_NIGHT,
            "NIGHT": SecurityMode.ARMED_NIGHT,
            "ARM_HOME": SecurityMode.ARMED_NIGHT,
            "CURFEW": SecurityMode.CURFEW,
        }
        target = mapping.get(norm, SecurityMode.DISARMED)
        self.set_mode(target)
        return target

    def process_state(self, current_state: HumanState, features: CSIFeatures):
        """
        根据检测器输出的人体生理状态，判定是否存在入侵行为或深夜离家走失
        """
        now = features.timestamp

        # 撤防状态下不进行入侵检测
        if self.mode == SecurityMode.DISARMED:
            return

        if self.arm_time is None:
            self.arm_time = now

        # 出门缓冲期内不报警 (给用户关门离开的时间)
        if self.grace_period_seconds > 0 and (now - self.arm_time) < self.grace_period_seconds:
            return

        # 报警冷却期 (触发后 30 秒内不重复轰炸手机)
        if self.last_alarm_time is not None and (now - self.last_alarm_time) < 30.0:
            return

        # 模式 3: 老人防走失模式 (CURFEW) - 监测老人离开后房间持续为空超时
        if self.mode == SecurityMode.CURFEW:
            if current_state == HumanState.EMPTY:
                if self.empty_start_time is None:
                    self.empty_start_time = now
                elif (now - self.empty_start_time) >= self.curfew_empty_timeout_seconds:
                    self.last_alarm_time = now
                    msg = f"防走失布防告警：监测区域已连续 {int(now - self.empty_start_time)} 秒无人，请关注老人是否深夜开门外出！"
                    logger.log_security_intrusion(self.mode.value, features.diff_energy)
                    if self.on_intruder_alarm:
                        self.on_intruder_alarm(IntrusionAlertLevel.ALARM, msg, features)
            elif current_state in (HumanState.MOVING, HumanState.STATIC, HumanState.FALL_SUSPECT, HumanState.FALL_CONFIRMED):
                self.empty_start_time = None
            return

        # 宠物活动直接放行，不触发防盗
        if current_state in (HumanState.EMPTY, HumanState.PET_ACTIVITY, HumanState.CALIBRATING):
            self.consecutive_human_frames = 0
            return

        # 模式 1: 离家布防模式 (家中应当完全无人)
        if self.mode == SecurityMode.ARMED_AWAY:
            if current_state == HumanState.MOVING:
                self.consecutive_human_frames += 1
                if self.consecutive_human_frames >= 2: # 连续 2 帧确认为人体走动
                    self.last_alarm_time = now
                    msg = "离家布防模式下检测到人员走动入侵！"
                    logger.log_security_intrusion(self.mode.value, features.diff_energy)
                    if self.on_intruder_alarm:
                        self.on_intruder_alarm(IntrusionAlertLevel.ALARM, msg, features)

            elif current_state == HumanState.STATIC:
                self.consecutive_human_frames += 1
                if self.consecutive_human_frames >= 10: # 长时间静止潜伏
                    self.last_alarm_time = now
                    msg = "离家布防模式下检测到人员潜伏静止 (呼吸微动)！"
                    logger.log_security_intrusion(self.mode.value, features.diff_energy)
                    if self.on_intruder_alarm:
                        self.on_intruder_alarm(IntrusionAlertLevel.SUSPECT, msg, features)

        # 模式 2: 夜间布防模式 (允许静态睡眠呼吸，对持续大幅走动闯入报警)
        elif self.mode == SecurityMode.ARMED_NIGHT:
            if current_state == HumanState.MOVING:
                self.consecutive_human_frames += 1
                if self.consecutive_human_frames >= 3:
                    self.last_alarm_time = now
                    msg = "夜间布防模式下检测到持续异常走动闯入！"
                    logger.log_security_intrusion(self.mode.value, features.diff_energy)
                    if self.on_intruder_alarm:
                        self.on_intruder_alarm(IntrusionAlertLevel.ALARM, msg, features)
            else:
                self.consecutive_human_frames = 0

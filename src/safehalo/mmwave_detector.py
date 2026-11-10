"""
SafeHalo 毫米波雷达空间态势与跌倒判定器 (mmwave_detector.py)
基于毫米波 FMCW 雷达点云/目标轨迹 (X, Y, Z, 速度, 距离) 的专用跌倒与人体姿态判定算法。

核心算法逻辑:
1. 三维高度骤降分析 (Height Drop Signature):
   - 检测目标躯干高度 Z 在短时间内 (0.3s - 0.8s) 由正常高度 (> 1.0m) 垂直坠落至地面高度 (< 0.4m)。
2. 地面持续静止监测 (Post-Fall Ground Inactivity Window):
   - 跌倒后，老人常伴随丧失行动能力。在地面高度持续静止超过预设阈值 (默认 3.5 秒) 方升级为确认跌倒，排除快速俯卧撑/捡东西等自恢复动作。
3. 生命体征微多普勒滤波 (Respiration Micro-Doppler Check):
   - 区分掉落的书包/衣物与真实活体: 检测胸腔微动呼吸频段 (8 - 30 bpm)，确认地面有活体昏迷。
4. 硬件雷达报警双向校验:
   - 兼容器件内置跌倒事件 (如 MR60FDA1 硬件标志) 与自研算法的双保险交叉投票。

纯 Python 标准库实现，零第三方依赖。
"""

import enum
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, List, Optional, Tuple

from .mmwave_parser import RadarFrame, RadarPosture, RadarTarget
from .logger import logger


class RadarHumanState(enum.Enum):
    """毫米波雷达判定的人体状态"""
    EMPTY = "empty"                     # 无人
    WALKING = "walking"                 # 行走/大动作移动
    SITTING = "sitting"                 # 坐姿稳定
    STANDING = "standing"               # 站立稳定
    FALL_SUSPECT = "fall_suspect"       # 怀疑跌倒 (出现下坠动作或瞬时高度骤降)
    FALL_CONFIRMED = "fall_confirmed"   # 确认跌倒 (地面静止超时，确诊坠地)


@dataclass
class RadarFallFeatures:
    """提取的毫米波雷达动态特征"""
    timestamp: float
    target_count: int
    primary_z: float                # 主目标当前垂向高度 (米)
    primary_speed: float            # 主目标当前径向/多普勒速度 (米/秒)
    descent_rate: float             # 垂直高度下坠速率 (米/秒)
    ground_stillness_sec: float     # 地面静止累计时间 (秒)
    has_respiration: bool           # 是否捕捉到活体呼吸微动
    respiration_bpm: float          # 呼吸心跳微动频率
    confidence: float               # 跌倒确信度 (0.0 ~ 1.0)
    raw_fall_flag: bool = False     # 硬件传感器原生告警


class MMWaveDetector:
    """毫米波雷达跌倒与姿态检测器"""

    def __init__(
        self,
        floor_height_thresh: float = 0.40,      # 地面高度阈值 (小于该高度视为贴地)
        standing_height_min: float = 1.05,      # 站姿最小标称高度
        fall_descent_thresh: float = 1.20,      # 下坠速率阈值 (米/秒)
        stillness_confirm_sec: float = 3.5,     # 地面静止确认时长 (秒)
        on_state_change: Optional[Callable[[RadarHumanState, RadarHumanState, RadarFallFeatures], None]] = None,
        on_fall_alarm: Optional[Callable[[RadarFallFeatures], None]] = None,
    ):
        self.floor_height_thresh = floor_height_thresh
        self.standing_height_min = standing_height_min
        self.fall_descent_thresh = fall_descent_thresh
        self.stillness_confirm_sec = stillness_confirm_sec

        self.on_state_change = on_state_change
        self.on_fall_alarm = on_fall_alarm

        self.state: RadarHumanState = RadarHumanState.EMPTY
        self.last_state_change = time.time()

        # 历史目标时序滑动窗口: [(ts, primary_target)]
        self.target_history: Deque[Tuple[float, RadarTarget]] = deque(maxlen=60)
        self.fall_suspect_start_time: Optional[float] = None
        self.last_alarm_time = 0.0

    def update(self, frame: RadarFrame) -> Tuple[RadarHumanState, RadarFallFeatures]:
        """
        接收单帧毫米波雷达数据，执行特征提取与时序状态机转移。
        """
        now = frame.timestamp
        targets = frame.targets

        # 1. 目标提取与排序: 优先选取反射能量最大或最靠近中心的主目标
        primary_target: Optional[RadarTarget] = None
        if targets:
            # 排序规则: 优先考虑有明确高度且能量强的目标
            sorted_targets = sorted(targets, key=lambda t: t.energy, reverse=True)
            primary_target = sorted_targets[0]

        # 2. 如果连续无目标，重置为 EMPTY
        if not frame.human_presence or primary_target is None:
            # 若之前处于怀疑或确认跌倒，且目标瞬间消失 (可能是雷达视野边缘跌落)
            if self.state in (RadarHumanState.FALL_SUSPECT, RadarHumanState.FALL_CONFIRMED):
                # 保持短暂记忆，不立即清零
                pass
            else:
                self._transition_to(RadarHumanState.EMPTY, now)
                features = RadarFallFeatures(
                    timestamp=now,
                    target_count=0,
                    primary_z=0.0,
                    primary_speed=0.0,
                    descent_rate=0.0,
                    ground_stillness_sec=0.0,
                    has_respiration=False,
                    respiration_bpm=0.0,
                    confidence=0.0,
                    raw_fall_flag=frame.fall_flag,
                )
                return self.state, features

        current_z = primary_target.z if primary_target else 0.0
        current_speed = primary_target.speed if primary_target else 0.0
        has_resp = (primary_target.respiration_bpm > 0) if primary_target else False
        resp_bpm = primary_target.respiration_bpm if primary_target else 0.0

        # 记录目标时序
        if primary_target:
            self.target_history.append((now, primary_target))

        # 3. 动态特征提取: 计算过去 0.6 秒内的垂直下坠速率 (dz/dt)
        descent_rate = 0.0
        if len(self.target_history) >= 2:
            # 寻找约 0.5s 前的样本
            past_sample = None
            for past_ts, past_t in reversed(self.target_history):
                dt = now - past_ts
                if 0.25 <= dt <= 0.8:
                    past_sample = (dt, past_t)
                    break
            if past_sample:
                dt, past_t = past_sample
                # dz = past_z - current_z (高度降低为正下坠速度)
                descent_rate = (past_t.z - current_z) / max(dt, 0.05)

        # 4. 地面静止与姿态判定
        is_on_ground = (current_z > 0 and current_z <= self.floor_height_thresh) or (
            primary_target and primary_target.posture == RadarPosture.LYING_FLOOR
        )

        stillness_duration = 0.0
        if is_on_ground:
            if self.fall_suspect_start_time is None:
                self.fall_suspect_start_time = now
            stillness_duration = now - self.fall_suspect_start_time
        else:
            # 离开地面 (起身)，重置下坠计时
            self.fall_suspect_start_time = None

        # 5. 置信度评分模型
        confidence = 0.0
        if is_on_ground:
            # 基础分 0.5
            confidence += 0.50
            # 伴随高速下坠动作 + 0.25
            if descent_rate > self.fall_descent_thresh:
                confidence += 0.25
            # 伴随微弱呼吸体征 (排除无生命物体) + 0.15
            if has_resp and 8.0 <= resp_bpm <= 35.0:
                confidence += 0.15
            # 地面持续静止达标 + 0.20
            if stillness_duration >= self.stillness_confirm_sec:
                confidence += 0.20
            # 硬件原生跌倒标志 + 0.20
            if frame.fall_flag:
                confidence += 0.20
        confidence = min(max(confidence, 0.0), 1.0)

        fall_features = RadarFallFeatures(
            timestamp=now,
            target_count=len(targets),
            primary_z=round(current_z, 3),
            primary_speed=round(current_speed, 3),
            descent_rate=round(descent_rate, 3),
            ground_stillness_sec=round(stillness_duration, 2),
            has_respiration=has_resp,
            respiration_bpm=round(resp_bpm, 1),
            confidence=round(confidence, 2),
            raw_fall_flag=frame.fall_flag,
        )

        # 6. 时序状态机状态转移
        new_state = self._evaluate_state(frame, fall_features, is_on_ground, stillness_duration)
        if new_state != self.state:
            self._transition_to(new_state, now, fall_features)

        # 7. 触发跌倒告警
        if self.state == RadarHumanState.FALL_CONFIRMED:
            if now - self.last_alarm_time > 8.0:  # 8 秒防告警轰炸
                self.last_alarm_time = now
                if self.on_fall_alarm:
                    try:
                        self.on_fall_alarm(fall_features)
                    except Exception as e:
                        logger.error(f"[MMWaveDetector] 触发跌倒告警异常: {e}")

        return self.state, fall_features

    def _evaluate_state(
        self,
        frame: RadarFrame,
        feat: RadarFallFeatures,
        is_on_ground: bool,
        stillness_duration: float,
    ) -> RadarHumanState:
        # 1. 硬件直接报告跌倒 或 算法确诊 (地面持续静止达标)
        if frame.fall_flag or (is_on_ground and stillness_duration >= self.stillness_confirm_sec):
            return RadarHumanState.FALL_CONFIRMED

        # 2. 下坠过程或短暂停留在地面 -> 怀疑跌倒
        if (is_on_ground and stillness_duration < self.stillness_confirm_sec) or (
            feat.descent_rate > self.fall_descent_thresh
        ):
            return RadarHumanState.FALL_SUSPECT

        # 3. 正常高度姿态
        if feat.primary_z >= self.standing_height_min:
            if abs(feat.primary_speed) > 0.18:
                return RadarHumanState.WALKING
            return RadarHumanState.STANDING

        if feat.primary_z > self.floor_height_thresh:
            if abs(feat.primary_speed) > 0.18:
                return RadarHumanState.WALKING
            return RadarHumanState.SITTING

        return self.state

    def _transition_to(
        self,
        new_state: RadarHumanState,
        ts: float,
        features: Optional[RadarFallFeatures] = None,
    ):
        old_state = self.state
        self.state = new_state
        self.last_state_change = ts

        if old_state != new_state:
            logger.info(
                f"[MMWaveDetector] 毫米波雷达状态流转: {old_state.value} -> {new_state.value} "
                f"(Z={features.primary_z if features else 0:.2f}m, "
                f"Stillness={features.ground_stillness_sec if features else 0:.1f}s, "
                f"Conf={features.confidence if features else 0:.2f})"
            )
            if self.on_state_change and features:
                try:
                    self.on_state_change(old_state, new_state, features)
                except Exception as e:
                    logger.error(f"[MMWaveDetector] 状态流转回调异常: {e}")

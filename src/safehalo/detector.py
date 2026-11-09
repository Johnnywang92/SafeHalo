"""
SafeHalo 状态判定与跌倒报警机 (detector.py)
专为「纯路由器方案 (csi_recv_router)」优化:
1. 具备自适应基线校准功能 (学习当前房间的本底噪声)
2. 四态判定: EMPTY (空房间) / STATIC (有人静止) / MOVING (有人运动) / FALL (跌倒告警)
3. 跌倒判定时序逻辑: [剧烈突变尖峰] -> [迅速转入持续静止] -> [超时锁定触发告警]
4. 路由器网络抖动容错: 丢包时自动保持状态，抑制瞬态误报
"""

import time
from enum import Enum
from typing import Callable, Optional
from .feature_extractor import CSIFeatures

class HumanState(Enum):
    CALIBRATING = "CALIBRATING"       # 正在校准空房基线
    EMPTY = "EMPTY"                   # 空房间无人
    STATIC = "STATIC"                 # 有人静止 (坐/躺/呼吸微动)
    MOVING = "MOVING"                 # 有人运动 (走动/做家务)
    FALL_SUSPECT = "FALL_SUSPECT"     # 疑似跌倒 (发生冲击，正在监测静止时长)
    FALL_CONFIRMED = "FALL_CONFIRMED" # 确认跌倒 (冲击后倒地静止超时，触发报警)
    ROUTER_OFFLINE = "ROUTER_OFFLINE" # 路由器离线或严重丢包

class RouterCSIDetector:
    def __init__(
        self,
        calibration_seconds: float = 6.0,
        fall_spike_threshold: float = 2.0,
        fall_stillness_seconds: float = 4.0,
        router_max_jitter_ms: float = 250.0,
        on_state_change: Optional[Callable[[HumanState, HumanState, CSIFeatures], None]] = None,
        on_fall_alarm: Optional[Callable[[CSIFeatures], None]] = None,
    ):
        self.calibration_seconds = calibration_seconds
        self.fall_spike_threshold = fall_spike_threshold
        self.fall_stillness_seconds = fall_stillness_seconds
        self.router_max_jitter_ms = router_max_jitter_ms
        self.on_state_change = on_state_change
        self.on_fall_alarm = on_fall_alarm

        self.current_state = HumanState.CALIBRATING
        self.state_start_time = 0.0

        # 校准统计数据
        self.calib_samples = 0
        self.base_variance = 0.0001
        self.base_diff = 0.001
        self.calib_start_time: Optional[float] = None
        self._calib_var_list = []
        self._calib_diff_list = []

        # 跌倒检测状态跟踪
        self.fall_spike_time: Optional[float] = None
        self.last_moving_time: float = 0.0

        # 状态去抖动
        self.state_hold_count = 0
        self.pending_state: Optional[HumanState] = None

    def start_calibration(self):
        """重新进入空房基线校准模式"""
        self.current_state = HumanState.CALIBRATING
        self.calib_start_time = None
        self._calib_var_list.clear()
        self._calib_diff_list.clear()

    def update(self, features: CSIFeatures) -> HumanState:
        now = features.timestamp

        # 检查路由器连通性与健康度
        if features.sample_rate_hz < 3.0:
            if self.current_state != HumanState.ROUTER_OFFLINE:
                self._switch_state(HumanState.ROUTER_OFFLINE, features)
            return self.current_state

        # 1. 基线校准阶段 (Calibration)
        if self.current_state == HumanState.CALIBRATING:
            if self.calib_start_time is None:
                self.calib_start_time = now

            self._calib_var_list.append(features.mean_variance)
            self._calib_diff_list.append(features.diff_energy)

            elapsed = now - self.calib_start_time
            if elapsed >= self.calibration_seconds:
                # 校准完成，取中位数作为稳定基线
                self._calib_var_list.sort()
                self._calib_diff_list.sort()
                n = len(self._calib_var_list)
                self.base_variance = max(1e-6, self._calib_var_list[n // 2])
                self.base_diff = max(1e-5, self._calib_diff_list[n // 2])
                self._switch_state(HumanState.EMPTY, features)
            return self.current_state

        # 2. 计算相对于空房基线的动态倍率
        var_ratio = features.mean_variance / self.base_variance
        diff_ratio = features.diff_energy / self.base_diff

        # 3. 跌倒关键冲击与时序判定
        # 真正的跌倒由两段组成:
        # 阶段 A (冲击): 剧烈运动/冲击峰值 (峰均比突发抬高 spike_ratio >= threshold 且相对差分抬高)
        # 阶段 B (骤停): 冲击发生后 3 秒内身体骤然停止运动 (diff_ratio 快速回落到静止/微动区间)
        is_high_impact = (features.spike_ratio >= self.fall_spike_threshold and diff_ratio > 2.5) or (var_ratio > 8.0 and features.spike_ratio > 1.8)

        if is_high_impact and self.current_state in (HumanState.MOVING, HumanState.STATIC):
            self.fall_spike_time = now

        # 如果记录到了最近的冲击，且运动能量迅速跌入低谷 (倒地骤停)，切入 FALL_SUSPECT
        if self.fall_spike_time and (now - self.fall_spike_time <= 3.5):
            if diff_ratio < 2.0 and self.current_state != HumanState.FALL_SUSPECT:
                self._switch_state(HumanState.FALL_SUSPECT, features)
                return self.current_state

        # 4. 处于「疑似跌倒」状态时的追踪
        if self.current_state == HumanState.FALL_SUSPECT:
            stillness_duration = now - self.fall_spike_time if self.fall_spike_time else 0.0

            # 如果检测到剧烈起身走动 (解除误报)
            if diff_ratio > 2.4 and var_ratio > 3.5:
                self.fall_spike_time = None
                self._switch_state(HumanState.MOVING, features)
                return self.current_state

            # 如果持续处于低能静止状态超时 (倒地不起)
            if stillness_duration >= self.fall_stillness_seconds:
                self._switch_state(HumanState.FALL_CONFIRMED, features)
                if self.on_fall_alarm:
                    self.on_fall_alarm(features)
                return self.current_state

            # 保持疑似状态观测
            return self.current_state

        # 5. 处于「确认跌倒」状态时
        if self.current_state == HumanState.FALL_CONFIRMED:
            # 只有当检测到明确起身走动时才解除告警
            if diff_ratio > 2.5 and var_ratio > 3.5:
                self.fall_spike_time = None
                self._switch_state(HumanState.MOVING, features)
            return self.current_state

        # 6. 常规状态判定 (MOVING / STATIC / EMPTY)
        # 运动判决: 走动时帧间差分和方差有大幅度提升
        is_moving = (diff_ratio > 2.5 and var_ratio > 3.0) or (diff_ratio > 3.2)

        # 存在判决 (有人静止/呼吸微动): 方差或微动比空房基线有稳定轻度抬升
        is_present = (var_ratio > 1.35 or diff_ratio > 1.3)

        if is_moving:
            raw_target = HumanState.MOVING
        elif is_present:
            raw_target = HumanState.STATIC
        else:
            raw_target = HumanState.EMPTY

        # 去抖动平滑 (连续 3 帧一致才切换状态，避免单帧波动)
        if raw_target != self.current_state:
            if self.pending_state == raw_target:
                self.state_hold_count += 1
                if self.state_hold_count >= 3:
                    self._switch_state(raw_target, features)
                    self.pending_state = None
                    self.state_hold_count = 0
            else:
                self.pending_state = raw_target
                self.state_hold_count = 1
        else:
            self.pending_state = None
            self.state_hold_count = 0

        return self.current_state

    def _switch_state(self, new_state: HumanState, features: CSIFeatures):
        old_state = self.current_state
        self.current_state = new_state
        self.state_start_time = features.timestamp
        if self.on_state_change:
            self.on_state_change(old_state, new_state, features)

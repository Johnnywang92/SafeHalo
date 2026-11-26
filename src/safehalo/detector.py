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
from .pet_filter import PetImmunityFilter, PetDiscriminationResult
from .targeted_inspector import TargetedFallInspector, TargetedInspectionState, TargetedInspectionResult
from .object_confirm import ObjectConfirmWatcher, DEFAULT_CV_THRESHOLD, MIN_INTERVALS
from .auto_calibrator import ContinuousAutoCalibrator
from .logger import logger

class HumanState(Enum):
    CALIBRATING = "CALIBRATING"       # 正在校准空房基线
    EMPTY = "EMPTY"                   # 空房间无人
    STATIC = "STATIC"                 # 有人静止 (坐/躺/呼吸微动)
    MOVING = "MOVING"                 # 有人运动 (走动/做家务)
    PET_ACTIVITY = "PET_ACTIVITY"     # 宠物活动 (已自动识别并抑制，防误报)
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
        on_request_burst_rate: Optional[Callable[[int], None]] = None,
        on_trigger_challenge: Optional[Callable[[str], None]] = None,
        on_challenge_intercepted: Optional[Callable[[str], None]] = None,
        auto_calibrator: Optional[ContinuousAutoCalibrator] = None,
        object_confirm_seconds: float = 0.0,
        object_confirm_cv: float = DEFAULT_CV_THRESHOLD,
        object_confirm_min_intervals: int = MIN_INTERVALS,
        enable_peak_regularity: bool = False,
        peak_regularity_cv: float = DEFAULT_CV_THRESHOLD,
    ):
        self.calibration_seconds = calibration_seconds
        self.fall_spike_threshold = fall_spike_threshold
        self.fall_stillness_seconds = fall_stillness_seconds
        self.router_max_jitter_ms = router_max_jitter_ms
        self.on_state_change = on_state_change
        self.on_fall_alarm = on_fall_alarm
        self.on_request_burst_rate = on_request_burst_rate
        self.on_trigger_challenge = on_trigger_challenge
        self.on_challenge_intercepted = on_challenge_intercepted
        self.enable_peak_regularity = enable_peak_regularity
        self.peak_regularity_cv = peak_regularity_cv

        self.current_state = HumanState.CALIBRATING
        self.state_start_time = 0.0

        # 多日环境基线自动学习器 (阶段 2)
        self.auto_calibrator = auto_calibrator or ContinuousAutoCalibrator()

        # 校准统计数据
        self.calib_samples = 0
        self.base_variance = self.auto_calibrator.state.base_variance
        self.base_diff = self.auto_calibrator.state.base_diff
        self.calib_start_time: Optional[float] = None
        self._calib_var_list = []
        self._calib_diff_list = []

        # 跌倒检测状态跟踪
        self.fall_spike_time: Optional[float] = None
        self.last_moving_time: float = 0.0

        # 宠物抗干扰滤波器
        self.pet_filter = PetImmunityFilter()
        self.latest_spr: float = 0.0

        # 定向深度排查与主动核验引擎
        self.targeted_inspector = TargetedFallInspector(
            inspection_duration_seconds=fall_stillness_seconds,
            on_request_burst_rate=on_request_burst_rate,
            on_trigger_challenge=on_trigger_challenge,
            enable_peak_regularity=enable_peak_regularity,
            peak_regularity_cv=peak_regularity_cv,
        )

        # 第二阶段物品确认（默认关闭: object_confirm_seconds <= 0）
        self.object_confirm_seconds = object_confirm_seconds
        self.object_confirm_cv = object_confirm_cv
        self.object_confirm_min_intervals = object_confirm_min_intervals
        self.object_watcher: Optional[ObjectConfirmWatcher] = None

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
            if self.object_watcher is not None:
                # 丢包窗口内的观察不可信，放弃本次确认（下一次完整观察重新开始）
                logger.log_object_watch("aborted", "路由器离线，放弃物品确认观察")
                self.object_watcher = None
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
                # 校准完成，取中位数并经由多日自动校准器检验抗污染
                self._calib_var_list.sort()
                self._calib_diff_list.sort()
                n = len(self._calib_var_list)
                cand_var = max(1e-6, self._calib_var_list[n // 2])
                cand_diff = max(1e-5, self._calib_diff_list[n // 2])
                eff_var, eff_diff, is_clean = self.auto_calibrator.validate_initial_calibration(
                    candidate_variance=cand_var,
                    candidate_diff=cand_diff,
                    timestamp=now,
                )
                self.base_variance = eff_var if not is_clean else cand_var
                self.base_diff = eff_diff if not is_clean else cand_diff
                self._switch_state(HumanState.EMPTY, features)
            return self.current_state

        # 2. 计算相对于空房基线的动态倍率
        var_ratio = features.mean_variance / self.base_variance
        diff_ratio = features.diff_energy / self.base_diff

        # 宠物识别评估 (Pet Immunity Evaluation)
        pet_res = self.pet_filter.evaluate(
            subcarrier_variances=features.subcarrier_variances,
            base_variance=self.base_variance,
            diff_energy=features.diff_energy,
            base_diff=self.base_diff,
        )
        self.latest_spr = pet_res.subcarrier_participation

        # 3. 跌倒关键冲击与时序判定
        # 真正的跌倒由两段组成:
        # 阶段 A (冲击): 剧烈运动/冲击峰值 (瞬态高方差或高差分冲击)
        # 阶段 B (骤停): 冲击发生后 3 秒内身体骤然停止运动 (diff_ratio 快速回落到静止/微动区间)
        is_high_impact = (
            (features.spike_ratio >= self.fall_spike_threshold and diff_ratio > 2.5) or
            (var_ratio > 8.0 and features.spike_ratio > 1.8) or
            (features.diff_energy > 0.035 and features.mean_variance > 0.003)
        )

        if is_high_impact and self.current_state in (HumanState.MOVING, HumanState.STATIC):
            self.fall_spike_time = now
            logger.log_fall_suspect(features.spike_ratio, features.diff_energy, "检测到剧烈坠落冲击")

        # 如果记录到了最近的冲击，且运动能量迅速跌入低谷 (倒地骤停)，切入 FALL_SUSPECT
        if self.fall_spike_time and (now - self.fall_spike_time <= 3.5):
            if diff_ratio < 2.5 and self.current_state != HumanState.FALL_SUSPECT:
                self._switch_state(HumanState.FALL_SUSPECT, features)
                return self.current_state

        # 3.5 第二阶段物品确认观察中（开关开启且阶段一已判 OBJECT）:
        #     保持 FALL_SUSPECT 不报警，直到长窗结论出来；只允许升级为跌倒。
        if self.current_state == HumanState.FALL_SUSPECT and self.object_watcher is not None:
            amps = features.current_amplitudes
            mean_amp = 0.0
            if amps:
                for a in amps:
                    mean_amp += a
                mean_amp /= len(amps)
            result = self.object_watcher.feed(now, mean_amp)
            if result is not None:
                self.object_watcher = None
                if result.escalate:
                    logger.log_object_watch(
                        "escalate", result.details,
                        peaks=result.peaks, intervals=result.intervals, cv=result.cv,
                    )
                    logger.log_fall_confirmed(result.details, result.bpm, 0.90)
                    self.fall_spike_time = None
                    self._switch_state(HumanState.FALL_CONFIRMED, features)
                    if self.on_fall_alarm:
                        self.on_fall_alarm(features)
                else:
                    phase = "insufficient" if result.insufficient else "confirmed"
                    logger.log_object_watch(
                        phase, result.details,
                        peaks=result.peaks, intervals=result.intervals, cv=result.cv,
                    )
                    self.fall_spike_time = None
                    self._switch_state(HumanState.EMPTY, features)
            return self.current_state

        # 4. 处于「疑似跌倒」状态时，启动定向深度核验
        if self.current_state == HumanState.FALL_SUSPECT:
            if self.targeted_inspector.current_state == TargetedInspectionState.STANDBY:
                self.targeted_inspector.trigger_inspection(timestamp=now, trigger_reason="初筛疑似跌倒冲击")

            # 驱动定向核验引擎分析地面微弱体征
            verdict = self.targeted_inspector.update_frame(
                timestamp=now,
                diff_energy=features.diff_energy,
                base_diff=self.base_diff,
                subcarrier_amplitudes=features.current_amplitudes
            )

            if verdict:
                if verdict.state == TargetedInspectionState.VERDICT_FALL:
                    # 确诊倒地受困 (探测到微弱呼吸体征但无起身)
                    logger.log_fall_confirmed(verdict.details, verdict.respiration_bpm, verdict.confidence)
                    self._switch_state(HumanState.FALL_CONFIRMED, features)
                    if self.on_fall_alarm:
                        try:
                            self.on_fall_alarm(features, verdict)
                        except TypeError:
                            self.on_fall_alarm(features)
                    return self.current_state

                elif verdict.state == TargetedInspectionState.VERDICT_RECOVERED:
                    # 自主站起翻身或质询期内应答，解除预警
                    logger.log_event("INSPECTION_RECOVERED", verdict.details, level="INFO")
                    if verdict.challenge_intercepted and self.on_challenge_intercepted:
                        try:
                            self.on_challenge_intercepted(verdict.details)
                        except Exception:
                            pass
                    self.fall_spike_time = None
                    self._switch_state(HumanState.MOVING, features)
                    return self.current_state

                elif verdict.state == TargetedInspectionState.VERDICT_OBJECT:
                    if self.object_confirm_seconds > 0:
                        # 阶段一判为物品: 不立即结案，启动长窗规整度确认。
                        # 观察期内保持 FALL_SUSPECT（趋向报警的安全侧），不报警。
                        self.object_watcher = ObjectConfirmWatcher(
                            start_ts=now,
                            duration_seconds=self.object_confirm_seconds,
                            cv_threshold=self.object_confirm_cv,
                            min_intervals=self.object_confirm_min_intervals,
                        )
                        logger.log_object_watch(
                            "start",
                            f"阶段一判为物品，启动 {self.object_confirm_seconds:.0f}s 长窗确认观察",
                        )
                        return self.current_state
                    # 排除人类跌倒 (无生命体征，系重物掉落地面)
                    logger.log_event("INSPECTION_OBJECT", verdict.details, level="INFO")
                    self.fall_spike_time = None
                    self._switch_state(HumanState.EMPTY, features)
                    return self.current_state

            # 继续保持 FALL_SUSPECT 观测中
            return self.current_state

        # 5. 处于「确认跌倒」状态时
        if self.current_state == HumanState.FALL_CONFIRMED:
            # 只有当检测到明确起身走动时才解除告警
            if diff_ratio > 2.5 and var_ratio > 3.5:
                self.fall_spike_time = None
                self._switch_state(HumanState.MOVING, features)
            return self.current_state

        # 6. 常规状态判定 (MOVING / STATIC / PET / EMPTY)
        # 运动判决: 走动时帧间差分和方差有大幅度提升
        is_moving = (diff_ratio > 2.5 and var_ratio > 3.0) or (diff_ratio > 3.2)

        # 存在判决 (有人静止/呼吸微动): 方差或微动比空房基线有稳定轻度抬升
        is_present = (var_ratio > 1.35 or diff_ratio > 1.3)

        if pet_res.is_pet and not self.fall_spike_time:
            # 识别到地面宠物活动，且近期无剧烈人体冲击，自动抑制人体判定
            raw_target = HumanState.PET_ACTIVITY
        elif is_moving:
            raw_target = HumanState.MOVING
        elif is_present:
            raw_target = HumanState.STATIC
        else:
            raw_target = HumanState.EMPTY
            # 在持续 EMPTY 静默阶段，驱动多日环境基线自动微调演进 (温漂与家具挪动自适应)
            updated_base = self.auto_calibrator.observe_frame(
                is_empty_or_quiet=True,
                mean_variance=features.mean_variance,
                diff_energy=features.diff_energy,
                timestamp=now,
            )
            if updated_base:
                self.base_variance, self.base_diff = updated_base

        if raw_target != HumanState.EMPTY:
            self.auto_calibrator.observe_frame(
                is_empty_or_quiet=False,
                mean_variance=features.mean_variance,
                diff_energy=features.diff_energy,
                timestamp=now,
            )

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
        logger.log_state_change(
            old_state.name,
            new_state.name,
            new_state.value,
            metrics={"variance": round(features.mean_variance, 6), "diff_energy": round(features.diff_energy, 5)}
        )
        if self.on_state_change:
            self.on_state_change(old_state, new_state, features)

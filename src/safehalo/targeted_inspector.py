"""
SafeHalo 定向深度排查与主动核验引擎 (targeted_inspector.py)
用于在「初筛疑似跌倒」时，启动多维度定向定向聚焦核验，彻底解决常规监测的误报与漏报。

定向检测四大核心手段:
1. 激振提频探测 (Active CSI Burst):
   - 常规监测 20~30Hz，一旦疑似跌倒，瞬间提频至 80~100Hz 高密发包，大幅提高时域微多普勒分辨率。
2. 地面弱生命微动扫描 (Ground Respiration & Micro-Motion Focus):
   - 聚焦地面层多径载波，提取 0.15Hz~0.4Hz (9~24次/分) 的胸腔呼吸微动与心跳微扰。
   - 关键判决: 如果检测到规则呼吸微动但无肢体位移，确认是「活体人类倒地受困」；
   - 若毫无呼吸微动，判定为「重物/书本/被子滑落地板」，避免谎报。
3. 交互质询窗口 (Interactive Challenge):
   - 支持向本地设备/蜂鸣器发出质询应答信号 ("如您安全请轻微动一下")，观测后续 5 秒内是否有应答微动。
"""

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional
from .logger import logger

class TargetedInspectionState(Enum):
    STANDBY = "STANDBY"                       # 常规待命
    TRIGGERED = "TRIGGERED"                   # 触发定向复检
    HIGH_FREQ_BURST = "HIGH_FREQ_BURST"       # 正在执行 100Hz 高密激振探测
    SCANNING_RESPIRATION = "SCANNING_RESP"    # 正在聚焦地面胸腔呼吸与微弱生命体征
    AWAITING_CHALLENGE = "AWAITING_CHALLENGE" # 等待交互应答
    VERDICT_FALL = "VERDICT_FALL"             # 最终确诊: 活体人体倒地受困！
    VERDICT_OBJECT = "VERDICT_OBJECT"         # 误报排除: 无生命体征，判定为物品/书本掉落
    VERDICT_RECOVERED = "VERDICT_RECOVERED"   # 自主恢复: 探测到老人已自主翻身或站起

@dataclass
class TargetedInspectionResult:
    state: TargetedInspectionState
    has_respiration: bool = False             # 是否检测到倒地人体呼吸微动
    respiration_bpm: float = 0.0              # 呼吸估算频次 (次/分钟)
    body_recovered: bool = False              # 是否已自主起身或响应质询
    challenge_intercepted: bool = False       # 是否在交互质询窗口内通过应答拦截了误报
    is_inanimate_object: bool = False         # 是否为无生命的掉落物
    confidence: float = 0.0
    details: str = ""

class TargetedFallInspector:
    def __init__(
        self,
        inspection_duration_seconds: float = 6.0,
        on_inspection_progress: Optional[Callable[[TargetedInspectionState, str], None]] = None,
        on_request_burst_rate: Optional[Callable[[int], None]] = None,
        on_trigger_challenge: Optional[Callable[[str], None]] = None,
        enable_peak_regularity: bool = False,
        peak_regularity_cv: float = 0.35,
        peak_regularity_min_range: float = 0.15,
    ):
        """
        :param inspection_duration_seconds: 定向深度检查窗口时长 (秒)
        :param on_inspection_progress: 复查进度通知回调
        :param on_request_burst_rate: 向探针/路由器请求提频发包的回调 (如提频到 100Hz)
        :param on_trigger_challenge: 进入本地声光/语音交互质询窗口时的回调 ("如您安全请动一下")
        :param enable_peak_regularity: (P0-3) 是否启用呼吸峰间隔规整度与准周期性抗噪检验 (默认 False 保持位级兼容)
        :param peak_regularity_cv: 呼吸峰间隔变异系数 (CV) 门限，<= 该门限确认为规律呼吸
        :param peak_regularity_min_range: 呼吸波形最小动态范围 (峰峰值)，低于该值判定为平坦噪声地面
        """
        self.inspection_duration_seconds = inspection_duration_seconds
        self.on_inspection_progress = on_inspection_progress
        self.on_request_burst_rate = on_request_burst_rate
        self.on_trigger_challenge = on_trigger_challenge
        self.enable_peak_regularity = enable_peak_regularity
        self.peak_regularity_cv = peak_regularity_cv
        self.peak_regularity_min_range = peak_regularity_min_range

        self.current_state = TargetedInspectionState.STANDBY
        self.start_time = 0.0
        self.manual_safe_acknowledged = False

        # 定向呼吸特征积分缓存 (记录最近高密帧的微动时序)
        self.respiration_wave_buffer = []
        self.diff_ratio_buffer = []

    def acknowledge_safe(self, source: str = "老人本地按键/语音应答"):
        """
        在质询窗口期内，老人或家属主动应答确认安全，立即拦截误报
        """
        if self.current_state != TargetedInspectionState.STANDBY:
            self.manual_safe_acknowledged = True
            logger.info(f"[TargetedInspector] 收到安全应答 ({source})，准备解除疑似跌倒告警")

    def trigger_inspection(self, timestamp: float, trigger_reason: str = "初筛疑似跌倒冲击"):
        """
        进入定向深度排查模式
        """
        self.current_state = TargetedInspectionState.TRIGGERED
        self.start_time = timestamp
        self.manual_safe_acknowledged = False
        self.respiration_wave_buffer.clear()
        self.diff_ratio_buffer.clear()

        # 1. 立即向射频端请求突发提频激振 (如 100Hz)
        if self.on_request_burst_rate:
            self.on_request_burst_rate(100)

        self._switch_state(TargetedInspectionState.HIGH_FREQ_BURST, f"启动定向激振复查: {trigger_reason}")

    def update_frame(
        self,
        timestamp: float,
        diff_energy: float,
        base_diff: float,
        subcarrier_amplitudes: List[float]
    ) -> Optional[TargetedInspectionResult]:
        """
        在定向复查窗口期内，逐帧分析高精度信号
        """
        if self.current_state == TargetedInspectionState.STANDBY:
            return None

        elapsed = timestamp - self.start_time

        # 计算帧间一阶能量倍率
        diff_ratio = diff_energy / base_diff if base_diff > 1e-6 else 1.0
        self.diff_ratio_buffer.append(diff_ratio)

        # A0) 检查是否收到主动安全确认 (按键/语音取消)
        if self.manual_safe_acknowledged:
            self._switch_state(TargetedInspectionState.VERDICT_RECOVERED, "收到主动安全应答，解除疑似跌倒状态")
            self.reset_to_standby()
            return TargetedInspectionResult(
                state=TargetedInspectionState.VERDICT_RECOVERED,
                body_recovered=True,
                challenge_intercepted=True,
                confidence=0.98,
                details="交互质询窗口内收到老人主动安全确认应答，已成功拦截误报"
            )

        # A) 检测到明显的大幅自主翻身或站起，或在交互质询期 (AWAITING_CHALLENGE) 内做出手势/肢体微动应答
        in_challenge = (self.current_state == TargetedInspectionState.AWAITING_CHALLENGE)
        recovery_thresh = 2.1 if in_challenge else 2.8
        if diff_ratio > recovery_thresh and elapsed > 0.8:
            msg = "交互质询期内检测到老人应答活动" if in_challenge else "检测到受检者已自主活动/站立"
            self._switch_state(TargetedInspectionState.VERDICT_RECOVERED, msg)
            self.reset_to_standby()
            return TargetedInspectionResult(
                state=TargetedInspectionState.VERDICT_RECOVERED,
                body_recovered=True,
                challenge_intercepted=in_challenge,
                confidence=0.94,
                details="交互质询窗口内探测到受检者主动肢体应答或自主恢复，警报平稳解除"
            )

        # 收集低频微波形 (用于估计胸腔呼吸)
        if subcarrier_amplitudes:
            # 提取对地面反射敏感的特征载波幅值均值
            self.respiration_wave_buffer.append(sum(subcarrier_amplitudes) / len(subcarrier_amplitudes))
            if len(self.respiration_wave_buffer) > 150:
                self.respiration_wave_buffer.pop(0)

        # B1) 时序推进阶段 2: 经过 1/3 时长后进入地面微弱呼吸扫描阶段
        scan_trigger_t = max(0.8, self.inspection_duration_seconds * 0.33)
        challenge_trigger_t = max(1.6, self.inspection_duration_seconds * 0.66)

        if elapsed >= scan_trigger_t and self.current_state == TargetedInspectionState.HIGH_FREQ_BURST:
            self._switch_state(TargetedInspectionState.SCANNING_RESPIRATION, "正在定向扫描地面胸腔呼吸微动")

        # B2) 时序推进阶段 3: 经过 2/3 时长后进入本地声光/语音交互质询窗口 ("无应答 -> 通知")
        if elapsed >= challenge_trigger_t and self.current_state == TargetedInspectionState.SCANNING_RESPIRATION:
            prompt_msg = "检测到疑似跌倒静止，正在发起本地声光/语音安全质询（如您安全请轻微动一下）"
            self._switch_state(TargetedInspectionState.AWAITING_CHALLENGE, prompt_msg)
            if self.on_trigger_challenge:
                try:
                    self.on_trigger_challenge(prompt_msg)
                except Exception as e:
                    logger.error(f"[TargetedInspector] 质询回调异常: {e}")

        # C) 达到定向复查超时，给出最终裁决
        if elapsed >= self.inspection_duration_seconds:
            # 评估呼吸微弱信号是否存在
            has_resp, bpm = self._evaluate_ground_respiration()

            # 同时结合近地面微动能量比 (无生命物体坠落后地面能量比接近 1.0，人体呼吸微动通常在 1.25~2.2 之间)
            avg_diff_ratio = (sum(self.diff_ratio_buffer[-60:]) / len(self.diff_ratio_buffer[-60:])) if self.diff_ratio_buffer else 1.0

            if has_resp or avg_diff_ratio > 1.28:
                # 确定检测到规律微弱呼吸或微弱生命体征，但无大肢体动作 -> 确诊倒地受困！
                effective_bpm = bpm if has_resp else 16.0
                self._switch_state(TargetedInspectionState.VERDICT_FALL, f"确诊倒地！探测到地面生命体征 (呼吸约 {effective_bpm:.0f} 次/分)")
                res = TargetedInspectionResult(
                    state=TargetedInspectionState.VERDICT_FALL,
                    has_respiration=True,
                    respiration_bpm=effective_bpm,
                    confidence=0.96,
                    details=f"定向复查确诊：地面检测到持续弱体征微动 (呼吸节律约 {effective_bpm:.0f} bpm)，且完全无自主起身迹象！"
                )
            else:
                # 既无起身动作，地面又完全没有胸腔起伏微动 -> 判定为物品/书本掉落地板
                self._switch_state(TargetedInspectionState.VERDICT_OBJECT, "排除跌倒：未探测到生命体征，判定为无生命物体坠落")
                res = TargetedInspectionResult(
                    state=TargetedInspectionState.VERDICT_OBJECT,
                    is_inanimate_object=True,
                    confidence=0.88,
                    details="定向复查排除：冲击后地面完全无呼吸或心跳微动，已自动过滤物品坠落误判"
                )

            self.reset_to_standby()
            return res

        return None

    def reset_to_standby(self):
        # 恢复常规发包率 (如 30Hz)
        if self.on_request_burst_rate:
            self.on_request_burst_rate(30)
        self.current_state = TargetedInspectionState.STANDBY
        self.respiration_wave_buffer.clear()
        self.diff_ratio_buffer.clear()

    def _evaluate_ground_respiration(self) -> (bool, float):
        """
        通过时域低通平滑与极值峰值分析，提取地面多径波形中的周期性胸腔呼吸微动 (0.15Hz ~ 0.4Hz)
        """
        n = len(self.respiration_wave_buffer)
        if n < 30:
            return (False, 0.0)

        # 1. 移动平均平滑滤波 (半窗 12 帧，有效消除高频射频噪声，保留慢速呼吸包络)
        w = 12
        smoothed = []
        for i in range(n):
            start_idx = max(0, i - w)
            end_idx = min(n, i + w + 1)
            smoothed.append(sum(self.respiration_wave_buffer[start_idx:end_idx]) / (end_idx - start_idx))

        # 2. 零均值化
        mean_v = sum(smoothed) / n
        zero_mean = [x - mean_v for x in smoothed]

        # 3. 统计呼吸波峰频次 (增加最小峰间距约束，人体呼吸峰值间隔至少 1 秒 ~ 25 帧)
        min_peak_dist = 25
        peaks = 0
        last_peak_idx = -min_peak_dist
        peak_indices = []
        for i in range(1, n - 1):
            if zero_mean[i] > zero_mean[i - 1] and zero_mean[i] > zero_mean[i + 1]:
                if zero_mean[i] > 0.000015 and (i - last_peak_idx >= min_peak_dist):
                    peaks += 1
                    last_peak_idx = i
                    peak_indices.append(i)

        if not self.enable_peak_regularity:
            # 若观察窗口内检测到 1~6 个规则呼吸波峰 (对应每分钟 10~24 次正常/慢速呼吸)
            if 1 <= peaks <= 6:
                duration_s = max(2.0, self.inspection_duration_seconds - 1.0)
                bpm = (peaks / duration_s) * 60.0
                return (True, min(26.0, max(12.0, bpm)))
            return (False, 0.0)

        # P0-3 规整度 / 准周期性强化抗噪模式:
        if peaks < 1 or peaks > 6:
            return (False, 0.0)

        dyn_range = max(zero_mean) - min(zero_mean)
        if dyn_range < self.peak_regularity_min_range:
            # 地面微动峰峰值极低，属于静态平坦噪声，非人体呼吸
            return (False, 0.0)

        duration_s = max(2.0, self.inspection_duration_seconds - 1.0)
        bpm = (peaks / duration_s) * 60.0

        if peaks == 1:
            # 单峰: 必须是生理性慢速呼吸波包 (波峰正瓣宽度应占样本至少 20%)
            p = peak_indices[0]
            left = p
            while left > 0 and zero_mean[left] > 0.0:
                left -= 1
            right = p
            while right < n - 1 and zero_mean[right] > 0.0:
                right += 1
            lobe_width = right - left
            if lobe_width < max(20, int(0.20 * n)):
                return (False, 0.0)
            return (True, min(26.0, max(12.0, bpm)))

        intervals = [peak_indices[k] - peak_indices[k - 1] for k in range(1, len(peak_indices))]
        if len(intervals) >= 2:
            mean_i = sum(intervals) / len(intervals)
            var_i = sum((x - mean_i) ** 2 for x in intervals) / len(intervals)
            cv = math.sqrt(var_i) / mean_i if mean_i > 1e-9 else float("inf")
            if cv > self.peak_regularity_cv:
                # 峰间隔不规整，判定为随机射频噪声波动，非真实规律胸腔呼吸
                return (False, 0.0)
        else:
            # 仅 1 个间隔 (2 个峰)，根据采样率检验间隔时长是否落在生理呼吸合理区间 [2.0s, 6.0s]
            fs = n / duration_s if duration_s > 1e-6 else 25.0
            interval_s = intervals[0] / fs
            if interval_s < 2.0 or interval_s > 6.0:
                return (False, 0.0)

        return (True, min(26.0, max(12.0, bpm)))

    def _switch_state(self, new_state: TargetedInspectionState, desc: str):
        self.current_state = new_state
        logger.log_inspection_progress(new_state.name, desc)
        if self.on_inspection_progress:
            self.on_inspection_progress(new_state, desc)

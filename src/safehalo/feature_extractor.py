"""
SafeHalo 路由器 CSI 特征提取器 (feature_extractor.py)
从滑动窗口内的多子载波归一化幅值流中提取用于状态判断的关键统计与时频特征。
"""

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

@dataclass
class CSIFeatures:
    timestamp: float = 0.0
    mean_variance: float = 0.0        # 子载波滑动方差均值 (MVS)
    max_variance: float = 0.0         # 最大子载波方差
    diff_energy: float = 0.0          # 帧间一阶差分能量 (高频活动指示)
    spike_ratio: float = 0.0          # 峰均比 (判断跌倒瞬时突变)
    respiration_energy: float = 0.0   # 低频平缓能量 (静止呼吸微动指示)
    sample_rate_hz: float = 0.0       # 路由器当前的实时采样率 (Hz)
    packet_interval_jitter: float = 0.0 # 路由器发包抖动 (ms)
    subcarrier_variances: List[float] = field(default_factory=list) # 各载波独立方差分布
    current_amplitudes: List[float] = field(default_factory=list)   # 当前帧归一化载波幅值向量

class RouterFeatureExtractor:
    def __init__(self, window_size: int = 50):
        """
        :param window_size: 滑动窗口长度 (若采样率 ~50Hz，50 帧对应 1.0 秒时序窗口)
        """
        self.window_size = window_size
        self.frame_buffer: Deque[List[float]] = deque(maxlen=window_size)
        self.time_buffer: Deque[float] = deque(maxlen=window_size)
        self.diff_buffer: Deque[float] = deque(maxlen=window_size)

    def push_frame(self, timestamp: float, amplitudes: List[float]) -> Optional[CSIFeatures]:
        """
        推入一帧经过预处理与归一化的子载波幅值向量，提取特征
        """
        if not amplitudes:
            return None

        # 计算帧间一阶差分 (高频能量指示)
        if self.frame_buffer:
            prev = self.frame_buffer[-1]
            diff = sum(abs(a - b) for a, b in zip(amplitudes, prev)) / len(amplitudes)
        else:
            diff = 0.0

        self.frame_buffer.append(amplitudes)
        self.time_buffer.append(timestamp)
        self.diff_buffer.append(diff)

        # 窗口未填满前，先建立初期统计
        if len(self.frame_buffer) < max(5, self.window_size // 3):
            return None

        num_frames = len(self.frame_buffer)
        num_subcarriers = len(amplitudes)

        # 1. 计算每个子载波在时间窗口内的方差
        variances = []
        for sc in range(num_subcarriers):
            vals = [self.frame_buffer[t][sc] for t in range(num_frames)]
            mean_val = sum(vals) / num_frames
            var = sum((x - mean_val) ** 2 for x in vals) / num_frames
            variances.append(var)

        mean_variance = sum(variances) / len(variances) if variances else 0.0
        max_variance = max(variances) if variances else 0.0

        # 2. 帧间差分能量 (反映身体各部位多普勒运动频度)
        diff_energy = sum(self.diff_buffer) / len(self.diff_buffer)

        # 3. 峰值比与冲击能量 (用于捕获倒地瞬间的尖峰冲击)
        # 近期 5 帧的瞬时最大突变
        short_window = list(self.diff_buffer)[-5:] if len(self.diff_buffer) >= 5 else list(self.diff_buffer)
        recent_max_diff = max(short_window) if short_window else 0.0
        mean_diff = diff_energy if diff_energy > 1e-6 else 1e-6
        spike_ratio = recent_max_diff / mean_diff

        # 4. 估计路由器当前发包频率与抖动 (Router health)
        intervals = []
        for i in range(1, len(self.time_buffer)):
            dt = self.time_buffer[i] - self.time_buffer[i - 1]
            if dt > 0:
                intervals.append(dt)

        if intervals:
            avg_dt = sum(intervals) / len(intervals)
            sample_rate = 1.0 / avg_dt if avg_dt > 1e-4 else 0.0
            dt_variance = sum((dt - avg_dt) ** 2 for dt in intervals) / len(intervals)
            jitter_ms = math.sqrt(dt_variance) * 1000.0
        else:
            sample_rate = 0.0
            jitter_ms = 0.0

        # 5. 微动呼吸能量估算 (在差分能量极低但方差高于纯噪底时显著)
        # 用方差均值与差分能量之比表征慢速微小位移 (如胸腔起伏)
        respiration_energy = mean_variance / (diff_energy + 1e-5)

        return CSIFeatures(
            timestamp=timestamp,
            mean_variance=mean_variance,
            max_variance=max_variance,
            diff_energy=diff_energy,
            spike_ratio=spike_ratio,
            respiration_energy=respiration_energy,
            sample_rate_hz=sample_rate,
            packet_interval_jitter=jitter_ms,
            subcarrier_variances=variances,
            current_amplitudes=list(amplitudes),
        )

"""
SafeHalo 路由器 CSI 信号预处理与去噪模块 (signal_filter.py)
专为「纯路由器方案 (csi_recv_router)」优化:
1. 解决路由器自动增益控制 (AGC) 和功率调节带来的虚假幅值跳变 (L2 Norm 归一化)
2. 剔除无效子载波 (直流 DC、保护频带、极度低信噪比子载波)
3. 异常脉冲滤除 (Hampel / 中值滤波)
"""

import math
from typing import List, Tuple

class RouterSignalFilter:
    def __init__(
        self,
        num_subcarriers: int = 64,
        guard_band_cutoff: int = 6,
        remove_dc: bool = True,
        hampel_window: int = 5,
        hampel_threshold: float = 3.0
    ):
        """
        :param num_subcarriers: 子载波总数 (HT20 默认为 64，实际有效数据子载波通常约 52 个)
        :param guard_band_cutoff: 边缘滤除的保护子载波数
        :param remove_dc: 是否移除中心直流子载波 (DC subcarrier)
        :param hampel_window: 时域去脉冲噪点的滑动窗口大小
        :param hampel_threshold: 离群点判决的西格玛倍数
        """
        self.num_subcarriers = num_subcarriers
        self.guard_band_cutoff = guard_band_cutoff
        self.remove_dc = remove_dc
        self.hampel_window = hampel_window
        self.hampel_threshold = hampel_threshold

        # 历史缓冲队列，用于时域滤波 (子载波数 x 窗口大小)
        self.history_buffers: List[List[float]] = []

    def get_valid_subcarrier_indices(self, total: int) -> List[int]:
        """
        过滤掉外侧保护频带和中心直流载波
        """
        valid = []
        center = total // 2
        for i in range(total):
            # 过滤两端保护频带
            if i < self.guard_band_cutoff or i >= (total - self.guard_band_cutoff):
                continue
            # 过滤直流载波
            if self.remove_dc and (i == 0 or i == center):
                continue
            valid.append(i)
        return valid

    def normalize_agc(self, amplitudes: List[float]) -> List[float]:
        """
        L2 范数归一化 (消除路由器由于动态功率控制与 AGC 增益变化引起的整体幅值漂移)
        A_norm[k] = A[k] / sqrt(sum(A[i]^2))
        """
        if not amplitudes:
            return []

        energy = sum(a * a for a in amplitudes)
        if energy <= 1e-9:
            return [0.0] * len(amplitudes)

        norm_factor = math.sqrt(energy)
        return [a / norm_factor for a in amplitudes]

    def remove_hampel_outliers(self, raw_series: List[float]) -> float:
        """
        单点 Hampel 滤波，使用滑动中位数及中位绝对偏差 (MAD) 替换单个丢包或跳变噪点
        """
        n = len(raw_series)
        if n < 3:
            return raw_series[-1]

        target = raw_series[-1]
        sorted_vals = sorted(raw_series)
        median = sorted_vals[n // 2]

        deviations = sorted(abs(x - median) for x in raw_series)
        mad = deviations[n // 2]

        # 阈值判定: 1.4826 为正态分布下的尺度因子
        threshold = self.hampel_threshold * 1.4826 * mad
        if abs(target - median) > threshold and threshold > 1e-6:
            # 是离群噪点，用中位数替代
            return median
        return target

    def process(self, raw_amplitudes: List[float]) -> List[float]:
        """
        完整滤波链:
        1. 选取有效子载波
        2. AGC 功率归一化
        3. 时域滤波去孤立噪点
        """
        if not raw_amplitudes:
            return []

        # 1. 提取有效子载波
        valid_indices = self.get_valid_subcarrier_indices(len(raw_amplitudes))
        extracted = [raw_amplitudes[i] for i in valid_indices]

        # 2. 消除路由器全局功率抖动 (AGC Normalization)
        normed = self.normalize_agc(extracted)

        # 3. 初始化/维护时域历史缓存
        m = len(normed)
        if len(self.history_buffers) != m:
            self.history_buffers = [[val] for val in normed]
            return normed

        filtered_amps = []
        for i, val in enumerate(normed):
            buf = self.history_buffers[i]
            buf.append(val)
            if len(buf) > self.hampel_window:
                buf.pop(0)
            clean_val = self.remove_hampel_outliers(buf)
            filtered_amps.append(clean_val)

        return filtered_amps

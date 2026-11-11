"""
SafeHalo 第二阶段「物品确认」观察器 (object_confirm.py)

背景: 阶段一（定向复查窗口，约 3 秒）只能数到 1~2 个呼吸峰，无法做间隔规整度
统计。因此当阶段一给出 VERDICT_OBJECT（疑似无生命物体坠落）时，若开关开启，
本观察器继续累积更长时长的地面微动波形（默认 60 秒），再对峰间隔做规整度检验:

  - 峰数不足（少于 4 个峰 / 3 个间隔）: 不下结论，维持阶段一的 OBJECT 判定；
  - 间隔规整（变异系数 <= 阈值）: 判定为活人呼吸，升级为跌倒并报警；
  - 间隔不规整: 维持 OBJECT 判定。

方向性约束: 本观察器只允许把 OBJECT 升级为 FALL，永不反向，因此只会降低
漏报风险；开关默认关闭（object_confirm_seconds <= 0）。

注意: 规整度阈值尚无真实数据标定，默认值只是占位，待 csi_recorder.py 采集
"空房间" 与 "真人静坐" 两组数据后确定。
"""
import math
from dataclasses import dataclass
from typing import List, Optional

# 抽取频率与窗口参数。全部以秒为单位，不依赖阶段一的帧数假设。
DECIM_HZ = 10.0                 # 长时间窗抽取频率
DECIM_INTERVAL = 1.0 / DECIM_HZ
DECIM_EPS = 1e-9                # 浮点容差: 否则 0.09999999999999998 < 0.1 会跳过整帧
SMOOTH_SECONDS = 1.0            # 移动平均半窗
MIN_PEAK_SECONDS = 1.0          # 相邻呼吸峰的最小间距（与阶段一的 1 秒假设一致）
AMP_THRESHOLD = 0.000015        # 零均值化后的幅值门槛（与阶段一相同量级）
MIN_INTERVALS = 3               # 少于 3 个间隔（<4 个峰）不下结论
MIN_SAMPLES = 30                # 观察数据不足 3 秒不下结论

# 间隔变异系数阈值: 占位值，待真实数据标定（见模块注释）。
DEFAULT_CV_THRESHOLD = 0.35


@dataclass
class ObjectConfirmResult:
    escalate: bool              # True = 判定为活人，升级 FALL
    confirmed_object: bool      # True = 维持 OBJECT 判定
    insufficient: bool          # True = 峰数/样本不足，未形成结论
    peaks: int = 0
    intervals: int = 0
    cv: float = 0.0
    bpm: float = 0.0
    details: str = ""


class ObjectConfirmWatcher:
    def __init__(
        self,
        start_ts: float,
        duration_seconds: float = 60.0,
        cv_threshold: float = DEFAULT_CV_THRESHOLD,
        min_intervals: int = MIN_INTERVALS,
    ):
        self.start_ts = start_ts
        self.duration_seconds = duration_seconds
        self.cv_threshold = cv_threshold
        self.min_intervals = min_intervals
        self.samples: List[float] = []
        self.sample_ts: List[float] = []
        # 让第一帧立即被抽取
        self.last_sample_ts = start_ts - DECIM_INTERVAL

    def feed(self, ts: float, amplitude: float) -> Optional[ObjectConfirmResult]:
        """逐帧喂入地面微动幅值；观察期结束当帧返回结果，否则返回 None。"""
        if ts - self.last_sample_ts >= DECIM_INTERVAL - DECIM_EPS:
            self.samples.append(amplitude)
            self.sample_ts.append(ts)
            self.last_sample_ts = ts
        if ts - self.start_ts >= self.duration_seconds - DECIM_EPS:
            return self._analyze()
        return None

    def _analyze(self) -> ObjectConfirmResult:
        n = len(self.samples)
        if n < MIN_SAMPLES:
            return ObjectConfirmResult(
                escalate=False, confirmed_object=True, insufficient=True,
                details=f"观察样本不足 ({n} < {MIN_SAMPLES})，维持物品判定",
            )

        w = int(round(SMOOTH_SECONDS * DECIM_HZ))
        smoothed = []
        for i in range(n):
            start_idx = max(0, i - w)
            end_idx = min(n, i + w + 1)
            total = 0.0
            for j in range(start_idx, end_idx):
                total += self.samples[j]
            smoothed.append(total / (end_idx - start_idx))

        mean_v = 0.0
        for x in smoothed:
            mean_v += x
        mean_v /= n
        zero_mean = [x - mean_v for x in smoothed]

        min_dist = int(round(MIN_PEAK_SECONDS * DECIM_HZ))
        peak_idx: List[int] = []
        last_peak = -min_dist
        for i in range(1, n - 1):
            # 左侧严格大于、右侧允许相等: 峰正好落在两个采样点中间时（纯正弦
            # 在 10Hz 网格上的常见情形）把平局归给左边，避免漏检。
            if zero_mean[i] > zero_mean[i - 1] and zero_mean[i] >= zero_mean[i + 1]:
                if zero_mean[i] > AMP_THRESHOLD and (i - last_peak >= min_dist):
                    peak_idx.append(i)
                    last_peak = i

        if len(peak_idx) - 1 < self.min_intervals:
            return ObjectConfirmResult(
                escalate=False, confirmed_object=True, insufficient=True,
                peaks=len(peak_idx),
                details=f"呼吸峰不足 ({len(peak_idx)} 个，需 {self.min_intervals + 1} 个)，维持物品判定",
            )

        intervals = []
        for i in range(1, len(peak_idx)):
            # 用真实采样时间戳计算峰间隔，避免抽稀网格量化误差污染 CV
            intervals.append(self.sample_ts[peak_idx[i]] - self.sample_ts[peak_idx[i - 1]])

        mean_i = 0.0
        for x in intervals:
            mean_i += x
        mean_i /= len(intervals)
        var_i = 0.0
        for x in intervals:
            var_i += (x - mean_i) ** 2
        var_i /= len(intervals)
        cv = math.sqrt(var_i) / mean_i if mean_i > 1e-9 else float("inf")
        bpm = 60.0 / mean_i if mean_i > 1e-9 else 0.0

        if cv <= self.cv_threshold:
            return ObjectConfirmResult(
                escalate=True, confirmed_object=False, insufficient=False,
                peaks=len(peak_idx), intervals=len(intervals), cv=cv, bpm=bpm,
                details=f"长窗呼吸节律规整 (峰间隔 CV={cv:.2f} <= {self.cv_threshold:.2f}, {bpm:.0f} bpm)，判定为活人并升级跌倒",
            )
        return ObjectConfirmResult(
            escalate=False, confirmed_object=True, insufficient=False,
            peaks=len(peak_idx), intervals=len(intervals), cv=cv, bpm=bpm,
            details=f"峰间隔不规整 (CV={cv:.2f} > {self.cv_threshold:.2f})，维持物品判定",
        )

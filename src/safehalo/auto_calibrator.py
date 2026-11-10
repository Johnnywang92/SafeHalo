"""
SafeHalo 阶段 2：多日环境基线自动学习与自适应校准引擎 (auto_calibrator.py)
解决痛点:
1. 冷启动抗污染 (Boot Contamination Guard):
   - 若设备断电重启或首次开机时，房间内恰好有人走动，传统固定几秒校准会把「有人走动的高能量」错误锁定为空房基线，导致后续漏报。
   - 本模块自动检测校准窗口的方差离散度；若判定开机窗口存在人员活动污染，自动回退至历史持久化基线或安全默认值，待房间首次安静时再无感完成真实标定。
2. 安装后多日自动学习 (Multi-Day Circadian Baseline Learning):
   - 连续跟踪安装后数天内的「长静默空房窗口 (Quiet Windows)」，按 24 小时分时段建立环境本底噪声画像（适应白天电器运转与深夜极静环境差异）。
3. 家具挪动与温漂自适应 (Environmental Drift Adaptation):
   - 当家中挪动椅子/家具或昼夜温度变化引起路由器发射功率微漂时，通过慢速指数滑动平均 (EMA) 自动平滑演进基线，无需人工重按校准键。
"""

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .logger import logger


@dataclass
class BaselineModelState:
    """持久化环境基线模型数据"""
    base_variance: float = 0.0001
    base_diff: float = 0.001
    hourly_variance: List[float] = field(default_factory=lambda: [0.0001] * 24)
    hourly_diff: List[float] = field(default_factory=lambda: [0.001] * 24)
    quiet_windows_learned: int = 0
    learning_days_set: List[str] = field(default_factory=list)
    boot_contamination_rejected: int = 0
    drift_adaptations_count: int = 0
    last_updated: str = ""


class ContinuousAutoCalibrator:
    """多日連続环境基线自学习与温漂校准引擎"""

    # 开机校准若方差超过该安全上限，判定开机时屋内有人走动（污染）
    MAX_CLEAN_BOOT_VARIANCE = 0.0025
    MAX_CLEAN_BOOT_DIFF = 0.015

    def __init__(
        self,
        storage_dir: Optional[str] = None,
        quiet_window_min_seconds: float = 30.0,
        ema_alpha: float = 0.08,
    ):
        self.storage_dir = storage_dir or os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "logs")
        )
        os.makedirs(self.storage_dir, exist_ok=True)
        self.model_file = os.path.join(self.storage_dir, "baseline_model.json")
        self.quiet_window_min_seconds = quiet_window_min_seconds
        self.ema_alpha = ema_alpha
        self._lock = threading.Lock()

        self.state = BaselineModelState()
        self.has_persisted_baseline = self._load_model()

        # 运行期静默窗口观测缓冲
        self._quiet_start_ts: Optional[float] = None
        self._quiet_var_samples: List[float] = []
        self._quiet_diff_samples: List[float] = []

    def _load_model(self) -> bool:
        if not os.path.exists(self.model_file):
            return False
        try:
            with open(self.model_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.state = BaselineModelState(
                    base_variance=float(data.get("base_variance", 0.0001)),
                    base_diff=float(data.get("base_diff", 0.001)),
                    hourly_variance=list(data.get("hourly_variance", [0.0001] * 24))[:24],
                    hourly_diff=list(data.get("hourly_diff", [0.001] * 24))[:24],
                    quiet_windows_learned=int(data.get("quiet_windows_learned", 0)),
                    learning_days_set=list(data.get("learning_days_set", [])),
                    boot_contamination_rejected=int(data.get("boot_contamination_rejected", 0)),
                    drift_adaptations_count=int(data.get("drift_adaptations_count", 0)),
                    last_updated=str(data.get("last_updated", "")),
                )
                return True
        except Exception as e:
            logger.error(f"[AutoCalibrator] 读取持久化基线模型失败: {e}")
            return False

    def _save_model(self):
        try:
            self.state.last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.model_file, "w", encoding="utf-8") as f:
                json.dump(asdict(self.state), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[AutoCalibrator] 保存基线模型失败: {e}")

    def validate_initial_calibration(
        self,
        candidate_variance: float,
        candidate_diff: float,
        timestamp: Optional[float] = None,
    ) -> Tuple[float, float, bool]:
        """
        检验开机初始校准结果是否受到人员走动污染:
        :return: (effective_base_variance, effective_base_diff, is_clean)
        """
        ts = timestamp or time.time()
        dt = datetime.fromtimestamp(ts)
        hour = dt.hour

        with self._lock:
            is_contaminated = (
                candidate_variance > self.MAX_CLEAN_BOOT_VARIANCE
                or candidate_diff > self.MAX_CLEAN_BOOT_DIFF
            )

            if is_contaminated:
                self.state.boot_contamination_rejected += 1
                if self.has_persisted_baseline:
                    fallback_var = self.state.hourly_variance[hour]
                    fallback_diff = self.state.hourly_diff[hour]
                    reason = "已回退至历史持久化多日基线"
                else:
                    fallback_var = 0.00012
                    fallback_diff = 0.0012
                    reason = "已采用安全默认底噪基线，待房间安静后自动重标定"

                logger.warning(
                    f"[AutoCalibrator] 检测到开机校准期存在人员走动干扰 "
                    f"(var={candidate_variance:.6f}), {reason}"
                )
                self._save_model()
                return fallback_var, fallback_diff, False

            # 干净的初始校准：若已有跨天历史基线则平滑融合，否则直接采纳
            if self.has_persisted_baseline and self.state.quiet_windows_learned > 0:
                eff_var = 0.6 * self.state.base_variance + 0.4 * candidate_variance
                eff_diff = 0.6 * self.state.base_diff + 0.4 * candidate_diff
            else:
                eff_var = max(1e-6, candidate_variance)
                eff_diff = max(1e-5, candidate_diff)
                self.state.hourly_variance = [eff_var] * 24
                self.state.hourly_diff = [eff_diff] * 24

            self.state.base_variance = round(eff_var, 7)
            self.state.base_diff = round(eff_diff, 6)
            self.state.hourly_variance[hour] = round(eff_var, 7)
            self.state.hourly_diff[hour] = round(eff_diff, 6)

            day_str = dt.strftime("%Y-%m-%d")
            if day_str not in self.state.learning_days_set:
                self.state.learning_days_set.append(day_str)

            self.has_persisted_baseline = True
            self._save_model()
            return self.state.base_variance, self.state.base_diff, True

    def observe_frame(
        self,
        is_empty_or_quiet: bool,
        mean_variance: float,
        diff_energy: float,
        timestamp: Optional[float] = None,
    ) -> Optional[Tuple[float, float]]:
        """
        在日常运行中持续观测：当房间处于持续静默 (EMPTY) 超过设定窗口时，
        自动执行多日环境基线微调学习，并返回更新后的 (base_variance, base_diff)。
        """
        now = timestamp or time.time()

        # 若当前不是静默状态，或瞬时能量高于安全空房阈值，则重置静默计时
        if (
            not is_empty_or_quiet
            or mean_variance > self.MAX_CLEAN_BOOT_VARIANCE
            or diff_energy > self.MAX_CLEAN_BOOT_DIFF
        ):
            self._quiet_start_ts = None
            self._quiet_var_samples.clear()
            self._quiet_diff_samples.clear()
            return None

        if self._quiet_start_ts is None:
            self._quiet_start_ts = now

        self._quiet_var_samples.append(mean_variance)
        self._quiet_diff_samples.append(diff_energy)

        elapsed = now - self._quiet_start_ts
        if elapsed < self.quiet_window_min_seconds or len(self._quiet_var_samples) < 10:
            return None

        # 达到一个完整静默学习窗口：取中位数并通过 EMA 融入全局与当前小时基线
        with self._lock:
            sorted_v = sorted(self._quiet_var_samples)
            sorted_d = sorted(self._quiet_diff_samples)
            med_v = max(1e-6, sorted_v[len(sorted_v) // 2])
            med_d = max(1e-5, sorted_d[len(sorted_d) // 2])

            dt = datetime.fromtimestamp(now)
            hour = dt.hour
            day_str = dt.strftime("%Y-%m-%d")

            alpha = self.ema_alpha
            new_base_v = (1.0 - alpha) * self.state.base_variance + alpha * med_v
            new_base_d = (1.0 - alpha) * self.state.base_diff + alpha * med_d

            hour_v = (1.0 - alpha * 1.5) * self.state.hourly_variance[hour] + (alpha * 1.5) * med_v
            hour_d = (1.0 - alpha * 1.5) * self.state.hourly_diff[hour] + (alpha * 1.5) * med_d

            self.state.base_variance = round(new_base_v, 7)
            self.state.base_diff = round(new_base_d, 6)
            self.state.hourly_variance[hour] = round(hour_v, 7)
            self.state.hourly_diff[hour] = round(hour_d, 6)
            self.state.quiet_windows_learned += 1
            self.state.drift_adaptations_count += 1

            if day_str not in self.state.learning_days_set:
                self.state.learning_days_set.append(day_str)

            self._quiet_start_ts = now
            self._quiet_var_samples.clear()
            self._quiet_diff_samples.clear()
            self._save_model()

            logger.debug(
                f"[AutoCalibrator] 完成第 {self.state.quiet_windows_learned} 次静默窗口自适应基线演进: "
                f"base_var={self.state.base_variance:.6f}, base_diff={self.state.base_diff:.5f}"
            )
            return (self.state.base_variance, self.state.base_diff)

    def get_status_summary(self) -> Dict[str, Any]:
        """获取供 Web 控制台与试点看板展示的多日自学习状态"""
        with self._lock:
            days_count = len(self.state.learning_days_set)
            # 安装满 3 天且累计学习 >= 20 个静默窗口视为 100% 成熟
            maturity = min(
                100.0,
                round(days_count * 25.0 + min(25.0, self.state.quiet_windows_learned * 1.25), 1),
            )
            return {
                "base_variance": self.state.base_variance,
                "base_diff": self.state.base_diff,
                "learning_days": days_count,
                "quiet_windows_learned": self.state.quiet_windows_learned,
                "maturity_percent": maturity,
                "boot_contamination_rejected": self.state.boot_contamination_rejected,
                "drift_adaptations_count": self.state.drift_adaptations_count,
                "last_updated": self.state.last_updated or "刚刚",
            }

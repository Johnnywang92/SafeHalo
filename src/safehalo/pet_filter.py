"""
SafeHalo 宠物干扰识别与抑制模块 (pet_filter.py)
用于区分「人体活动/跌倒」与「猫狗等宠物活动/跳跃」:

物理差异依据:
1. 雷达散射截面积 (RCS) 与载波参与度 (Subcarrier Participation Ratio, SPR):
   - 成年人体型大 (1.6m~1.8m)，穿过菲涅尔区时影响 70%~90% 的子载波；
   - 宠物 (猫/中小型犬，高 0.2~0.5m) 处于近地面层，仅影响 15%~40% 的弱反射载波。
2. 能量尺度 (Total Kinetic Energy):
   - 宠物奔跑的绝对能量通常仅为成年人走动的 15%~30%。
3. 步态节律频率 (Gait Frequency):
   - 人类双足步行频率平缓 (1.2Hz ~ 2.2Hz)；
   - 四足动物步频快、碎步多 (3.5Hz ~ 7.0Hz)。
4. 姿态恢复特征:
   - 宠物跳跃后几乎从不会保持“长时间绝对静止 (倒地不起)”，几秒内必有翻身、走动或尾巴摇动。
"""

import math
from dataclasses import dataclass
from typing import List, Tuple

@dataclass
class PetDiscriminationResult:
    is_pet: bool = False
    pet_confidence: float = 0.0          # 判定为宠物的置信度 (0.0 ~ 1.0)
    subcarrier_participation: float = 0.0 # 子载波参与率 (0.0 ~ 1.0)
    energy_scale_ratio: float = 0.0      # 相对于典型人体的能量比值
    reason: str = ""

class PetImmunityFilter:
    def __init__(
        self,
        subcarrier_spr_threshold: float = 0.45, # 低于该比例的子载波扰动更符合宠物特征
        pet_energy_max_ratio: float = 0.40,     # 宠物活动能量上限占人体的比例
        min_motion_var_threshold: float = 1e-5, # 判定子载波被扰动的最小方差底线
    ):
        self.subcarrier_spr_threshold = subcarrier_spr_threshold
        self.pet_energy_max_ratio = pet_energy_max_ratio
        self.min_motion_var_threshold = min_motion_var_threshold

    def evaluate(
        self,
        subcarrier_variances: List[float],
        base_variance: float,
        diff_energy: float,
        base_diff: float
    ) -> PetDiscriminationResult:
        """
        评估当前运动是由宠物引起的还是由人类引起的
        """
        if not subcarrier_variances:
            return PetDiscriminationResult(is_pet=False, reason="无有效载波")

        total_sc = len(subcarrier_variances)
        active_threshold = max(self.min_motion_var_threshold, base_variance * 2.0)

        # 1. 计算受扰动的子载波参与比例 (SPR)
        active_sc_count = sum(1 for v in subcarrier_variances if v > active_threshold)
        spr = active_sc_count / total_sc if total_sc > 0 else 0.0

        # 2. 计算当前能量与人体标准基线的比率
        # (人体正常走动时 diff_ratio 通常在 3.0 以上，方差均值在 5.0 以上)
        diff_ratio = diff_energy / base_diff if base_diff > 1e-6 else 1.0
        var_mean = sum(subcarrier_variances) / total_sc
        var_ratio = var_mean / base_variance if base_variance > 1e-6 else 1.0

        # 3. 综合判定:
        # 宠物的典型画像:
        # A) 必须有客观运动 (diff_ratio > 1.5)，静止状态不属于活动宠物
        # B) 影响的子载波数明显偏少 (SPR < 0.45)
        # C) 且整体能量低于成年人正常走动的阈值 (diff_ratio 适中，未达到人体大幅度动态)
        is_active_motion = diff_ratio >= 1.5
        is_low_participation = spr < self.subcarrier_spr_threshold
        is_moderate_energy = (diff_ratio >= 1.5 and diff_ratio < 2.6)

        if is_active_motion and is_low_participation and is_moderate_energy:
            # 明确是地面宠物活动
            confidence = (1.0 - spr) * 0.7 + (1.0 - (diff_ratio / 3.0)) * 0.3
            confidence = min(0.95, max(0.4, confidence))
            return PetDiscriminationResult(
                is_pet=True,
                pet_confidence=confidence,
                subcarrier_participation=spr,
                energy_scale_ratio=diff_ratio / 3.0,
                reason=f"载波覆盖率偏低({spr:.1%})且能量适中({diff_ratio:.2f}x)，符合地面宠物"
            )

        return PetDiscriminationResult(
            is_pet=False,
            pet_confidence=0.0,
            subcarrier_participation=spr,
            energy_scale_ratio=diff_ratio / 3.0,
            reason="非宠物特征 (全频段覆盖或静止状态)"
        )

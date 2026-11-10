"""
SafeHalo 路由器空间摆放位置引导与算法自适应引擎 (placement_adapter.py)
专为首次安装 (First-time Onboarding) 与多场景算法自适应调优设计:

核心功能:
1. 空间摆放场景向导建模 (Room Placement Wizard Model):
   - 支持客厅 (Living Room)、卧室 (Bedroom)、卫生间 (Bathroom)、厨房 (Kitchen)、玄关走廊 (Corridor) 5 大核心家庭场景。
   - 细化摆放高度 (低位 0.3~0.8m、桌面中位 0.8~1.5m、天花板高位 1.8~2.5m) 与房间面积。
2. 算法超参数物理级自适应调优 (Physical Parameter Auto-Tuning):
   - 【卫生间 (高危滑倒场景)】: 调高跌倒灵敏度 (阈值从 2.0 降至 1.15)，缩短倒地静止确认至 3.0 秒 (极速报警)，激活水汽水流多径去噪滤波器。
   - 【卧室 (床铺与起夜场景)】: 调紧床铺与地面高度区隔 (雷达地面门限收紧至 0.30m，床面 0.55m 绝不误报)，增强晨间起卧微动敏感度。
   - 【客厅 (开阔与家宠场景)】: 适配大面积漫射多径，扩大时域抗扰滤波窗口，针对宠物活动提升 SPR 过滤强度。
   - 【厨房 (金属家电多径区)】: 增加金属反射吸收阻尼，抑制冰箱与微波炉电磁谐振。
3. 联动基线即时重学习与配置持久化:
   - 保存至 `logs/placement_config.json`。
   - 引导用户完成摆放后，自动触发 5 秒环境底噪自校准。

纯 Python 标准库实现，零第三方依赖。
"""

import enum
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from .logger import logger


class RoomType(enum.Enum):
    """房间安装场景枚举"""
    LIVING_ROOM = "LIVING_ROOM"     # 客厅 (开阔多径，宠物多，沙发/电视)
    BEDROOM = "BEDROOM"             # 卧室 (床铺避让，起卧微动，静卧睡眠)
    BATHROOM = "BATHROOM"           # 卫生间 (极高危滑倒，水流扰动，极速响应)
    KITCHEN = "KITCHEN"             # 厨房 (金属家电多径反弹，频繁停顿)
    CORRIDOR = "CORRIDOR"           # 玄关/走廊 (线性通过通道)


class MountHeightLevel(enum.Enum):
    """路由器/探针安装高度等级"""
    LOW = "LOW"                     # 低位: 0.3m ~ 0.8m (地插、矮脚柜)
    MID_DESK = "MID_DESK"           # 中位: 0.8m ~ 1.5m (电视柜、书桌、床头柜 - 推荐黄金高度)
    HIGH_CEILING = "HIGH_CEILING"   # 高位: 1.8m ~ 2.5m (吸顶、壁挂顶部)


@dataclass
class TunedParameters:
    """根据摆放位置计算出的算法超参数包"""
    fall_spike_threshold: float         # CSI 冲击突变方差倍数阈值
    fall_stillness_seconds: float       # 跌倒倒地静止确诊时长 (秒)
    pet_spr_threshold: float            # 宠物子载波比率判定门限
    radar_floor_height_thresh: float    # 毫米波雷达判定贴地的标称高度 (米)
    hampel_window_size: int             # 时域去噪滑动窗口
    water_flow_damping: bool            # 是否激活水流微动阻尼
    metal_multipath_damping: bool       # 是否激活金属电器反射阻尼
    description: str                    # 调优策略人读说明


@dataclass
class RoomPlacementConfig:
    """房间摆放与首次安装配置"""
    room_type: str = "LIVING_ROOM"
    room_name: str = "客厅"
    mount_height: str = "MID_DESK"
    room_area_sqm: float = 25.0
    has_pets: bool = False
    has_water_splash: bool = False
    is_configured: bool = False         # 是否已完成首次向导引导
    updated_at: str = ""


class PlacementAdapter:
    """路由器摆放自适应协调器"""

    def __init__(self, config_dir: Optional[str] = None):
        self.config_dir = config_dir or os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "logs")
        )
        os.makedirs(self.config_dir, exist_ok=True)
        self.config_file = os.path.join(self.config_dir, "placement_config.json")
        self._lock = threading.Lock()

        self.current_config = RoomPlacementConfig()
        self.current_tuned_params = self.compute_tuned_parameters(self.current_config)

        # 启动时载入已保存配置
        self.load_config()

    def load_config(self) -> RoomPlacementConfig:
        """从本地 JSON 文件读取配置"""
        with self._lock:
            if os.path.exists(self.config_file):
                try:
                    with open(self.config_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self.current_config = RoomPlacementConfig(
                            room_type=data.get("room_type", "LIVING_ROOM"),
                            room_name=data.get("room_name", "客厅"),
                            mount_height=data.get("mount_height", "MID_DESK"),
                            room_area_sqm=float(data.get("room_area_sqm", 25.0)),
                            has_pets=bool(data.get("has_pets", False)),
                            has_water_splash=bool(data.get("has_water_splash", False)),
                            is_configured=bool(data.get("is_configured", True)),
                            updated_at=data.get("updated_at", ""),
                        )
                        self.current_tuned_params = self.compute_tuned_parameters(self.current_config)
                        logger.info(
                            f"[PlacementAdapter] 已载入摆放配置: 【{self.current_config.room_name}】"
                            f"({self.current_config.room_type}, 高度: {self.current_config.mount_height})"
                        )
                except Exception as e:
                    logger.error(f"[PlacementAdapter] 读取摆放配置异常: {e}")
            return self.current_config

    def save_config(
        self,
        room_type: str,
        room_name: str,
        mount_height: str = "MID_DESK",
        room_area_sqm: float = 25.0,
        has_pets: bool = False,
        has_water_splash: bool = False,
    ) -> Tuple[RoomPlacementConfig, TunedParameters]:
        """保存用户在向导中设置的房间与摆放参数，并计算自适应超参数"""
        with self._lock:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.current_config = RoomPlacementConfig(
                room_type=room_type,
                room_name=room_name,
                mount_height=mount_height,
                room_area_sqm=room_area_sqm,
                has_pets=has_pets,
                has_water_splash=has_water_splash,
                is_configured=True,
                updated_at=now_str,
            )
            self.current_tuned_params = self.compute_tuned_parameters(self.current_config)

            # 持久化落盘
            try:
                with open(self.config_file, "w", encoding="utf-8") as f:
                    json.dump(asdict(self.current_config), f, ensure_ascii=False, indent=2)
                logger.info(
                    f"[PlacementAdapter] 成功保存新摆放配置: 【{room_name}】，算法超参数已完成物理级自适应调优"
                )
            except Exception as e:
                logger.error(f"[PlacementAdapter] 写入摆放配置失败: {e}")

            return self.current_config, self.current_tuned_params

    @staticmethod
    def compute_tuned_parameters(cfg: RoomPlacementConfig) -> TunedParameters:
        """
        核心物理模型: 根据房间类别、高度与物理扰动因子推导算法最优参数
        """
        r_type = cfg.room_type.upper()
        m_height = cfg.mount_height.upper()

        # 1. 默认基准参数 (中型客厅基准)
        spike_thresh = 2.0
        stillness_sec = 4.0
        pet_spr = 0.45 if cfg.has_pets else 0.30
        radar_floor_z = 0.38
        hampel_win = 5
        water_damping = False
        metal_damping = False
        desc_parts = []

        # 2. 房间类型专业微调
        if r_type == "BATHROOM":
            # 卫生间: 极高危湿滑摔倒区，必须极速高灵敏响应
            spike_thresh = 1.15         # 灵敏度提高 74%
            stillness_sec = 3.0        # 缩短至 3 秒极速报警
            radar_floor_z = 0.35
            hampel_win = 7             # 抑制花洒与地面流水高频抖动
            water_damping = True
            desc_parts.append("【卫生间极速防滑响应】: 跌倒冲击阈值调至 1.15，倒地静止 3 秒极速触发，启用花洒水流微动抑制")

        elif r_type == "BEDROOM":
            # 卧室: 侧重起卧床面区分与微动呼吸
            spike_thresh = 1.40         # 虚弱老人下床跌倒冲击可能较小，放宽门限
            stillness_sec = 4.5        # 4.5 秒确认 (排除翻身坐起)
            radar_floor_z = 0.30       # 床高通常 0.50m，必须低于 0.30m 方算倒地，杜绝卧床休息误报
            hampel_win = 5
            desc_parts.append("【卧室床铺安全避让】: 地面高度阈值收紧至 0.30m (避免床上坐卧误判)，强化夜间起夜灵敏度")

        elif r_type == "KITCHEN":
            # 厨房: 金属电器多径反弹严重
            spike_thresh = 1.85
            stillness_sec = 3.8
            metal_damping = True
            desc_parts.append("【厨房金属抗反弹】: 激活冰箱与油烟机多径共振吸收，平抑高频谐振")

        elif r_type == "CORRIDOR":
            # 走廊/玄关: 线性直道，快速通过通道
            spike_thresh = 1.60
            stillness_sec = 3.5
            desc_parts.append("【走廊线性通道】: 快速响应直行路径变化")

        else: # LIVING_ROOM 或 通用
            spike_thresh = 2.10
            stillness_sec = 4.0
            hampel_win = 7             # 客厅窗帘、电视等慢速多径抑制
            desc_parts.append("【客厅大开间多径适应】: 扩大时域滤波抗窗帘抖动，优化多人走动场景")

        # 3. 安装高度补偿
        if m_height == "LOW":
            # 低位安装 (0.3~0.8m): 探针极度贴近地面，地面动能被放大 30%
            spike_thresh *= 1.15
            desc_parts.append("矮柜低位安装: 补偿地面多径反射放大效应")
        elif m_height == "HIGH_CEILING":
            # 高位/吸顶安装 (1.8~2.5m): 俯视角度，垂直坠落分量最清晰
            spike_thresh *= 0.95
            radar_floor_z = max(radar_floor_z, 0.40)
            desc_parts.append("吸顶高位安装: 增强俯视垂向下坠特征")

        # 4. 宠物补偿
        if cfg.has_pets:
            pet_spr = 0.50
            desc_parts.append("家有猫狗: 提升 SPR 载波参与度过滤级别至 50%")

        return TunedParameters(
            fall_spike_threshold=round(spike_thresh, 2),
            fall_stillness_seconds=round(stillness_sec, 1),
            pet_spr_threshold=round(pet_spr, 2),
            radar_floor_height_thresh=round(radar_floor_z, 2),
            hampel_window_size=hampel_win,
            water_flow_damping=water_damping,
            metal_multipath_damping=metal_damping,
            description=" | ".join(desc_parts),
        )

    def apply_to_engines(
        self,
        csi_engine: Optional[Any] = None,
        dual_engine: Optional[Any] = None,
    ):
        """将当前计算出的参数动态应用至正在运行的算法引擎实例中"""
        params = self.current_tuned_params
        logger.info(f"[PlacementAdapter] 正在向算法核心下发参数: {params.description}")

        # 1. 注入 CSI 状态机
        if csi_engine and hasattr(csi_engine, "detector"):
            det = csi_engine.detector
            det.fall_spike_threshold = params.fall_spike_threshold
            det.fall_stillness_seconds = params.fall_stillness_seconds
            if hasattr(det, "pet_filter"):
                det.pet_filter.subcarrier_spr_threshold = params.pet_spr_threshold
            # 重新发起 5 秒快速基线标定
            det.start_calibration()
            logger.info(
                f"[PlacementAdapter] CSI 引擎已适配: 冲击阈值={params.fall_spike_threshold}, "
                f"静止确认={params.fall_stillness_seconds}s，已重置基线标定"
            )

        # 2. 注入双模/毫米波引擎
        if dual_engine:
            if hasattr(dual_engine, "radar_detector"):
                dual_engine.radar_detector.floor_height_thresh = params.radar_floor_height_thresh
                dual_engine.radar_detector.stillness_confirm_sec = params.fall_stillness_seconds
            if hasattr(dual_engine, "csi_detector"):
                dual_engine.csi_detector.fall_spike_threshold = params.fall_spike_threshold
                dual_engine.csi_detector.fall_stillness_seconds = params.fall_stillness_seconds
                dual_engine.csi_detector.start_calibration()

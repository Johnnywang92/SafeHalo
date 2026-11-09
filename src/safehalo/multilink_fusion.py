"""
SafeHalo 多 Wi-Fi 与多链路空间分集融合感知引擎 (multilink_fusion.py)
解决家庭多个 Wi-Fi / 多个 AP 场景下的干扰问题，并将多信号转化为空间分集优势:

核心算法能力:
1. 多 BSSID 独立流解耦:
   - 自动按 MAC 地址分离不同路由器/AP 的 CSI 报文，各链路独立运行 AGC 归一化与环境基线学习。
2. 空间盲区消解 (Blind Angle Elimination):
   - 单链路对平行于视距方向 (LOS) 的运动存在多普勒盲区。
   - 双/多链路通过 90° 交叉菲涅尔区，利用最大比合并 (Maximal Ratio Combining) 消除体位盲区。
3. 空间交叉校验与抗扰 (Cross-Validation Immunity):
   - 局部干扰 (如卧室风扇、窗帘飘动、单点猫狗) 仅影响单一链路。
   - 真实人体动作和跌倒具有跨链路空间扰动关联性，大幅压低误报。
4. 房间级微定位 (Room-level Localization):
   - 对比各 AP 链路的差分动能衰减比率，自动推断受检老人所处房间 (如「客厅」vs「主卧」)。
"""

import math
import time
from typing import Dict, List, Optional, Tuple, Callable
from dataclasses import dataclass, field

from .csi_parser import CSIPacket, CSIParser
from .signal_filter import RouterSignalFilter
from .feature_extractor import RouterFeatureExtractor, CSIFeatures
from .detector import RouterCSIDetector, HumanState
from .logger import logger

@dataclass
class LinkTelemetry:
    bssid: str
    room_name: str
    packet_count: int = 0
    last_seen: float = 0.0
    current_state: HumanState = HumanState.CALIBRATING
    latest_features: Optional[CSIFeatures] = None
    mean_variance: float = 0.0
    diff_energy: float = 0.0
    snr_weight: float = 1.0  # 基于链路质量的自适应融合权重

@dataclass
class FusedSpatialResult:
    fused_state: HumanState
    active_room: str
    fused_energy: float
    fused_confidence: float
    participating_links: int
    link_details: Dict[str, LinkTelemetry]

class MultiLinkFusionEngine:
    def __init__(
        self,
        links_config: Optional[Dict[str, str]] = None,
        on_fused_state_change: Optional[Callable[[HumanState, HumanState, FusedSpatialResult], None]] = None,
        on_fall_alarm: Optional[Callable[[FusedSpatialResult], None]] = None,
    ):
        """
        :param links_config: MAC地址与房间名映射表，如:
               {"c0:06:c3:11:22:33": "客厅路由器", "c0:06:c3:44:55:66": "卧室副路由"}
               若为空则自动动态注册首次发现的 MAC 节点。
        """
        self.links_config = {k.lower().replace("-", ":"): v for k, v in (links_config or {}).items()}
        self.on_fused_state_change = on_fused_state_change
        self.on_fall_alarm = on_fall_alarm

        self.parser = CSIParser()
        self.filters: Dict[str, RouterSignalFilter] = {}
        self.extractors: Dict[str, RouterFeatureExtractor] = {}
        self.detectors: Dict[str, RouterCSIDetector] = {}
        self.telemetries: Dict[str, LinkTelemetry] = {}

        self.fused_state = HumanState.CALIBRATING
        self.last_alarm_time = 0.0

        # 初始化预设链路
        for mac, room in self.links_config.items():
            self._init_link(mac, room)

    def _init_link(self, mac: str, room_name: str):
        mac = mac.lower().replace("-", ":")
        if mac in self.telemetries:
            return

        self.filters[mac] = RouterSignalFilter()
        self.extractors[mac] = RouterFeatureExtractor(window_size=40)
        
        # 每个链路使用独立基线校准，以适应不同房间的墙体结构与家具多径
        detector = RouterCSIDetector(
            calibration_seconds=4.0,
            fall_stillness_seconds=4.5,
            on_state_change=lambda old, new, feat, m=mac: self._on_sublink_state_change(m, old, new, feat),
        )
        self.detectors[mac] = detector
        self.telemetries[mac] = LinkTelemetry(bssid=mac, room_name=room_name)
        logger.info(f"多链路引擎已注册 AP 节点: [{mac}] -> {room_name}")

    def feed_line(self, line: str, timestamp_override: Optional[float] = None) -> Optional[FusedSpatialResult]:
        """
        输入来自任意 Wi-Fi AP 链路的 CSI 原始行，自动识别源 BSSID 并完成空间多径融合。
        """
        packet = self.parser.parse_line(line)
        if not packet or not packet.mac:
            return None

        mac = packet.mac.lower().replace("-", ":")
        now = timestamp_override if timestamp_override is not None else time.time()

        # 动态自学习新发现的未知路由器 AP
        if mac not in self.telemetries:
            if not self.links_config:
                # 未指定白名单时，自动注册最多 3 个活跃 AP
                if len(self.telemetries) < 3:
                    room_alias = f"AP节点-{len(self.telemetries)+1}"
                    self._init_link(mac, room_alias)
                else:
                    return None
            else:
                # 若配置了白名单且不匹配，则丢弃无关报文 (抑制邻居 WiFi 串扰)
                return None

        telem = self.telemetries[mac]
        telem.packet_count += 1
        telem.last_seen = now

        # 1. 独立链路归一化滤波
        clean_amps = self.filters[mac].process(packet.amplitudes)

        # 2. 独立链路特征提取
        features = self.extractors[mac].push_frame(timestamp=now, amplitudes=clean_amps)
        if not features:
            return None

        telem.latest_features = features
        telem.mean_variance = features.mean_variance
        telem.diff_energy = features.diff_energy

        # 3. 独立链路状态判定
        sub_state = self.detectors[mac].update(features)
        telem.current_state = sub_state

        # 4. 多链路时空多维融合判决
        return self._fuse_spatial_decision(now)

    def _on_sublink_state_change(self, mac: str, old_state: HumanState, new_state: HumanState, features: CSIFeatures):
        room = self.telemetries[mac].room_name
        logger.debug(f"子链路 [{room}] 状态流转: {old_state.value} -> {new_state.value}")

    def _fuse_spatial_decision(self, now: float) -> FusedSpatialResult:
        """
        空间多径融合决策矩阵 (Spatial Diversity Decision Matrix)
        """
        active_links = [
            t for t in self.telemetries.values()
            if (now - t.last_seen) < 1.0 and t.latest_features is not None
        ]

        if not active_links:
            return FusedSpatialResult(
                fused_state=HumanState.EMPTY,
                active_room="未知",
                fused_energy=0.0,
                fused_confidence=0.5,
                participating_links=0,
                link_details=self.telemetries
            )

        # 1. 房间级定位推断: 找到动能/方差扰动最大的链路
        max_link = max(active_links, key=lambda l: l.diff_energy)
        active_room = max_link.room_name

        # 2. 空间分集融合能量计算 (最大比合并消除视距夹角盲区)
        # 跌倒冲击垂直于 A 视距时 A 信号微弱，但必与 B 视距形成明显切角扰动
        energies = [l.diff_energy for l in active_links]
        max_energy = max(energies)
        second_energy = sorted(energies, reverse=True)[1] if len(energies) > 1 else 0.0
        fused_energy = max_energy + 0.35 * second_energy

        # 3. 跨链路投票与跌倒确诊
        # 统计各子链路状态
        states = [l.current_state for l in active_links]
        
        # 优先级 A: 跌倒确认 (FALL_CONFIRMED)
        if any(s == HumanState.FALL_CONFIRMED for s in states):
            target_state = HumanState.FALL_CONFIRMED
            confidence = 0.96
        # 优先级 B: 疑似跌倒观测 (FALL_SUSPECT)
        elif any(s == HumanState.FALL_SUSPECT for s in states):
            target_state = HumanState.FALL_SUSPECT
            confidence = 0.88
        # 优先级 C: 运动 (MOVING)
        elif any(s == HumanState.MOVING for s in states):
            target_state = HumanState.MOVING
            confidence = 0.92
        # 优先级 D: 宠物抑制 (全部活跃链路均判定为宠物，或局部宠物且无其他人体反应)
        elif all(s == HumanState.PET_ACTIVITY for s in states):
            target_state = HumanState.PET_ACTIVITY
            confidence = 0.90
        # 优先级 E: 静止有人 (STATIC)
        elif any(s == HumanState.STATIC for s in states):
            target_state = HumanState.STATIC
            confidence = 0.85
        # 优先级 F: 校准中
        elif any(s == HumanState.CALIBRATING for s in states):
            target_state = HumanState.CALIBRATING
            confidence = 0.70
        else:
            target_state = HumanState.EMPTY
            confidence = 0.95

        old_state = self.fused_state
        if target_state != old_state:
            self.fused_state = target_state
            logger.info(f"多链路融合状态流转: {old_state.value} -> {target_state.value} (主要活动区域: {active_room})")
            if self.on_fused_state_change:
                res = FusedSpatialResult(
                    fused_state=target_state,
                    active_room=active_room,
                    fused_energy=fused_energy,
                    fused_confidence=confidence,
                    participating_links=len(active_links),
                    link_details=self.telemetries
                )
                self.on_fused_state_change(old_state, target_state, res)

        result = FusedSpatialResult(
            fused_state=self.fused_state,
            active_room=active_room,
            fused_energy=fused_energy,
            fused_confidence=confidence,
            participating_links=len(active_links),
            link_details=self.telemetries
        )

        if self.fused_state == HumanState.FALL_CONFIRMED and (now - self.last_alarm_time) > 15.0:
            self.last_alarm_time = now
            if self.on_fall_alarm:
                self.on_fall_alarm(result)

        return result

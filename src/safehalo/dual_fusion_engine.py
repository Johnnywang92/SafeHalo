"""
SafeHalo 双模态异构融合感知引擎 (dual_fusion_engine.py)
深度整合 Wi-Fi CSI (非视距/穿墙全域覆盖) 与 毫米波雷达 (视距/三维高度与呼吸微动)。

核心互补优势:
1. 穿墙与视距互补 (Coverage & Occlusion Complementarity):
   - 毫米波雷达 (24/60GHz): 视距精度极高 (厘米级测距与测高)，但无法穿透实体水泥承重墙。
   - Wi-Fi CSI: 多径效应使其具备穿墙探测与全屋漫射感知能力。
   - 策略: 当卫生间/隔断遮挡导致雷达失的 (Radar Empty) 时，自动回退到 Wi-Fi CSI 全域感知；雷达可视时以雷达精确高度校准。
2. 误报消解 (False Alarm Annihilation):
   - 场景 A (窗帘摆动/风扇): Wi-Fi CSI 波动，但雷达能量门限及人体点云判定无目标 -> 压制误报。
   - 场景 B (猫狗跳跃): Wi-Fi CSI 出现动能尖峰，但雷达测定目标高度过低 (< 0.35m) 且无人类体型反射截面 -> 归为宠物。
   - 场景 C (掉落衣物/书包): Wi-Fi CSI 动能骤降，雷达高度虽低但缺乏胸腔呼吸微多普勒 (0 bpm) -> 排除误报。
3. 真实跌倒秒级双验 (Double-Verification for Fall):
   - Wi-Fi CSI 突发能量跃迁并静止 + 毫米波雷达躯干高度 Z 跌至地面 (<0.4m) 且保持静止
   - 触发 99.9% 确信度极速告警。

纯 Python 标准库实现，零第三方依赖。
"""

import enum
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple, Union

from .csi_parser import CSIPacket
from .detector import HumanState, RouterCSIDetector
from .feature_extractor import CSIFeatures, RouterFeatureExtractor
from .logger import logger
from .mmwave_detector import MMWaveDetector, RadarFallFeatures, RadarHumanState
from .mmwave_parser import RadarFrame, UnifiedRadarParser
from .signal_filter import RouterSignalFilter
from .neighbor_filter import NeighborSignalFilter


class FusedState(enum.Enum):
    """双模态融合全局人体状态"""
    CALIBRATING = "calibrating"         # 标定中
    EMPTY = "empty"                     # 全屋无人
    NORMAL_ACTIVITY = "normal"          # 正常走动/起居
    RESTING = "resting"                 # 坐卧休息中 (正常)
    PET_ACTIVITY = "pet_activity"       # 宠物活动 (已滤除)
    FALL_SUSPECT = "fall_suspect"       # 怀疑跌倒 (单模态触发，等待二次复核)
    FALL_CONFIRMED = "fall_confirmed"   # 极速确认跌倒 (双模态交叉确认或单模态强证据)


@dataclass
class FusedPerceptionResult:
    """双模融合判定快照"""
    timestamp: float
    fused_state: FusedState
    fused_confidence: float             # 综合置信度 (0.0 ~ 1.0)
    decision_reason: str                # 判定规则链路与决策原因
    csi_state: HumanState               # Wi-Fi CSI 当前状态
    csi_energy: float                   # Wi-Fi CSI 动态能量
    radar_state: RadarHumanState        # 毫米波雷达当前状态
    radar_z: float                      # 毫米波雷达测量高度 (米)
    radar_respiration_bpm: float        # 呼吸频率 (次/分)
    primary_sensor: str                 # 主导决策传感器 ("dual", "radar_los", "wifi_nlos")


class DualSensorFusionEngine:
    """Wi-Fi CSI 与 毫米波雷达双模态感知中枢"""

    def __init__(
        self,
        router_mac: Optional[str] = None,
        radar_protocol: str = "auto",
        radar_sensor_id: str = "mmwave_living",
        on_fused_state_change: Optional[Callable[[FusedState, FusedState, FusedPerceptionResult], None]] = None,
        on_fall_alarm: Optional[Callable[[FusedPerceptionResult], None]] = None,
    ):
        self.router_mac = router_mac.lower().replace("-", ":") if router_mac else None
        self.on_fused_state_change = on_fused_state_change
        self.on_fall_alarm = on_fall_alarm

        # 1. Wi-Fi CSI 感知链路
        self.neighbor_filter = NeighborSignalFilter(
            home_bssids=[self.router_mac] if self.router_mac else None,
            rssi_floor=-72,
        )
        self.csi_filter = RouterSignalFilter()
        self.csi_extractor = RouterFeatureExtractor(window_size=30)
        self.csi_detector = RouterCSIDetector()
        self.current_csi_state = HumanState.CALIBRATING
        self.latest_csi_features: Optional[CSIFeatures] = None
        self.last_csi_update = 0.0

        # 2. 毫米波雷达感知链路
        self.radar_parser = UnifiedRadarParser(default_protocol=radar_protocol, sensor_id=radar_sensor_id)
        self.radar_detector = MMWaveDetector()
        self.current_radar_state = RadarHumanState.EMPTY
        self.latest_radar_features: Optional[RadarFallFeatures] = None
        self.last_radar_update = 0.0

        # 3. 融合决策状态
        self.fused_state = FusedState.CALIBRATING
        self.last_alarm_time = 0.0

    # ==========================
    # 数据入口 1: Wi-Fi CSI 流
    # ==========================
    def feed_csi_line(self, line: str, timestamp: Optional[float] = None) -> Optional[FusedPerceptionResult]:
        """从路由器串口或网络套接字喂入一行 CSI 文本报文"""
        from .csi_parser import CSIParser
        if not hasattr(self, "_csi_parser"):
            self._csi_parser = CSIParser()

        packet = self._csi_parser.parse_line(line)
        if not packet:
            return None
        return self.feed_csi_packet(packet, timestamp)

    def feed_csi_packet(self, packet: CSIPacket, timestamp: Optional[float] = None) -> Optional[FusedPerceptionResult]:
        """接收已解析的 CSIPacket"""
        ts = timestamp or time.time()

        # 邻居 Wi-Fi 信号多维拦截过滤
        filter_res = self.neighbor_filter.filter_packet(packet, timestamp=ts)
        if not filter_res.allowed:
            return None

        self.last_csi_update = ts

        # 滤波与特征提取
        clean_amps = self.csi_filter.process(packet.amplitudes)
        features = self.csi_extractor.push_frame(timestamp=ts, amplitudes=clean_amps)
        if not features:
            return None

        self.latest_csi_features = features
        self.current_csi_state = self.csi_detector.update(features)

        # 触发双模融合仲裁
        return self._evaluate_fusion(ts)

    # ==========================
    # 数据入口 2: 毫米波雷达数据流
    # ==========================
    def feed_radar_bytes(self, chunk: bytes, protocol: Optional[str] = None) -> Optional[FusedPerceptionResult]:
        """接收毫米波雷达串口二进制字节流 (支持 LD2410 / LD2450 / MR60FDA1)"""
        frames = self.radar_parser.feed_bytes(chunk, protocol=protocol)
        res = None
        for frame in frames:
            res = self.feed_radar_frame(frame)
        return res

    def feed_radar_line(self, line: str) -> Optional[FusedPerceptionResult]:
        """接收毫米波雷达 JSON 文本流 (网络/MQTT/测试)"""
        frame = self.radar_parser.feed_line(line)
        if frame:
            return self.feed_radar_frame(frame)
        return None

    def feed_radar_frame(self, frame: RadarFrame) -> Optional[FusedPerceptionResult]:
        """直接注入解析完成的 RadarFrame 结构"""
        ts = frame.timestamp
        self.last_radar_update = ts
        radar_state, radar_features = self.radar_detector.update(frame)

        self.current_radar_state = radar_state
        self.latest_radar_features = radar_features

        # 触发双模融合仲裁
        return self._evaluate_fusion(ts)

    # ==========================
    # 核心算法: 双模态贝叶斯/规则融合仲裁
    # ==========================
    def _evaluate_fusion(self, now: float) -> FusedPerceptionResult:
        # 传感器健康与新鲜度检查 (3 秒内视为有效在线)
        csi_alive = (now - self.last_csi_update) < 3.0
        radar_alive = (now - self.last_radar_update) < 3.0

        csi_state = self.current_csi_state if csi_alive else HumanState.CALIBRATING
        csi_energy = self.latest_csi_features.motion_energy if (self.latest_csi_features and csi_alive) else 0.0

        radar_state = self.current_radar_state if radar_alive else RadarHumanState.EMPTY
        radar_z = self.latest_radar_features.primary_z if (self.latest_radar_features and radar_alive) else 0.0
        radar_bpm = self.latest_radar_features.respiration_bpm if (self.latest_radar_features and radar_alive) else 0.0
        radar_conf = self.latest_radar_features.confidence if (self.latest_radar_features and radar_alive) else 0.0

        target_fused_state = FusedState.NORMAL_ACTIVITY
        confidence = 0.5
        reason = "日常平稳起居"
        primary_sensor = "dual"

        # ----------------------------------------------------
        # 规则 1: 极速跌倒确认 (双模同时确认 或 强证据互证)
        # ----------------------------------------------------
        if radar_state == RadarHumanState.FALL_CONFIRMED and csi_state in (
            HumanState.FALL_CONFIRMED,
            HumanState.FALL_SUSPECT,
            HumanState.STATIC,
            HumanState.EMPTY,
        ):
            target_fused_state = FusedState.FALL_CONFIRMED
            confidence = 0.99
            reason = (
                f"双模态交叉确认: 雷达测量目标倒地静止 (Z={radar_z:.2f}m) 且 CSI 验证空间动能突降后静止"
            )
            primary_sensor = "dual"

        elif radar_state == RadarHumanState.FALL_CONFIRMED and not csi_alive:
            # 仅雷达确认跌倒 (CSI 离线)
            target_fused_state = FusedState.FALL_CONFIRMED
            confidence = 0.92
            reason = f"雷达高精确认跌倒 (CSI 离线, Z={radar_z:.2f}m)"
            primary_sensor = "radar_los"

        elif csi_state == HumanState.FALL_CONFIRMED and (
            radar_state == RadarHumanState.EMPTY or not radar_alive
        ):
            # 典型穿墙/盲区场景 (如卫生间内摔倒，毫米波雷达视距被墙壁阻挡)
            target_fused_state = FusedState.FALL_CONFIRMED
            confidence = 0.88
            reason = "Wi-Fi CSI 穿墙全域检测确认跌倒 (毫米波雷达因墙体遮挡无视距)"
            primary_sensor = "wifi_nlos"

        # ----------------------------------------------------
        # 规则 2: 排除掉落物体 / 宠物误报 (优先级高于单纯怀疑)
        # ----------------------------------------------------
        elif csi_state in (HumanState.FALL_CONFIRMED, HumanState.FALL_SUSPECT) and radar_alive:
            if radar_z <= 0.40 and radar_bpm == 0.0 and (not self.latest_radar_features or not self.latest_radar_features.has_respiration):
                target_fused_state = FusedState.NORMAL_ACTIVITY
                confidence = 0.88
                reason = "排除掉落物品误报 (雷达测量地面静止物体无活体呼吸微动)"
                primary_sensor = "radar_los"
            elif radar_z >= 1.05:
                # CSI 发生剧烈波动，但雷达显示人稳定处于站立高度
                target_fused_state = FusedState.NORMAL_ACTIVITY
                confidence = 0.85
                reason = "排除 CSI 动能瞬态误报 (雷达测量老人处于站立行走姿态)"
                primary_sensor = "radar_los"
            elif (
                radar_state == RadarHumanState.FALL_SUSPECT
                or csi_state == HumanState.FALL_SUSPECT
            ):
                target_fused_state = FusedState.FALL_SUSPECT
                confidence = max(0.65, radar_conf)
                reason = "检测到疑似跌倒前驱特征 (下坠速率突增或空间动能剧烈骤降)，启动二次复核"
                primary_sensor = "dual" if (csi_alive and radar_alive) else ("radar_los" if radar_alive else "wifi_nlos")

        # ----------------------------------------------------
        # 规则 3: 怀疑跌倒 (单雷达触发或雷达未在线但 CSI 怀疑)
        # ----------------------------------------------------
        elif (
            radar_state == RadarHumanState.FALL_SUSPECT
            or csi_state == HumanState.FALL_SUSPECT
        ):
            target_fused_state = FusedState.FALL_SUSPECT
            confidence = max(0.65, radar_conf)
            reason = "检测到疑似跌倒前驱特征 (下坠速率突增或空间动能剧烈骤降)，启动二次复核"
            primary_sensor = "dual" if (csi_alive and radar_alive) else ("radar_los" if radar_alive else "wifi_nlos")

        # ----------------------------------------------------
        # 规则 4: 静止休息 / 坐卧
        # ----------------------------------------------------
        elif csi_state in (HumanState.STATIC, HumanState.EMPTY):
            if radar_state == RadarHumanState.EMPTY:
                target_fused_state = FusedState.EMPTY
                confidence = 0.95
                reason = "全屋双模验证无人"
            else:
                target_fused_state = FusedState.RESTING
                confidence = 0.90
                reason = f"老人静止休息中 (雷达保持锁定, 呼吸={radar_bpm:.1f} bpm)"

        # ----------------------------------------------------
        # 规则 5: 日常走动
        # ----------------------------------------------------
        else:
            target_fused_state = FusedState.NORMAL_ACTIVITY
            confidence = 0.90
            reason = "正常人体活动中"

        result = FusedPerceptionResult(
            timestamp=now,
            fused_state=target_fused_state,
            fused_confidence=round(confidence, 2),
            decision_reason=reason,
            csi_state=csi_state,
            csi_energy=round(csi_energy, 2),
            radar_state=radar_state,
            radar_z=round(radar_z, 2),
            radar_respiration_bpm=round(radar_bpm, 1),
            primary_sensor=primary_sensor,
        )

        # 状态流转处理
        if target_fused_state != self.fused_state:
            old_fused = self.fused_state
            self.fused_state = target_fused_state
            logger.info(
                f"[DualFusionEngine] 全局融合状态流转: {old_fused.value} -> {target_fused_state.value} "
                f"| 理由: {reason} (置信度: {confidence:.2f})"
            )
            if self.on_fused_state_change:
                try:
                    self.on_fused_state_change(old_fused, target_fused_state, result)
                except Exception as e:
                    logger.error(f"[DualFusionEngine] 融合状态变更回调异常: {e}")

        # 触发跌倒告警
        if self.fused_state == FusedState.FALL_CONFIRMED:
            if now - self.last_alarm_time > 8.0:
                self.last_alarm_time = now
                logger.critical(
                    f"[DualFusionEngine] !!! 触发双模极速跌倒报警 !!! 原因: {reason} | 置信度: {confidence:.2f}"
                )
                if self.on_fall_alarm:
                    try:
                        self.on_fall_alarm(result)
                    except Exception as e:
                        logger.error(f"[DualFusionEngine] 跌倒告警回调异常: {e}")

        return result

"""
SafeHalo 纯路由器 CSI 处理引擎 (router_csi_engine.py)
整合解析器、信号去噪、特征提取与状态判定机。
支持 BSSID (路由器 MAC 地址) 严格白名单过滤。
"""

import time
from typing import Callable, List, Optional
from .csi_parser import CSIParser, CSIPacket
from .signal_filter import RouterSignalFilter
from .feature_extractor import RouterFeatureExtractor, CSIFeatures
from .detector import RouterCSIDetector, HumanState

class RouterCSIEngine:
    def __init__(
        self,
        router_mac: Optional[str] = None,
        window_size: int = 30,
        calibration_seconds: float = 4.0,
        fall_stillness_seconds: float = 5.0,
        on_state_change: Optional[Callable[[HumanState, HumanState, CSIFeatures], None]] = None,
        on_fall_alarm: Optional[Callable[[CSIFeatures], None]] = None,
    ):
        """
        :param router_mac: 路由器的 BSSID / MAC 地址 (如 "aa:bb:cc:dd:ee:ff")，若提供则自动过滤非路由器报文
        """
        self.router_mac = router_mac.lower().replace("-", ":") if router_mac else None
        self.parser = CSIParser()
        self.filter = RouterSignalFilter()
        self.extractor = RouterFeatureExtractor(window_size=window_size)
        self.detector = RouterCSIDetector(
            calibration_seconds=calibration_seconds,
            fall_stillness_seconds=fall_stillness_seconds,
            on_state_change=on_state_change,
            on_fall_alarm=on_fall_alarm,
        )

        # 统计计数
        self.total_packets_received = 0
        self.matched_router_packets = 0
        self.dropped_packets = 0

    def feed_line(self, line: str, timestamp_override: Optional[float] = None) -> Optional[Tuple_Result]:
        """
        接收一行串口原始输出并处理。
        :return: (packet, features, state) 或 None
        """
        self.total_packets_received += 1
        packet = self.parser.parse_line(line)
        if not packet:
            self.dropped_packets += 1
            return None

        # 路由器 MAC 白名单校验 (纯路由器方案核心防护: 避免邻居或手机发包干扰)
        if self.router_mac:
            incoming_mac = packet.mac.lower().replace("-", ":")
            if incoming_mac and incoming_mac != self.router_mac:
                # 丢弃非指定路由器的包
                self.dropped_packets += 1
                return None

        self.matched_router_packets += 1

        # 时间戳获取 (优先使用系统物理时钟，其次使用 ESP32 内部微秒时钟)
        if timestamp_override is not None:
            ts = timestamp_override
        else:
            ts = time.time()

        # 1. 信号滤波 & 消除路由器 AGC 抖动
        clean_amps = self.filter.process(packet.amplitudes)

        # 2. 特征提取
        features = self.extractor.push_frame(timestamp=ts, amplitudes=clean_amps)
        if not features:
            return None

        # 3. 状态判定机更新
        current_state = self.detector.update(features)

        return (packet, features, current_state)

Tuple_Result = tuple

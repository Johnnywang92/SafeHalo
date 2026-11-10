"""
SafeHalo: 路由原生 Wi-Fi CSI 与毫米波雷达双模态空间感知与老人防跌倒系统
"""

from .csi_parser import CSIPacket, CSIParser
from .signal_filter import RouterSignalFilter
from .feature_extractor import RouterFeatureExtractor, CSIFeatures
from .detector import RouterCSIDetector, HumanState
from .router_csi_engine import RouterCSIEngine
from .multilink_fusion import MultiLinkFusionEngine, FusedSpatialResult
from .mmwave_parser import (
    RadarFrame,
    RadarTarget,
    RadarPosture,
    LD2410Parser,
    LD2450Parser,
    MR60FDA1Parser,
    JsonRadarParser,
    UnifiedRadarParser,
)
from .mmwave_detector import MMWaveDetector, RadarHumanState, RadarFallFeatures
from .dual_fusion_engine import DualSensorFusionEngine, FusedState, FusedPerceptionResult
from .routine_monitor import (
    RoutineMonitorEngine,
    RoutineProfileConfig,
    RoutineAnomalyType,
    RoutineAnomalyEvent,
)
from .placement_adapter import (
    PlacementAdapter,
    RoomPlacementConfig,
    TunedParameters,
    RoomType,
    MountHeightLevel,
)
from .auto_calibrator import ContinuousAutoCalibrator, BaselineModelState
from .miniprogram_bridge import (
    WeChatMiniProgramBridge,
    IncidentFeedbackManager,
    AlertIncidentRecord,
    IncidentStatus,
)
from .pilot_telemetry import (
    PilotHouseholdManager,
    PilotHouseholdProfile,
    MonthlyHouseholdStats,
)

__all__ = [
    "CSIPacket",
    "CSIParser",
    "RouterSignalFilter",
    "RouterFeatureExtractor",
    "CSIFeatures",
    "RouterCSIDetector",
    "HumanState",
    "RouterCSIEngine",
    "MultiLinkFusionEngine",
    "FusedSpatialResult",
    "RadarFrame",
    "RadarTarget",
    "RadarPosture",
    "LD2410Parser",
    "LD2450Parser",
    "MR60FDA1Parser",
    "JsonRadarParser",
    "UnifiedRadarParser",
    "MMWaveDetector",
    "RadarHumanState",
    "RadarFallFeatures",
    "DualSensorFusionEngine",
    "FusedState",
    "FusedPerceptionResult",
    "RoutineMonitorEngine",
    "RoutineProfileConfig",
    "RoutineAnomalyType",
    "RoutineAnomalyEvent",
    "PlacementAdapter",
    "RoomPlacementConfig",
    "TunedParameters",
    "RoomType",
    "MountHeightLevel",
]

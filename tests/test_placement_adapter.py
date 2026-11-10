"""
SafeHalo 路由器摆放位置与算法自适应单元测试 (test_placement_adapter.py)
"""

import os
import shutil
import tempfile
import unittest

from src.safehalo.placement_adapter import (
    PlacementAdapter,
    RoomPlacementConfig,
    TunedParameters,
    RoomType,
    MountHeightLevel,
)
from src.safehalo.router_csi_engine import RouterCSIEngine
from src.safehalo.dual_fusion_engine import DualSensorFusionEngine


class TestPlacementAdapter(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.adapter = PlacementAdapter(config_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_bathroom_ultra_sensitive_tuning(self):
        """测试卫生间高危场景算法自适应: 极高灵敏冲击阈值 (1.15), 3 秒极速报警, 水流去噪激活"""
        cfg = RoomPlacementConfig(
            room_type="BATHROOM",
            room_name="主卧卫生间",
            mount_height="MID_DESK",
            has_water_splash=True,
        )
        params = PlacementAdapter.compute_tuned_parameters(cfg)

        self.assertEqual(params.fall_spike_threshold, 1.15)
        self.assertEqual(params.fall_stillness_seconds, 3.0)
        self.assertTrue(params.water_flow_damping)
        self.assertEqual(params.radar_floor_height_thresh, 0.35)
        self.assertIn("卫生间极速防滑响应", params.description)

    def test_bedroom_bed_avoidance_tuning(self):
        """测试卧室场景算法自适应: 避让 0.5m 床面, 地面高度收紧至 0.30m, 4.5 秒确认"""
        cfg = RoomPlacementConfig(
            room_type="BEDROOM",
            room_name="老人卧室",
            mount_height="MID_DESK",
            has_pets=False,
        )
        params = PlacementAdapter.compute_tuned_parameters(cfg)

        self.assertEqual(params.fall_spike_threshold, 1.40)
        self.assertEqual(params.fall_stillness_seconds, 4.5)
        self.assertEqual(params.radar_floor_height_thresh, 0.30)
        self.assertIn("卧室床铺安全避让", params.description)

    def test_living_room_with_pets_tuning(self):
        """测试客厅开阔场景 + 家养宠物自适应: SPR 提升至 50%, 时域滤波放大至 7 帧"""
        cfg = RoomPlacementConfig(
            room_type="LIVING_ROOM",
            room_name="大客厅",
            mount_height="MID_DESK",
            has_pets=True,
        )
        params = PlacementAdapter.compute_tuned_parameters(cfg)

        self.assertEqual(params.fall_spike_threshold, 2.10)
        self.assertEqual(params.pet_spr_threshold, 0.50)
        self.assertEqual(params.hampel_window_size, 7)
        self.assertIn("家有猫狗", params.description)

    def test_mount_height_compensation(self):
        """测试不同安装高度 (矮柜 vs 吸顶) 的物理电磁补偿"""
        # 低位 (矮柜 0.5m): 地面多径反射放大，阈值上浮 15%
        cfg_low = RoomPlacementConfig(room_type="LIVING_ROOM", mount_height="LOW")
        p_low = PlacementAdapter.compute_tuned_parameters(cfg_low)
        self.assertAlmostEqual(p_low.fall_spike_threshold, 2.10 * 1.15, places=2)

        # 高位 (吸顶 2.2m): 垂直俯视分量明显，阈值下浮 5%
        cfg_high = RoomPlacementConfig(room_type="LIVING_ROOM", mount_height="HIGH_CEILING")
        p_high = PlacementAdapter.compute_tuned_parameters(cfg_high)
        self.assertAlmostEqual(p_high.fall_spike_threshold, 2.10 * 0.95, places=2)

    def test_save_and_apply_to_engines(self):
        """测试保存配置并将超参数动态注入 CSI 引擎与双模引擎"""
        cfg, params = self.adapter.save_config(
            room_type="BATHROOM",
            room_name="次卫",
            mount_height="MID_DESK",
        )
        self.assertTrue(cfg.is_configured)
        self.assertEqual(cfg.room_name, "次卫")

        # 验证配置文件已落盘
        self.assertTrue(os.path.exists(self.adapter.config_file))

        # 测试实例化 CSI 引擎并下发参数
        csi_engine = RouterCSIEngine()
        dual_engine = DualSensorFusionEngine()

        self.adapter.apply_to_engines(csi_engine=csi_engine, dual_engine=dual_engine)

        # 检查 CSI 探测器内部参数是否已生效
        self.assertEqual(csi_engine.detector.fall_spike_threshold, 1.15)
        self.assertEqual(csi_engine.detector.fall_stillness_seconds, 3.0)

        # 检查毫米波雷达探测器内部参数是否已生效
        self.assertEqual(dual_engine.radar_detector.floor_height_thresh, 0.35)
        self.assertEqual(dual_engine.radar_detector.stillness_confirm_sec, 3.0)


if __name__ == "__main__":
    unittest.main()

"""
SafeHalo 毫米波雷达解析、判定与双模融合单元测试 (test_mmwave_fusion.py)
验证:
1. LD2410 / LD2450 / MR60FDA1 / JSON 各协议报文解码
2. 毫米波空间高度骤降与地面静止判定时序
3. 双模态异构融合决策 (双模跌倒秒级确认、穿墙盲区接管、掉落杂物/宠物误报排除)
"""

import struct
import time
import unittest

from src.safehalo.mmwave_parser import (
    LD2410Parser,
    LD2450Parser,
    MR60FDA1Parser,
    JsonRadarParser,
    UnifiedRadarParser,
    RadarPosture,
    RadarFrame,
    RadarTarget,
)
from src.safehalo.mmwave_detector import (
    MMWaveDetector,
    RadarHumanState,
    RadarFallFeatures,
)
from src.safehalo.dual_fusion_engine import (
    DualSensorFusionEngine,
    FusedState,
    FusedPerceptionResult,
)
from src.safehalo.detector import HumanState
from src.safehalo.csi_parser import CSIPacket


class TestMMWaveParser(unittest.TestCase):
    def test_ld2410_parser(self):
        parser = LD2410Parser(sensor_id="radar_test")
        # 构造一个 LD2410 目标报文:
        # 头: FD FC FB FA
        # 长度: 2 字节小端 (8 字节数据)
        # payload:
        #   type: 0x02
        #   state: 0x01 (运动目标)
        #   move_dist: 180 cm (0x00B4)
        #   move_energy: 75 (0x4B)
        #   stat_dist: 0 cm (0x0000)
        #   stat_energy: 0 (0x00)
        #   detect_dist: 600 cm (0x0258)
        # 尾: 04 03 02 01
        payload = bytes([0x02, 0x01]) + struct.pack("<H", 180) + bytes([75]) + struct.pack("<H", 0) + bytes([0, 0x58, 0x02])
        data_len = len(payload)
        frame_bytes = (
            LD2410Parser.FRAME_HEADER
            + struct.pack("<H", data_len)
            + payload
            + LD2410Parser.FRAME_TAIL
        )

        frames = parser.parse_bytes(frame_bytes)
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertTrue(f.human_presence)
        self.assertEqual(f.motion_state, "motion")
        self.assertEqual(len(f.targets), 1)
        self.assertAlmostEqual(f.targets[0].y, 1.80, places=2)
        self.assertEqual(f.targets[0].energy, 75.0)

    def test_ld2450_parser(self):
        parser = LD2450Parser(sensor_id="radar_ld2450")
        # 帧长 30 字节
        # Header: AA FF 03 00
        # Target 1: x = 500mm (0.5m), y = 2500mm (2.5m), speed = -30cm/s (-0.3m/s), res = 100
        # Target 2: 0, 0, 0, 0
        # Target 3: 0, 0, 0, 0
        # Tail: 55 CC
        t1 = struct.pack("<hhhH", 500, 2500, -30, 100)
        t2 = struct.pack("<hhhH", 0, 0, 0, 0)
        t3 = struct.pack("<hhhH", 0, 0, 0, 0)
        raw = LD2450Parser.FRAME_HEADER + t1 + t2 + t3 + LD2450Parser.FRAME_TAIL
        self.assertEqual(len(raw), 30)

        frames = parser.parse_bytes(raw)
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertTrue(f.human_presence)
        self.assertEqual(len(f.targets), 1)
        t = f.targets[0]
        self.assertAlmostEqual(t.x, 0.5, places=2)
        self.assertAlmostEqual(t.y, 2.5, places=2)
        self.assertAlmostEqual(t.speed, -0.3, places=2)
        self.assertEqual(t.posture, RadarPosture.MOVING)

    def test_mr60fda1_parser(self):
        parser = MR60FDA1Parser(sensor_id="radar_mr60")
        # 构造 MR60FDA1 跌倒上报:
        # 53 59 | ctrl: 0x83 (跌倒) | cmd: 0x01 | len: 00 01 | payload: 0x01 (跌倒) | sum: 00 | tail: 54 43
        raw = bytes([0x53, 0x59, 0x83, 0x01, 0x00, 0x01, 0x01, 0x00, 0x54, 0x43])
        frames = parser.parse_bytes(raw)
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertTrue(f.fall_flag)
        self.assertEqual(f.targets[0].posture, RadarPosture.LYING_FLOOR)
        self.assertAlmostEqual(f.targets[0].z, 0.2, places=2)

    def test_json_parser(self):
        parser = JsonRadarParser()
        json_line = '{"sensor_id": "living_radar", "fall_flag": false, "targets": [{"id": 1, "x": -0.2, "y": 3.1, "z": 0.25, "speed": 0.0, "posture": "lying", "bpm": 18.0}]}'
        frame = parser.parse_line(json_line)
        self.assertIsNotNone(frame)
        self.assertTrue(frame.human_presence)
        self.assertEqual(len(frame.targets), 1)
        self.assertEqual(frame.targets[0].posture, RadarPosture.LYING_FLOOR)
        self.assertEqual(frame.targets[0].respiration_bpm, 18.0)


class TestMMWaveDetector(unittest.TestCase):
    def test_height_drop_and_stillness_fall_confirmation(self):
        state_changes = []
        fall_alarms = []

        detector = MMWaveDetector(
            floor_height_thresh=0.40,
            standing_height_min=1.0,
            fall_descent_thresh=1.0,
            stillness_confirm_sec=0.4,  # 加速测试
            on_state_change=lambda old, new, feat: state_changes.append((old, new)),
            on_fall_alarm=lambda feat: fall_alarms.append(feat),
        )

        now = 1000.0
        # 1. 站立阶段
        t_stand = RadarTarget(target_id=1, x=0, y=2.0, z=1.65, speed=0.0, posture=RadarPosture.STAND)
        f_stand = RadarFrame(timestamp=now, sensor_id="r1", protocol="test", human_presence=True, targets=[t_stand])
        state, feat = detector.update(f_stand)
        self.assertEqual(state, RadarHumanState.STANDING)

        # 2. 快速下坠 -> 倒地 (0.4秒内高度由 1.65m 坠至 0.25m, dz/dt ~ 3.5 m/s)
        now += 0.4
        t_fallen = RadarTarget(target_id=1, x=0, y=2.0, z=0.25, speed=-1.5, posture=RadarPosture.LYING_FLOOR, respiration_bpm=16.0)
        f_fallen = RadarFrame(timestamp=now, sensor_id="r1", protocol="test", human_presence=True, targets=[t_fallen])
        state, feat = detector.update(f_fallen)
        self.assertEqual(state, RadarHumanState.FALL_SUSPECT)
        self.assertGreater(feat.descent_rate, 1.0)

        # 3. 在地面持续静止超过 0.4 秒 -> 升级为 FALL_CONFIRMED
        now += 0.5
        f_still = RadarFrame(timestamp=now, sensor_id="r1", protocol="test", human_presence=True, targets=[t_fallen])
        state, feat = detector.update(f_still)
        self.assertEqual(state, RadarHumanState.FALL_CONFIRMED)
        self.assertEqual(len(fall_alarms), 1)
        self.assertGreaterEqual(feat.confidence, 0.8)


class TestDualSensorFusionEngine(unittest.TestCase):
    def setUp(self):
        self.fused_changes = []
        self.fused_alarms = []
        self.engine = DualSensorFusionEngine(
            router_mac="c0:06:c3:11:22:33",
            on_fused_state_change=lambda old, new, res: self.fused_changes.append((old, new)),
            on_fall_alarm=lambda res: self.fused_alarms.append(res),
        )

    def test_dual_fall_cross_confirmation(self):
        """测试场景: CSI 动能骤降静止 + 毫米波雷达测量目标跌至地面 (Z=0.25m) -> 极速 99% 双模报警"""
        now = time.time()
        # 1. 注入雷达数据: 确认跌倒，高度 0.22m，有呼吸微动
        t_fall = RadarTarget(target_id=1, x=0, y=2.2, z=0.22, posture=RadarPosture.LYING_FLOOR, respiration_bpm=16.5)
        r_frame = RadarFrame(timestamp=now, sensor_id="r1", protocol="test", human_presence=True, fall_flag=True, targets=[t_fall])
        # 模拟雷达内部完成地面静止超时
        self.engine.radar_detector.state = RadarHumanState.FALL_CONFIRMED
        self.engine.radar_detector.last_state_change = now - 5.0

        res = self.engine.feed_radar_frame(r_frame)
        self.assertIsNotNone(res)

        # 2. 模拟 CSI 处于静止/跌倒检测态
        self.engine.current_csi_state = HumanState.FALL_CONFIRMED
        self.engine.last_csi_update = now

        res = self.engine.feed_radar_frame(r_frame)
        self.assertEqual(res.fused_state, FusedState.FALL_CONFIRMED)
        self.assertGreaterEqual(res.fused_confidence, 0.95)
        self.assertIn("双模态交叉确认", res.decision_reason)

    def test_wall_penetration_fallback(self):
        """测试场景: 老人在卫生间跌倒，雷达因承重墙遮挡为空 (Empty)，Wi-Fi CSI 穿墙报警接管"""
        now = time.time()
        # 毫米波雷达报告无人 (被墙遮挡)
        r_empty = RadarFrame(timestamp=now, sensor_id="r1", protocol="test", human_presence=False, targets=[])
        self.engine.feed_radar_frame(r_empty)

        # Wi-Fi CSI 穿墙检测触发 FALL_CONFIRMED
        self.engine.current_csi_state = HumanState.FALL_CONFIRMED
        self.engine.last_csi_update = now

        res = self.engine._evaluate_fusion(now)
        self.assertEqual(res.fused_state, FusedState.FALL_CONFIRMED)
        self.assertEqual(res.primary_sensor, "wifi_nlos")
        self.assertIn("穿墙全域检测", res.decision_reason)

    def test_inanimate_drop_rejection(self):
        """测试场景: 掉落枕头/大衣物，CSI 出现剧烈动能跳变后静止，但雷达测定高度虽低却无胸腔呼吸微动 -> 排除误报"""
        now = time.time()
        # 雷达报告地面静止目标，但没有呼吸微动 (0 bpm)
        t_bag = RadarTarget(target_id=1, x=0, y=1.5, z=0.15, posture=RadarPosture.LYING_FLOOR, respiration_bpm=0.0)
        r_frame = RadarFrame(timestamp=now, sensor_id="r1", protocol="test", human_presence=True, targets=[t_bag])
        self.engine.feed_radar_frame(r_frame)

        # CSI 发生突变
        self.engine.current_csi_state = HumanState.FALL_SUSPECT
        self.engine.last_csi_update = now

        res = self.engine._evaluate_fusion(now)
        self.assertNotEqual(res.fused_state, FusedState.FALL_CONFIRMED)
        self.assertIn("排除掉落物品误报", res.decision_reason)

    def test_standing_pet_rejection(self):
        """测试场景: 宠物在地面剧烈奔跑导致 CSI 波动，但雷达显示主人在站立走动 (Z=1.65m) -> 排除 CSI 跌倒怀疑"""
        now = time.time()
        t_human = RadarTarget(target_id=1, x=0, y=2.0, z=1.65, speed=0.3, posture=RadarPosture.MOVING)
        r_frame = RadarFrame(timestamp=now, sensor_id="r1", protocol="test", human_presence=True, targets=[t_human])
        self.engine.feed_radar_frame(r_frame)

        # CSI 因宠物触发怀疑
        self.engine.current_csi_state = HumanState.FALL_SUSPECT
        self.engine.last_csi_update = now

        res = self.engine._evaluate_fusion(now)
        self.assertEqual(res.fused_state, FusedState.NORMAL_ACTIVITY)
        self.assertIn("排除 CSI 动能瞬态误报", res.decision_reason)


if __name__ == "__main__":
    unittest.main()

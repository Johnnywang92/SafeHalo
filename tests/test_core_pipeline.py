"""
SafeHalo 核心单模与多链路感知流水线单元测试 (tests/test_core_pipeline.py)
覆盖:
1. CSIParser 报文解析与子载波幅相还原
2. RouterSignalFilter 去噪、水流阻尼与厨房金属谐振阻尼
3. PetImmunityFilter 宠物识别与抑制
4. TargetedFallInspector 定向深度复查与 100Hz 突发提频回调
5. SecurityGuardEngine 离家布防、夜间布防、老人防走失 (CURFEW) 模式
6. MultiLinkFusionEngine 多 AP 空间分集与房间级微定位
7. RoutineMonitorEngine 深夜严重未眠 (IRREGULAR_LATE_SLEEP) 异常诊断
8. MiniMQTTClient 下行 PUBLISH 控制指令解析
9. PlacementAdapter 误报反馈自适应调参
10. scripts/evaluator.py 阶段 1 基准评估器验证
"""

from datetime import datetime
import math
import struct
import tempfile
import unittest

from src.safehalo.csi_parser import CSIParser
from src.safehalo.signal_filter import RouterSignalFilter
from src.safehalo.pet_filter import PetImmunityFilter
from src.safehalo.targeted_inspector import TargetedFallInspector, TargetedInspectionState
from src.safehalo.security_guard import SecurityGuardEngine, SecurityMode, IntrusionAlertLevel
from src.safehalo.detector import HumanState
from src.safehalo.feature_extractor import CSIFeatures
from src.safehalo.multilink_fusion import MultiLinkFusionEngine
from src.safehalo.routine_monitor import RoutineMonitorEngine, RoutineAnomalyType
from src.safehalo.ha_discovery import MiniMQTTClient
from src.safehalo.placement_adapter import PlacementAdapter
from src.safehalo.router_csi_engine import RouterCSIEngine
from scripts.evaluator import generate_synthetic_benchmark_stream, evaluate_stream


class TestCorePipeline(unittest.TestCase):
    def test_csi_parser_and_damping_filters(self):
        """测试 CSI 解析器及信号滤波器的水流阻尼/金属多径阻尼"""
        parser = CSIParser()
        raw_iq = [3, 4] * 64  # 每个子载波幅值 = 5.0
        raw_iq[40] = 60       # 模拟局部金属谐振尖峰
        raw_iq[41] = 80
        line = f'CSI_DATA,1,10,aa:bb:cc:11:22:33,-48,0,0,0,0,0,0,0,0,0,0,-92,0,6,0,1000,0,0,0,128,0,"{raw_iq}"'

        pkt = parser.parse_line(line)
        self.assertIsNotNone(pkt)
        self.assertEqual(pkt.subcarrier_count, 64)
        self.assertEqual(pkt.rssi, -48)

        flt = RouterSignalFilter(water_flow_damping=True, metal_multipath_damping=True)
        out1 = flt.process(pkt.amplitudes)
        out2 = flt.process(pkt.amplitudes)
        self.assertEqual(len(out1), len(out2))
        # L2 归一化后平方和应接近 1.0
        self.assertAlmostEqual(sum(x * x for x in out1), 1.0, places=4)

    def test_pet_immunity_filter(self):
        """测试地面宠物低载波参与率 (SPR) 识别"""
        pet_flt = PetImmunityFilter(subcarrier_spr_threshold=0.45)
        # 50 个子载波中仅 8 个受扰动 (SPR = 16% < 45%)，且整体能量适中 (diff_ratio = 2.0)
        variances = [0.0005 if i < 8 else 0.00005 for i in range(50)]
        res = pet_flt.evaluate(
            subcarrier_variances=variances,
            base_variance=0.0001,
            diff_energy=0.002,
            base_diff=0.001,
        )
        self.assertTrue(res.is_pet)
        self.assertLess(res.subcarrier_participation, 0.45)

    def test_targeted_inspector_and_burst_callback(self):
        """测试疑似跌倒定向复查的 100Hz 激振回调与地面呼吸判决"""
        burst_rates = []
        inspector = TargetedFallInspector(
            inspection_duration_seconds=3.0,
            on_request_burst_rate=lambda hz: burst_rates.append(hz),
        )
        t0 = 1000.0
        inspector.trigger_inspection(t0, "单元测试触发")
        self.assertEqual(burst_rates[-1], 100)
        self.assertEqual(inspector.current_state, TargetedInspectionState.HIGH_FREQ_BURST)

        # 喂入带有微弱呼吸波动的倒地帧直至超时确诊
        verdict = None
        for step in range(1, 100):
            ts = t0 + step * 0.04
            breath = 0.01 + 0.002 * math.sin(2 * math.pi * 0.25 * (ts - t0))
            verdict = inspector.update_frame(
                timestamp=ts,
                diff_energy=0.0014,
                base_diff=0.0010,
                subcarrier_amplitudes=[breath] * 32,
            )
            if verdict:
                break

        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.state, TargetedInspectionState.VERDICT_FALL)
        # 复查结束后自动恢复 30Hz
        self.assertEqual(burst_rates[-1], 30)

    def test_security_guard_modes(self):
        """测试离家布防、夜间布防、老人防走失 (CURFEW) 三大模式"""
        alarms = []
        guard = SecurityGuardEngine(
            initial_mode=SecurityMode.DISARMED,
            on_intruder_alarm=lambda lvl, msg, _f: alarms.append((lvl, msg)),
            grace_period_seconds=0.0,
            curfew_empty_timeout_seconds=10.0,
        )

        # 1. 夜间布防: 静态睡眠不报警，连续大幅走动报警
        guard.set_mode_by_name("ARMED_NIGHT")
        t0 = time_now = 2000.0
        for i in range(5):
            guard.process_state(HumanState.STATIC, CSIFeatures(timestamp=t0 + i, diff_energy=0.001))
        self.assertEqual(len(alarms), 0)

        for i in range(3):
            guard.process_state(HumanState.MOVING, CSIFeatures(timestamp=t0 + 10 + i, diff_energy=0.05))
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[-1][0], IntrusionAlertLevel.ALARM)

        # 2. 防走失模式 (CURFEW): 空房超时触发告警
        alarms.clear()
        guard.set_mode_by_name("CURFEW")
        guard.last_alarm_time = None
        guard.process_state(HumanState.EMPTY, CSIFeatures(timestamp=t0 + 50))
        guard.process_state(HumanState.EMPTY, CSIFeatures(timestamp=t0 + 62))
        self.assertEqual(len(alarms), 1)
        self.assertIn("防走失", alarms[-1][1])

    def test_multilink_fusion_localization(self):
        """测试多链路空间分集融合与房间级微定位"""
        ml = MultiLinkFusionEngine(
            links_config={
                "aa:bb:cc:00:00:01": "客厅",
                "aa:bb:cc:00:00:02": "主卧",
            }
        )
        t0 = 5000.0
        res = None
        for i in range(20):
            ts = t0 + i * 0.05
            # 客厅链路稳定微弱，主卧链路发生剧烈运动
            iq_living = [10, 10] * 64
            iq_bedroom = [int(10 + 12 * math.sin(i + k * 0.3)) for k in range(128)]
            line1 = f'CSI_DATA,1,{i},aa:bb:cc:00:00:01,-45,0,0,0,0,0,0,0,0,0,0,-92,0,6,0,1000,0,0,0,128,0,"{iq_living}"'
            line2 = f'CSI_DATA,1,{i},aa:bb:cc:00:00:02,-48,0,0,0,0,0,0,0,0,0,0,-92,0,6,0,1000,0,0,0,128,0,"{iq_bedroom}"'
            ml.feed_line(line1, timestamp_override=ts)
            res = ml.feed_line(line2, timestamp_override=ts)

        self.assertIsNotNone(res)
        self.assertEqual(res.participating_links, 2)
        self.assertEqual(res.active_room, "主卧")

    def test_routine_irregular_late_sleep(self):
        """测试凌晨 03:00 严重未眠且持续走动异常预警"""
        with tempfile.TemporaryDirectory() as tmpdir:
            mon = RoutineMonitorEngine(storage_dir=tmpdir)
            late_night_dt = datetime(2026, 11, 10, 3, 15, 0)
            ts = late_night_dt.timestamp()
            events = []
            for i in range(4):
                events.extend(
                    mon.update_state(
                        human_state=HumanState.MOVING,
                        timestamp=ts + i * 30,
                        is_presence=True,
                        is_motion=True,
                    )
                )
            self.assertTrue(any(e.anomaly_type == RoutineAnomalyType.IRREGULAR_LATE_SLEEP for e in events))

    def test_mqtt_publish_decode_and_false_alarm_feedback(self):
        """测试 MQTT 下行报文解析与误报反馈自动上浮阈值"""
        topic_bytes = b"safehalo/command/security_mode"
        payload_bytes = b"ARMED_AWAY"
        body = struct.pack("!H", len(topic_bytes)) + topic_bytes + payload_bytes
        topic, payload = MiniMQTTClient.decode_publish_body(body, qos=0)
        self.assertEqual(topic, "safehalo/command/security_mode")
        self.assertEqual(payload, "ARMED_AWAY")

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = PlacementAdapter(config_dir=tmpdir)
            engine = RouterCSIEngine()
            old_thresh = adapter.current_tuned_params.fall_spike_threshold
            new_params = adapter.record_false_alarm_feedback("单元测试误报反馈", csi_engine=engine)
            self.assertGreater(new_params.fall_spike_threshold, old_thresh)
            self.assertEqual(engine.detector.fall_spike_threshold, new_params.fall_spike_threshold)

    def test_phase1_evaluator_benchmark(self):
        """测试阶段 1 评估脚本在标准合成流上的验收通过性"""
        records = generate_synthetic_benchmark_stream()
        report = evaluate_stream(records)
        self.assertEqual(report.empty_false_alarms, 0)
        self.assertTrue(report.fall_detected)
        self.assertTrue(report.passed_phase1)


if __name__ == "__main__":
    unittest.main()

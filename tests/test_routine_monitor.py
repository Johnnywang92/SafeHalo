"""
SafeHalo 老人日常作息画像与异常偏离诊断单元测试 (test_routine_monitor.py)
"""

import os
import shutil
import tempfile
import time
from datetime import datetime
import unittest

from src.safehalo.detector import HumanState
from src.safehalo.routine_monitor import (
    RoutineMonitorEngine,
    RoutineProfileConfig,
    RoutineAnomalyType,
    RoutineAnomalyEvent,
)


class TestRoutineMonitor(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.anomalies_received = []

        # 配置测试基线: 起床 07:30 (容忍 1.5h, 即 09:00 前需起床), 日间最大静止 3.0h, 夜间最大起夜 3 次, 外出最大 4.0h
        self.config = RoutineProfileConfig(
            typical_wake_hour=7.5,
            wake_delay_tolerance_hours=1.5,
            typical_bed_hour=22.5,
            max_daytime_inactivity_hours=3.0,
            max_night_restless_count=3,
            max_outing_hours=4.0,
            auto_learn_enabled=True,
        )

        self.engine = RoutineMonitorEngine(
            config=self.config,
            storage_dir=self.test_dir,
            on_anomaly=lambda evt: self.anomalies_received.append(evt),
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_normal_wake_and_bed_recording(self):
        """测试正常起居时序记录与起床/入睡时间捕捉"""
        # 模拟早上 07:20 起床走动
        ts_wake = datetime.strptime("2026-11-09 07:20:00", "%Y-%m-%d %H:%M:%S").timestamp()
        self.engine.update_state(human_state=HumanState.MOVING, timestamp=ts_wake, is_presence=True, is_motion=True)

        summary = self.engine.get_summary()
        self.assertTrue(summary["woken_up"])
        self.assertEqual(summary["wake_time"], "07:20")
        self.assertEqual(len(self.anomalies_received), 0)

        # 模拟晚上 22:40 静止入睡
        ts_bed = datetime.strptime("2026-11-09 22:40:00", "%Y-%m-%d %H:%M:%S").timestamp()
        self.engine.update_state(human_state=HumanState.STATIC, timestamp=ts_bed, is_presence=True, is_motion=False)

        summary = self.engine.get_summary()
        self.assertEqual(summary["bed_time"], "22:40")

    def test_wakeup_delayed_anomaly(self):
        """测试场景 1: 晨间超过预期起床截止时间 (09:00) 仍未见室内活动 -> 触发 WAKEUP_DELAYED"""
        # 早上 09:15，老人确认在家，但处于 STATIC 静止态，且之前未曾起过床
        ts_delayed = datetime.strptime("2026-11-09 09:15:00", "%Y-%m-%d %H:%M:%S").timestamp()
        self.engine.last_motion_time = ts_delayed - 3600 * 8  # 过去 8 小时无走动

        self.engine.update_state(human_state=HumanState.STATIC, timestamp=ts_delayed, is_presence=True, is_motion=False)

        self.assertEqual(len(self.anomalies_received), 1)
        evt = self.anomalies_received[0]
        self.assertEqual(evt.anomaly_type, RoutineAnomalyType.WAKEUP_DELAYED)
        self.assertEqual(evt.severity, "ALERT")
        self.assertIn("晨间超期未见起床预警", evt.title)

        summary = self.engine.get_summary()
        self.assertTrue(summary["has_anomaly"])

    def test_prolonged_daytime_inactivity(self):
        """测试场景 2: 日间在室内连续超过设定阈值 (3.0h) 无任何活动 -> 触发 PROLONGED_INACTIVITY"""
        # 模拟上午 10:00 有过走动起床
        ts_motion = datetime.strptime("2026-11-09 10:00:00", "%Y-%m-%d %H:%M:%S").timestamp()
        self.engine.update_state(human_state=HumanState.MOVING, timestamp=ts_motion, is_presence=True, is_motion=True)
        self.anomalies_received.clear()

        # 到了下午 13:40 (已静止 3.67 小时，超过设防 3.0 小时)
        ts_inactive = datetime.strptime("2026-11-09 13:40:00", "%Y-%m-%d %H:%M:%S").timestamp()
        self.engine.update_state(human_state=HumanState.STATIC, timestamp=ts_inactive, is_presence=True, is_motion=False)

        self.assertEqual(len(self.anomalies_received), 1)
        evt = self.anomalies_received[0]
        self.assertEqual(evt.anomaly_type, RoutineAnomalyType.PROLONGED_INACTIVITY)
        self.assertIn("日间异常长时间无活动预警", evt.title)

    def test_night_restlessness_anomaly(self):
        """测试场景 3: 夜间起夜走动达到阈值 (>= 3 次) -> 触发 NIGHT_RESTLESSNESS"""
        # 模拟深夜 01:00, 02:00, 03:30 三次起夜走动
        t1 = datetime.strptime("2026-11-09 01:00:00", "%Y-%m-%d %H:%M:%S").timestamp()
        t2 = datetime.strptime("2026-11-09 02:00:00", "%Y-%m-%d %H:%M:%S").timestamp()
        t3 = datetime.strptime("2026-11-09 03:30:00", "%Y-%m-%d %H:%M:%S").timestamp()

        self.engine.update_state(human_state=HumanState.MOVING, timestamp=t1, is_presence=True, is_motion=True)
        self.engine.update_state(human_state=HumanState.MOVING, timestamp=t2, is_presence=True, is_motion=True)
        self.engine.update_state(human_state=HumanState.MOVING, timestamp=t3, is_presence=True, is_motion=True)

        self.assertEqual(self.engine.metrics.night_restless_count, 3)
        self.assertEqual(len(self.anomalies_received), 1)
        evt = self.anomalies_received[0]
        self.assertEqual(evt.anomaly_type, RoutineAnomalyType.NIGHT_RESTLESSNESS)
        self.assertIn("夜间起夜/异常走动频次过高", evt.title)

    def test_prolonged_outing_anomaly(self):
        """测试场景 4: 老人外出超过设定安全时长 (4.0h) 未归 -> 触发 PROLONGED_OUTING"""
        # 模拟上午 09:00 老人离家 (状态转为 EMPTY)
        ts_leave = datetime.strptime("2026-11-09 09:00:00", "%Y-%m-%d %H:%M:%S").timestamp()
        self.engine.update_state(human_state=HumanState.EMPTY, timestamp=ts_leave, is_presence=False, is_motion=False)

        # 下午 13:30 (已离家 4.5 小时，超过阈值 4.0 小时)
        ts_check = datetime.strptime("2026-11-09 13:30:00", "%Y-%m-%d %H:%M:%S").timestamp()
        self.engine.update_state(human_state=HumanState.EMPTY, timestamp=ts_check, is_presence=False, is_motion=False)

        self.assertEqual(len(self.anomalies_received), 1)
        evt = self.anomalies_received[0]
        self.assertEqual(evt.anomaly_type, RoutineAnomalyType.PROLONGED_OUTING)
        self.assertIn("老人长时间离家未归预警", evt.title)


if __name__ == "__main__":
    unittest.main()

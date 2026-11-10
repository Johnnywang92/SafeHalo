"""
SafeHalo 阶段 2 核心闭环与 3~5 户入户试点单元测试 (tests/test_phase2_pilot.py)
覆盖:
1. 四段式跌倒判定：剧烈冲击 -> 持续静止 -> 本地交互质询 (AWAITING_CHALLENGE) -> 应答拦截 vs 无应答报警
2. 多日自动校准引擎 (ContinuousAutoCalibrator)：开机走动抗污染保护、跨天静默窗口 EMA 学习与持久化
3. 家属端微信小程序对接与误报反馈靶向调参 (WeChatMiniProgramBridge & IncidentFeedbackManager)
4. 3~5 户入户试点月度误报统计与达标考核报表 (PilotHouseholdManager)
"""

import math
import tempfile
import unittest

from src.safehalo.targeted_inspector import TargetedFallInspector, TargetedInspectionState
from src.safehalo.auto_calibrator import ContinuousAutoCalibrator
from src.safehalo.miniprogram_bridge import (
    WeChatMiniProgramBridge,
    IncidentFeedbackManager,
    IncidentStatus,
)
from src.safehalo.pilot_telemetry import PilotHouseholdManager
from src.safehalo.placement_adapter import PlacementAdapter
from src.safehalo.router_csi_engine import RouterCSIEngine


class TestPhase2Features(unittest.TestCase):
    def test_four_stage_challenge_and_safe_acknowledgement(self):
        """测试阶段 2 四段式闭环：进入 AWAITING_CHALLENGE 质询窗口并通过主动应答拦截误报"""
        challenges_triggered = []
        inspector = TargetedFallInspector(
            inspection_duration_seconds=4.5,
            on_trigger_challenge=lambda msg: challenges_triggered.append(msg),
        )

        t0 = 10000.0
        inspector.trigger_inspection(t0, "阶段2四段式单元测试")

        # 推进到 3.2s (> 4.5 * 0.66 = 2.97s)，应进入 AWAITING_CHALLENGE 并触发本地声光质询回调
        for step in range(1, 35):
            ts = t0 + step * 0.1
            inspector.update_frame(ts, diff_energy=0.0012, base_diff=0.0010, subcarrier_amplitudes=[0.1] * 32)

        self.assertEqual(inspector.current_state, TargetedInspectionState.AWAITING_CHALLENGE)
        self.assertEqual(len(challenges_triggered), 1)

        # 模拟老人在质询窗口内按下安全键或语音应答
        inspector.acknowledge_safe("老人语音应答：我没事")
        verdict = inspector.update_frame(t0 + 3.6, diff_energy=0.0012, base_diff=0.0010, subcarrier_amplitudes=[0.1] * 32)

        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.state, TargetedInspectionState.VERDICT_RECOVERED)
        self.assertTrue(verdict.challenge_intercepted)
        self.assertEqual(inspector.current_state, TargetedInspectionState.STANDBY)

    def test_auto_calibrator_contamination_guard_and_multiday_ema(self):
        """测试安装后多日基线学习与开机人员走动污染拦截"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cal = ContinuousAutoCalibrator(
                storage_dir=tmpdir,
                quiet_window_min_seconds=5.0,
                ema_alpha=0.2,
            )

            # 1. 模拟开机校准时屋内有人走动 (方差 0.015 >> 0.0025)，应触发抗污染拦截
            eff_var, eff_diff, is_clean = cal.validate_initial_calibration(
                candidate_variance=0.015,
                candidate_diff=0.045,
                timestamp=1700000000.0,
            )
            self.assertFalse(is_clean)
            self.assertLess(eff_var, 0.001)
            self.assertEqual(cal.state.boot_contamination_rejected, 1)

            # 2. 模拟干净的校准并连续观测跨天静默窗口
            cal.validate_initial_calibration(0.00010, 0.0010, timestamp=1700000100.0)
            for i in range(15):
                res = cal.observe_frame(
                    is_empty_or_quiet=True,
                    mean_variance=0.00020,
                    diff_energy=0.0020,
                    timestamp=1700086500.0 + i * 0.5,  # 次日时间戳
                )
            summary = cal.get_status_summary()
            self.assertGreaterEqual(summary["learning_days"], 2)
            self.assertGreaterEqual(summary["quiet_windows_learned"], 1)
            self.assertGreater(summary["base_variance"], 0.00010)

    def test_miniprogram_bridge_and_category_feedback_tuning(self):
        """测试微信小程序订阅消息载荷构建与按误报分类靶向调优参数"""
        with tempfile.TemporaryDirectory() as tmpdir:
            mp = WeChatMiniProgramBridge(template_id="TPL_FALL_001")
            mgr = IncidentFeedbackManager(storage_dir=tmpdir, default_household_id="H02", miniprogram_bridge=mp)
            adapter = PlacementAdapter(config_dir=tmpdir)
            engine = RouterCSIEngine()

            # 1. 记录一条告警事件并验证微信订阅消息格式
            inc = mgr.record_incident(
                room_name="客厅",
                event_type="FALL_ALARM",
                confidence=0.95,
                respiration_bpm=17.0,
                details="阶段2测试告警",
            )
            wx_payload = mp.build_subscribe_payload("openid_family_01", inc)
            self.assertEqual(wx_payload["touser"], "openid_family_01")
            self.assertIn(inc.event_id, wx_payload["page"])

            # 2. 家属在小程序上标记该事件为「宠物误报 (pet)」，验证宠物 SPR 阈值自动提升
            old_pet_spr = adapter.current_tuned_params.pet_spr_threshold
            ok, msg, updated_inc = mgr.submit_feedback(
                event_id=inc.event_id,
                is_false_alarm=True,
                category="pet",
                notes="家里柯基跳下沙发",
                placement_adapter=adapter,
                csi_engine=engine,
            )
            self.assertTrue(ok)
            self.assertEqual(updated_inc.status, IncidentStatus.FALSE_ALARM)
            self.assertGreater(adapter.current_tuned_params.pet_spr_threshold, old_pet_spr)
            self.assertEqual(
                engine.detector.pet_filter.subcarrier_spr_threshold,
                adapter.current_tuned_params.pet_spr_threshold,
            )

    def test_pilot_household_monthly_false_alarm_telemetry(self):
        """测试 3~5 户入户试点每户每月误报统计与达标判定"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pilot = PilotHouseholdManager(storage_dir=tmpdir, active_household_id="H01")
            ts_nov = 1794272000.0  # 2026-11

            # H01 发生 1 次误报、2 次质询拦截、1 次真实告警 (<=2 次误报，达标)
            pilot.record_false_alarm("H01", category="normal_sit_lie", timestamp=ts_nov)
            pilot.record_challenge_intercept("H01", timestamp=ts_nov)
            pilot.record_challenge_intercept("H01", timestamp=ts_nov)
            pilot.record_true_alarm("H01", timestamp=ts_nov)

            # H02 发生 2 次宠物误报 (<=2 次误报，达标)
            pilot.record_false_alarm("H02", category="pet", timestamp=ts_nov)
            pilot.record_false_alarm("H02", category="pet", timestamp=ts_nov)

            dash = pilot.get_pilot_dashboard("2026-11")
            self.assertEqual(dash["pilot_households_count"], 5)
            self.assertEqual(dash["total_false_alarms"], 3)
            self.assertEqual(dash["total_challenge_intercepts"], 2)
            self.assertTrue(dash["pilot_overall_passed"])

            md_report = pilot.export_markdown_report("2026-11")
            self.assertIn("H01", md_report)
            self.assertIn("达标", md_report)


if __name__ == "__main__":
    unittest.main()

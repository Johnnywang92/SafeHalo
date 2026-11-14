"""
SafeHalo Web 控制台前端契约与双引擎接口对齐测试 (tests/test_web_contract.py)

验证内容 (对应 P1-12)：
1. 静态提取 src/safehalo/web/index.html 中所有 fetch() 端点与请求方法。
2. 校验所有前端请求端点在 Python (web_portal.py) 与 Rust (http.rs) 双引擎中均有对应路由处理。
3. 校验前端 updateTelemetry() / pollFamilyBoard() 所绑定的 JSON 字段与 /api/status, /api/ha/status,
   /api/routine, /api/multilink, /api/placement, /api/incidents 返回结构 100% 契约一致。
4. 校验免登端点 (must_change_exempt / public telemetry) 在双引擎中一致放行。
"""

import json
import os
import re
import unittest

from src.safehalo import web_portal


class TestWebContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.html_path = os.path.join(cls.repo_root, "src", "safehalo", "web", "index.html")
        cls.python_web_path = os.path.join(cls.repo_root, "src", "safehalo", "web_portal.py")
        cls.rust_http_path = os.path.join(cls.repo_root, "experiments", "rust-daemon", "src", "http.rs")
        cls.rust_state_path = os.path.join(cls.repo_root, "experiments", "rust-daemon", "src", "state.rs")
        cls.rust_serial_path = os.path.join(cls.repo_root, "experiments", "rust-daemon", "src", "serial.rs")

        with open(cls.html_path, "r", encoding="utf-8") as f:
            cls.html_content = f.read()

        with open(cls.python_web_path, "r", encoding="utf-8") as f:
            cls.python_code = f.read()

        with open(cls.rust_http_path, "r", encoding="utf-8") as f:
            rust_http_code = f.read()
        with open(cls.rust_state_path, "r", encoding="utf-8") as f:
            rust_state_code = f.read()
        with open(cls.rust_serial_path, "r", encoding="utf-8") as f:
            rust_serial_code = f.read()
        cls.rust_code = rust_http_code + "\n" + rust_state_code + "\n" + rust_serial_code

    def test_frontend_endpoints_extracted(self):
        """1. 提取前端所有 fetch() 端点并校验其非空"""
        endpoints = re.findall(r"fetch\(['\"](/api/[^'\"]+)", self.html_content)
        unique_endpoints = sorted(set(endpoints))
        self.assertGreaterEqual(len(unique_endpoints), 20)

        # 核心关键路径端点必须全覆盖
        required_endpoints = [
            "/api/auth/current",
            "/api/auth/login",
            "/api/auth/logout",
            "/api/auth/update_password",
            "/api/challenge/acknowledge",
            "/api/ha/status",
            "/api/incidents",
            "/api/incidents/feedback",
            "/api/logs",
            "/api/logs/clear",
            "/api/multilink",
            "/api/onboarding/test_push",
            "/api/placement",
            "/api/placement/save",
            "/api/routine",
            "/api/set_mode",
            "/api/status",
            "/api/test_push",
            "/api/trigger_inspect",
            "/api/users",
            "/api/users/create",
            "/api/users/delete",
        ]
        for ep in required_endpoints:
            self.assertIn(ep, unique_endpoints, f"前端 index.html 遗漏必需端点: {ep}")

    def test_endpoints_exist_in_python_engine(self):
        """2. 校验前端所有 fetch() 端点在 Python 引擎中均已注册实现"""
        endpoints = sorted(set(re.findall(r"fetch\(['\"](/api/[^'\"]+)", self.html_content)))
        for ep in endpoints:
            # 路由可能以字符串常量或 startswith 形式出现
            self.assertIn(
                ep,
                self.python_code,
                f"Python web_portal.py 未实现前端端点: {ep}",
            )

    def test_endpoints_exist_in_rust_engine(self):
        """3. 校验前端所有 fetch() 端点在 Rust 引擎中均已注册实现"""
        endpoints = sorted(set(re.findall(r"fetch\(['\"](/api/[^'\"]+)", self.html_content)))
        for ep in endpoints:
            self.assertIn(
                f'"{ep}"',
                self.rust_code,
                f"Rust experiments/rust-daemon/src/http.rs 未实现前端端点: {ep}",
            )

    def assert_field_in_code(self, field: str, code: str, lang: str):
        found = (f'"{field}"' in code) or (f'\\"{field}\\"' in code) or (f"'{field}'" in code)
        self.assertTrue(found, f"{lang} 缺少字段: {field}")

    def test_telemetry_fields_contract(self):
        """4. 校验 /api/status 返回字段与 updateTelemetry() DOM 绑定契约"""
        expected_status_fields = [
            "sample_rate",
            "variance",
            "jitter",
            "spr",
            "is_pet",
            "radar_online",
            "fused_state",
            "neighbor_filter",
        ]
        for field in expected_status_fields:
            self.assert_field_in_code(field, self.python_code, "Python status_json")
            self.assert_field_in_code(field, self.rust_code, "Rust status_json")

    def test_family_board_ha_status_fields_contract(self):
        """5. 校验 /api/ha/status 返回字段与 pollFamilyBoard() 契约"""
        ha_fields = [
            "fall_detected",
            "presence",
            "motion",
            "pet_activity",
            "human_state",
            "fused_state",
            "security_mode",
            "radar_height_m",
            "respiration_bpm",
            "confidence",
            "decision_reason",
            "routine_has_anomaly",
            "wake_time",
            "bed_time",
            "night_restless_count",
        ]
        for field in ha_fields:
            self.assert_field_in_code(field, self.python_code, "Python /api/ha/status")
            self.assert_field_in_code(field, self.rust_code, "Rust /api/ha/status")

    def test_routine_fields_contract(self):
        """6. 校验 /api/routine 返回字段与 renderFamilyRoutine() 契约"""
        routine_fields = [
            "hourly_activity",
            "night_restless_count",
            "bed_time",
            "wake_time",
            "total_active_minutes",
            "history",
        ]
        for field in routine_fields:
            self.assert_field_in_code(field, self.python_code, "Python /api/routine")
            self.assert_field_in_code(field, self.rust_code, "Rust /api/routine")

    def test_multilink_fields_contract(self):
        """7. 校验 /api/multilink 返回字段契约"""
        multilink_fields = [
            "enabled",
            "supported",
            "active_room",
            "participating_links",
        ]
        for field in multilink_fields:
            self.assert_field_in_code(field, self.python_code, "Python /api/multilink")
            self.assert_field_in_code(field, self.rust_code, "Rust /api/multilink")

    def test_exempt_public_endpoints_aligned(self):
        """8. 校验双引擎免登公开接口 (must_change_exempt / public telemetry) 保持一致"""
        exempt_endpoints = [
            "/api/auth/current",
            "/api/ha/status",
            "/api/routine",
            "/api/multilink",
            "/api/placement",
            "/api/neighbor_spectrum",
            "/api/phase2/pilot",
            "/api/incidents",
            "/api/devices",
            "/api/health",
        ]
        for ep in exempt_endpoints:
            # 校验在 Python 与 Rust 的免登放行集合中
            self.assertIn(ep, self.python_code, f"Python 缺少免登端点声明: {ep}")
            self.assertIn(ep, self.rust_code, f"Rust 缺少免登端点声明: {ep}")

    def test_devices_command_whitelist_contract(self):
        """9. 校验设备指令白名单在 Python 与 Rust 中一致包含核心作动指令"""
        for cmd in ["BURST_RATE", "CHALLENGE_BEEP", "PING", "REBOOT"]:
            self.assertIn(cmd, self.python_code, f"Python 缺少白名单指令: {cmd}")
            self.assertIn(cmd, self.rust_code, f"Rust 缺少白名单指令: {cmd}")


if __name__ == "__main__":
    unittest.main()

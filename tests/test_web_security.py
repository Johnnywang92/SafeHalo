"""
SafeHalo Web 控制台安全加固回归测试 (tests/test_web_security.py)
镜像 Rust 端 verify/service_check.py::test_security 的同等用例，覆盖 P0-4：
1. 匿名访问受保护接口被拒、公开遥测接口仍可访问
2. 首次登录强制改密闭环（must_change_password 闸门 + 改密后旧会话失效 + 旧密码失效）
3. 密码策略（>=8 位、禁用默认 admin123、新旧密码不得相同）
4. 免登接口分策略（向导未配置免登 / 配置后需管理员；事件反馈需登录态）
5. 本地安全应答免登但限速 + 审计落盘
6. CSRF 来源校验（Cookie 跨站拒绝、同源放行、Bearer 豁免）
7. 登录限速（5 次/60 秒）与审计
8. 家庭成员 RBAC 边界
9. HTTPS 部署 Secure Cookie 标记

数据安全：全部写入重定向到临时目录，绝不触碰真实 data/users.json 与 logs/。
"""

import http.client
import json
import logging.handlers
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from src.safehalo import web_portal
from src.safehalo.auth_manager import auth_manager
from src.safehalo.logger import logger
from src.safehalo.placement_adapter import PlacementAdapter, RoomPlacementConfig
from src.safehalo.pilot_telemetry import PilotHouseholdManager
from src.safehalo.miniprogram_bridge import IncidentFeedbackManager
from src.safehalo.web_portal import SafeHaloWebHandler, _RateLimiter

STRONG_PWD = "Admin@2026x"


class TestWebSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="safehalo-web-sec-")
        cls.log_dir = os.path.join(cls.tmp, "logs")
        os.makedirs(cls.log_dir, exist_ok=True)

        # 1. 就地重定向共享 logger 单例（其他模块持有同一引用），避免写真实 logs/
        cls._orig_logger_paths = (logger.log_dir, logger.log_file, logger.events_file)
        logger.log_dir = cls.log_dir
        logger.log_file = os.path.join(cls.log_dir, "safehalo.log")
        logger.events_file = os.path.join(cls.log_dir, "events.jsonl")
        logger.recent_events.clear()
        for h in list(logger.logger.handlers):
            if isinstance(h, logging.handlers.RotatingFileHandler):
                h.close()
                h.baseFilename = os.path.abspath(logger.log_file)
                h.stream = h._open()

        # 2. 就地重定向共享 auth_manager 单例，避免写真实 data/users.json
        cls._orig_users_file = auth_manager.data_file
        auth_manager.data_file = os.path.join(cls.tmp, "users.json")

        # 3. 启动一次性 HTTP 服务（随机空闲端口，仅本机）
        try:
            cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SafeHaloWebHandler)
            cls.server.allow_reuse_address = True
            cls.port = cls.server.server_address[1]
            cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
            cls.thread.start()

            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(("127.0.0.1", cls.port))
            cls.loopback_ok = True
        except Exception:
            cls.loopback_ok = False

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()
        if hasattr(cls, "thread"):
            cls.thread.join(timeout=5)
        # 还原共享单例
        logger.log_dir, logger.log_file, logger.events_file = cls._orig_logger_paths
        for h in list(logger.logger.handlers):
            if isinstance(h, logging.handlers.RotatingFileHandler):
                h.close()
                h.baseFilename = os.path.abspath(logger.log_file)
                h.stream = h._open()
        auth_manager.data_file = cls._orig_users_file
        auth_manager.active_sessions.clear()
        SafeHaloWebHandler.cookie_secure = False
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if not getattr(self, "loopback_ok", False):
            self.skipTest("Loopback TCP connection not permitted in current sandbox environment")
        # 每个用例重置：全新 admin/admin123（强制改密）、清空会话与限速器、未配置向导
        if os.path.exists(auth_manager.data_file):
            os.remove(auth_manager.data_file)
        auth_manager.active_sessions.clear()
        auth_manager._load_or_init_users()
        web_portal._rate_limiter = _RateLimiter()
        web_portal.placement_adapter = PlacementAdapter(config_dir=self.log_dir)
        web_portal.pilot_manager = PilotHouseholdManager(storage_dir=self.log_dir)
        web_portal.incident_manager = IncidentFeedbackManager(storage_dir=self.log_dir)
        SafeHaloWebHandler.cookie_secure = False

    # ---- 请求工具 ----
    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="ignore")
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {}
        conn.close()
        return resp.status, parsed, resp_headers

    def login(self, username, password):
        return self.req("POST", "/api/auth/login", {"username": username, "password": password})

    def bearer(self, token):
        return {"Authorization": "Bearer " + token}

    def admin_token(self):
        """完成强制改密闭环，返回可用的管理员 token。"""
        status, body, _ = self.login("admin", "admin123")
        self.assertEqual(status, 200)
        token = body["token"]
        status, body, _ = self.req("POST", "/api/auth/update_password",
                                   {"old_password": "admin123", "new_password": STRONG_PWD},
                                   headers=self.bearer(token))
        self.assertEqual(status, 200, body)
        status, body, _ = self.login("admin", STRONG_PWD)
        self.assertEqual(status, 200)
        self.assertFalse(body["user"]["must_change_password"])
        return body["token"]

    def read_events(self):
        if not os.path.exists(logger.events_file):
            return []
        with open(logger.events_file, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    # ---- 用例 ----
    def test_anonymous_protected_rejected_public_open(self):
        for path in ("/api/status", "/api/users", "/api/logs"):
            status, _, _ = self.req("GET", path)
            self.assertEqual(status, 401, path)
        for path in ("/api/users/create", "/api/test_push", "/api/logs/clear", "/api/set_mode"):
            status, _, _ = self.req("POST", path, {})
            self.assertEqual(status, 401, path)
        # 公开壳与遥测无需登录
        self.assertEqual(self.req("GET", "/")[0], 200)
        status, body, _ = self.req("GET", "/api/auth/current")
        self.assertEqual(status, 200)
        self.assertFalse(body["authenticated"])
        self.assertEqual(self.req("GET", "/api/ha/status")[0], 200)
        self.assertEqual(self.req("GET", "/api/placement")[0], 200)

    def test_forced_password_change_flow(self):
        status, body, _ = self.login("admin", "admin123")
        self.assertEqual(status, 200)
        self.assertTrue(body["user"]["must_change_password"])
        token = body["token"]
        self.assertGreaterEqual(len(token), 32)
        # 强制改密期间敏感操作被拦
        status, body, _ = self.req("GET", "/api/users", headers=self.bearer(token))
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "PasswordChangeRequired")
        self.assertEqual(self.req("GET", "/api/status", headers=self.bearer(token))[0], 403)
        # 公开接口与改密接口豁免
        self.assertEqual(self.req("GET", "/api/placement", headers=self.bearer(token))[0], 200)
        self.assertEqual(self.req("GET", "/api/auth/current", headers=self.bearer(token))[0], 200)
        # 改密成功
        status, body, _ = self.req("POST", "/api/auth/update_password",
                                   {"old_password": "admin123", "new_password": STRONG_PWD},
                                   headers=self.bearer(token))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["success"])
        # 旧会话失效
        self.assertEqual(self.req("GET", "/api/users", headers=self.bearer(token))[0], 401)
        # 旧密码失效、新密码可用且不再强制改密
        self.assertEqual(self.login("admin", "admin123")[0], 401)
        status, body, _ = self.login("admin", STRONG_PWD)
        self.assertEqual(status, 200)
        self.assertFalse(body["user"]["must_change_password"])
        self.assertEqual(self.req("GET", "/api/users", headers=self.bearer(body["token"]))[0], 200)

    def test_password_policy(self):
        token = self.admin_token()
        # 过短
        status, body, _ = self.req("POST", "/api/users/create",
                                   {"username": "fam1", "password": "short", "role": "family"},
                                   headers=self.bearer(token))
        self.assertEqual(status, 400)
        self.assertFalse(body["success"])
        # 默认密码禁用
        status, body, _ = self.req("POST", "/api/users/create",
                                   {"username": "fam2", "password": "admin123", "role": "family"},
                                   headers=self.bearer(token))
        self.assertEqual(status, 400)
        # 合规创建
        status, body, _ = self.req("POST", "/api/users/create",
                                   {"username": "family", "password": "secret123", "role": "family"},
                                   headers=self.bearer(token))
        self.assertEqual(status, 200, body)
        # 新旧相同
        status, body, _ = self.req("POST", "/api/auth/update_password",
                                   {"old_password": STRONG_PWD, "new_password": STRONG_PWD},
                                   headers=self.bearer(token))
        self.assertEqual(status, 400)
        # 新密码过短
        status, body, _ = self.req("POST", "/api/auth/update_password",
                                   {"old_password": STRONG_PWD, "new_password": "weak"},
                                   headers=self.bearer(token))
        self.assertEqual(status, 400)

    def test_login_free_endpoint_gating(self):
        # 未配置：向导免登保存
        status, body, _ = self.req("POST", "/api/placement/save",
                                   {"room_type": "BEDROOM", "room_name": "卧室", "care_nickname": "奶奶"})
        self.assertEqual(status, 200, body)
        self.assertTrue(web_portal.placement_adapter.current_config.is_configured)
        # 配置后：免登被拒
        self.assertEqual(self.req("POST", "/api/placement/save", {"room_name": "客厅"})[0], 401)
        self.assertEqual(self.req("POST", "/api/onboarding/test_push", {})[0], 401)
        # 事件反馈需登录态
        self.assertEqual(self.req("POST", "/api/incidents/feedback", {"event_id": "x"})[0], 401)
        # 管理员可改；家庭成员不可改
        admin = self.admin_token()
        self.assertEqual(self.req("POST", "/api/placement/save",
                                  {"room_name": "客厅"}, headers=self.bearer(admin))[0], 200)
        self.req("POST", "/api/users/create",
                 {"username": "family", "password": "secret123", "role": "family"},
                 headers=self.bearer(admin))
        fam = self.login("family", "secret123")[1]["token"]
        self.assertEqual(self.req("POST", "/api/placement/save",
                                  {"room_name": "书房"}, headers=self.bearer(fam))[0], 403)
        # 登录态可提交事件反馈
        self.assertEqual(self.req("POST", "/api/incidents/feedback",
                                  {"event_id": "x", "is_false_alarm": True},
                                  headers=self.bearer(fam))[0], 200)

    def test_challenge_ack_rate_limit_and_audit(self):
        status, body, _ = self.req("POST", "/api/challenge/acknowledge", {"source": "语音应答"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["success"])
        # 5 秒内第二次被限速
        self.assertEqual(self.req("POST", "/api/challenge/acknowledge", {})[0], 429)
        types = [e["type"] for e in self.read_events()]
        self.assertIn("CHALLENGE_ACK", types)

    def test_csrf_origin_check(self):
        token = self.admin_token()
        cookie = {"Cookie": "safehalo_session=" + token}
        same_origin = "http://127.0.0.1:%d" % self.port
        # Cookie + 跨站 Origin → 拒绝
        status, body, _ = self.req("POST", "/api/trigger_inspect", {},
                                   headers={**cookie, "Origin": "http://evil.example"})
        self.assertEqual(status, 403, body)
        # Cookie + 同源 → 放行
        self.assertEqual(self.req("POST", "/api/trigger_inspect", {},
                                  headers={**cookie, "Origin": same_origin})[0], 200)
        # Bearer 豁免 CSRF（即便带跨站 Origin）
        self.assertEqual(self.req("POST", "/api/trigger_inspect", {},
                                  headers={**self.bearer(token), "Origin": "http://evil.example"})[0], 200)

    def test_login_rate_limit_and_audit(self):
        for _ in range(5):
            status, _, _ = self.login("admin", "wrong-password")
            self.assertEqual(status, 401)
        status, body, headers = self.login("admin", "wrong-password")
        self.assertEqual(status, 429, body)
        self.assertEqual(headers.get("retry-after"), "60")
        types = [e["type"] for e in self.read_events()]
        self.assertIn("AUTH", types)

    def test_family_rbac_boundary(self):
        admin = self.admin_token()
        self.req("POST", "/api/users/create",
                 {"username": "family", "password": "secret123", "role": "family"},
                 headers=self.bearer(admin))
        fam = self.login("family", "secret123")[1]["token"]
        # 家庭成员可读取实时状态
        self.assertEqual(self.req("GET", "/api/status", headers=self.bearer(fam))[0], 200)
        # 但不可管理成员 / 测试推送 / 清日志 / 切模式
        self.assertEqual(self.req("GET", "/api/users", headers=self.bearer(fam))[0], 403)
        for path in ("/api/test_push", "/api/logs/clear", "/api/set_mode", "/api/users/create"):
            self.assertEqual(self.req("POST", path, {}, headers=self.bearer(fam))[0], 403, path)

    def test_actuation_endpoints_require_successful_callback(self):
        token = self.admin_token()
        headers = self.bearer(token)
        original_device = SafeHaloWebHandler.on_device_command
        original_sensor = SafeHaloWebHandler.on_sensor_actuate
        self.addCleanup(setattr, SafeHaloWebHandler, "on_device_command", original_device)
        self.addCleanup(setattr, SafeHaloWebHandler, "on_sensor_actuate", original_sensor)

        cases = (
            ("/api/devices/command", "on_device_command", {"command": "BURST_RATE:100"}),
            ("/api/sensors/actuate", "on_sensor_actuate", {"device_id": "speaker_living", "command": "CHALLENGE_BEEP:1"}),
        )
        for path, attr, payload in cases:
            status, body, _ = self.req("POST", path, {**payload, "command": "rm -rf /"}, headers=headers)
            self.assertEqual(status, 400, path)
            self.assertFalse(body["success"])

            setattr(SafeHaloWebHandler, attr, None)
            status, body, _ = self.req("POST", path, payload, headers=headers)
            self.assertEqual(status, 503, path)
            self.assertFalse(body["success"])

            setattr(SafeHaloWebHandler, attr, lambda *_: False)
            status, body, _ = self.req("POST", path, payload, headers=headers)
            self.assertEqual(status, 503, path)
            self.assertFalse(body["success"])

            def fail(*_):
                raise OSError("serial disconnected")

            setattr(SafeHaloWebHandler, attr, fail)
            status, body, _ = self.req("POST", path, payload, headers=headers)
            self.assertEqual(status, 503, path)
            self.assertFalse(body["success"])

            setattr(SafeHaloWebHandler, attr, lambda *_: True)
            status, body, _ = self.req("POST", path, payload, headers=headers)
            self.assertEqual(status, 200, path)
            self.assertTrue(body["success"])
            self.assertEqual(body["status"], "sent")

    def test_actuation_callbacks_report_serial_write_result(self):
        import io
        from safehalo_service import SafeHaloSupervisor

        supervisor = SafeHaloSupervisor.__new__(SafeHaloSupervisor)
        old_rate = web_portal.shared_system_state.get("sample_rate")
        self.addCleanup(web_portal.shared_system_state.__setitem__, "sample_rate", old_rate)

        supervisor._active_csi_serial = None
        self.assertFalse(supervisor._on_request_burst_rate(100))
        self.assertFalse(supervisor._on_web_device_command("csi_probe_0", "BURST_RATE:100"))
        self.assertFalse(supervisor._on_speaker_actuate("speaker_living", "CHALLENGE_BEEP:1"))

        serial = io.BytesIO()
        supervisor._active_csi_serial = serial
        self.assertTrue(supervisor._on_request_burst_rate(100))
        self.assertEqual(serial.getvalue(), b"BURST_RATE:100\n")

        serial.seek(0)
        serial.truncate(0)
        self.assertTrue(supervisor._on_speaker_actuate("speaker_living", "CHALLENGE_BEEP:1"))
        self.assertEqual(serial.getvalue(), b"CHALLENGE_BEEP:1\n")

        class BrokenSerial:
            def write(self, _):
                raise OSError("serial disconnected")

        supervisor._active_csi_serial = BrokenSerial()
        old_rate = web_portal.shared_system_state["sample_rate"]
        self.assertFalse(supervisor._on_request_burst_rate(50))
        self.assertEqual(web_portal.shared_system_state["sample_rate"], old_rate)
        self.assertFalse(supervisor._on_web_device_command("csi_probe_0", "STATUS"))
        self.assertFalse(supervisor._on_speaker_actuate("speaker_living", "CHALLENGE_BEEP:1"))

    def test_secure_cookie_flag(self):
        SafeHaloWebHandler.cookie_secure = True
        status, _, headers = self.login("admin", "admin123")
        self.assertEqual(status, 200)
        self.assertIn("; Secure", headers.get("set-cookie", ""))
        SafeHaloWebHandler.cookie_secure = False
        status, _, headers = self.login("admin", "admin123")
        self.assertEqual(status, 200)
        self.assertNotIn("; Secure", headers.get("set-cookie", ""))


if __name__ == "__main__":
    unittest.main()
"""
SafeHalo Web 控制台与配置服务器 (web_portal.py)
基于 Python 原生 http.server 开发，零第三方依赖，可无缝运行于 OpenWrt 路由器与本地电脑。
提供:
1. 实时状态与遥测数据可视化
2. 摆放与安装规范、支持路由器型号文档查询
3. 微信手机告警 Token 配置与一键测试推送
"""

import http.server
import json
import os
import socketserver
import time
from datetime import datetime
from typing import Dict, List, Optional
from .notifier import AlertNotifier
from .logger import logger
from .auth_manager import auth_manager

# 全局共享状态字典 (供算法后台线程与 Web 前端交互)
shared_system_state = {
    "state": "EMPTY",
    "sample_rate": 30.0,
    "variance": 0.00012,
    "jitter": 8.5,
    "spr": 0.18,
    "is_pet": False,
    "security_mode": "FALL_GUARD",
    "radar_online": False,
    "radar_protocol": "None",
    "radar_z": 0.0,
    "radar_bpm": 0.0,
    "fused_state": "CALIBRATING",
    "fused_confidence": 1.0,
    "fused_reason": "单模 Wi-Fi CSI 运行中",
    "recent_events": [
        {"time": datetime.now().strftime("%H:%M:%S"), "text": "SafeHalo 系统启动，完成基线自适应校准"}
    ]
}

def log_event(text: str):
    logger.log_event("WEB_PORTAL", text, level="INFO")

class SafeHaloWebHandler(http.server.SimpleHTTPRequestHandler):
    HTML_PATH = os.path.join(os.path.dirname(__file__), "web", "index.html")

    def _get_session(self) -> Optional[Dict]:
        """从 Authorization 标头或 Cookie 中提取并验证会话"""
        # 1. 尝试 Bearer Token
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            sess = auth_manager.validate_session(token)
            if sess:
                return sess

        # 2. 尝试 Cookie
        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            for part in cookie_header.split(";"):
                part = part.strip()
                if part.startswith("safehalo_session="):
                    token = part[len("safehalo_session="):].strip()
                    sess = auth_manager.validate_session(token)
                    if sess:
                        return sess
        return None

    def _send_json(self, status_code: int, data: Dict, extra_headers: Optional[Dict[str, str]] = None):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        # 静态首页 (无需登录先展示壳，前端根据认证状态自动弹出登录窗)
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if os.path.exists(self.HTML_PATH):
                with open(self.HTML_PATH, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b"<h1>SafeHalo Index.html not found</h1>")
            return

        # 1. 认证状态检查接口 (公开)
        if self.path == "/api/auth/current":
            sess = self._get_session()
            if sess:
                self._send_json(200, {"authenticated": True, "user": sess})
            else:
                self._send_json(200, {"authenticated": False, "user": None})
            return

        # --- 以下接口均需要登录 ---
        sess = self._get_session()
        if not sess:
            self._send_json(401, {"error": "Unauthorized", "message": "未登录或登录已过期，请重新登录"})
            return

        # 2. 获取用户列表 (仅管理员权限)
        if self.path == "/api/users":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可查看成员列表"})
                return
            self._send_json(200, {"users": auth_manager.list_users()})
            return

        # 3. 获取实时状态与遥测
        if self.path == "/api/status":
            events = logger.get_recent_events(limit=15)
            if events:
                shared_system_state["recent_events"] = [{"time": e["time_short"], "text": e["message"]} for e in events]
            self._send_json(200, shared_system_state)
            return

        # 4. 获取日志
        if self.path.startswith("/api/logs"):
            if self.path == "/api/logs/download":
                if os.path.exists(logger.log_file):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="safehalo.log"')
                    self.end_headers()
                    with open(logger.log_file, "rb") as f:
                        self.wfile.write(f.read())
                else:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"No log file found.")
                return

            payload = {
                "events": logger.get_recent_events(limit=80),
                "tail": logger.get_log_tail(lines=80),
                "log_file_size": os.path.getsize(logger.log_file) if os.path.exists(logger.log_file) else 0
            }
            self._send_json(200, payload)
            return

        # 默认 404
        self.send_error(404, "Not Found")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)
        data = {}
        try:
            data = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            pass

        client_ip = self.client_address[0] if self.client_address else "未知"

        # 1. 用户登录接口 (公开)
        if self.path == "/api/auth/login":
            username = data.get("username", "").strip()
            password = data.get("password", "")
            ok, token_or_msg, user_info = auth_manager.login(username, password, client_ip=client_ip)
            if ok:
                # 写入安全 Cookie (HttpOnly, SameSite=Lax, 保留 7 天)
                cookie_str = f"safehalo_session={token_or_msg}; Path=/; HttpOnly; SameSite=Lax; Max-Age={7*24*3600}"
                self._send_json(200, {
                    "success": True,
                    "token": token_or_msg,
                    "user": user_info,
                    "message": "登录成功"
                }, extra_headers={"Set-Cookie": cookie_str})
            else:
                self._send_json(401, {"success": False, "message": token_or_msg})
            return

        # --- 以下 POST 操作均需要有效登录态 ---
        sess = self._get_session()
        if not sess:
            self._send_json(401, {"error": "Unauthorized", "message": "未登录或登录已过期，请重新登录"})
            return

        # 2. 用户登出
        if self.path == "/api/auth/logout":
            cookie_header = self.headers.get("Cookie", "")
            for part in cookie_header.split(";"):
                part = part.strip()
                if part.startswith("safehalo_session="):
                    auth_manager.logout(part[len("safehalo_session="):].strip())
            # 清除 Cookie
            clear_cookie = "safehalo_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            self._send_json(200, {"success": True, "message": "已安全退出登录"}, extra_headers={"Set-Cookie": clear_cookie})
            return

        # 3. 用户修改自身密码
        if self.path == "/api/auth/update_password":
            old_pwd = data.get("old_password", "")
            new_pwd = data.get("new_password", "")
            ok, msg = auth_manager.update_password(sess["username"], old_pwd, new_pwd)
            self._send_json(200 if ok else 400, {"success": ok, "message": msg})
            return

        # 4. 创建新家庭成员 (仅管理员权限)
        if self.path == "/api/users/create":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可添加成员"})
                return
            new_user = data.get("username", "")
            new_pwd = data.get("password", "")
            new_role = data.get("role", "family")
            new_name = data.get("display_name", "")
            ok, msg = auth_manager.create_user(new_user, new_pwd, role=new_role, display_name=new_name)
            self._send_json(200 if ok else 400, {"success": ok, "message": msg})
            return

        # 5. 删除家庭成员 (仅管理员权限)
        if self.path == "/api/users/delete":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可删除成员"})
                return
            del_user = data.get("username", "")
            ok, msg = auth_manager.delete_user(del_user, operator_username=sess["username"])
            self._send_json(200 if ok else 400, {"success": ok, "message": msg})
            return

        # 6. 发送手机测试告警 (仅管理员权限)
        if self.path == "/api/test_push":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可触发测试推送"})
                return
            token = data.get("pushplus_token", "").strip()
            notifier = AlertNotifier(pushplus_token=token if token else None)
            success = notifier.send_fall_alarm(room_name="家庭测试区域", details=f"管理员 [{sess['username']}] 点击控制台【发送测试告警】")
            log_event(f"用户 [{sess['username']}] 触发手机测试告警推送 (结果: {'成功' if success else '模拟发送'})")
            self._send_json(200, {"success": success, "message": "测试告警已发送，请检查微信通知！"})
            return

        # 7. 手动触发定向排查 (所有登录成员均可关心老人)
        if self.path == "/api/trigger_inspect":
            log_event(f"成员 [{sess['username']}] 手动请求【启动定向深度排查】(激振提频至 100Hz)")
            shared_system_state["state"] = "FALL_SUSPECT"
            shared_system_state["sample_rate"] = 100.0
            self._send_json(200, {
                "success": True,
                "message": "已向射频节点下发指令：提频至 100Hz，正在定向聚焦地面生命呼吸体征..."
            })
            return

        # 8. 切换安防模式 (仅管理员权限)
        if self.path == "/api/set_mode":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可修改安防守护模式"})
                return
            mode = data.get("mode", "FALL_GUARD")
            shared_system_state["security_mode"] = mode
            log_event(f"安防守护模式已由管理员 [{sess['username']}] 切换为: {mode}")
            self._send_json(200, {"success": True, "message": f"模式已切换为 {mode}"})
            return

        # 9. 清空系统与审计日志 (仅管理员权限)
        if self.path == "/api/logs/clear":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可清空系统日志"})
                return
            logger.clear_logs()
            logger.info(f"管理员 [{sess['username']}] 清空了系统与事件审计日志")
            self._send_json(200, {"success": True, "message": "系统与事件日志已成功清空"})
            return

        self.send_error(404, "Not Found")

def start_portal_server(port: int = 8080, host: str = "0.0.0.0"):
    """
    启动 SafeHalo Web 配置与远程管理服务
    :param port: 监听端口 (默认 8080)
    :param host: 监听地址 (默认 0.0.0.0，允许局域网与远程穿透访问)
    """
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((host, port), SafeHaloWebHandler) as httpd:
        print(f"\n{'='*65}")
        print(f"🌐 SafeHalo Web 远程管理控制台已启动: http://0.0.0.0:{port}")
        print(f"本地局域网访问: http://localhost:{port} 或 http://<路由器LAN_IP>:{port}")
        print(f"远程公网访问: 支持 Cloudflare Tunnel / Tailscale / FRP 穿透")
        print(f"默认管理员账号: admin / admin123")
        print(f"{'='*65}\n")
        httpd.serve_forever()

if __name__ == "__main__":
    start_portal_server(8080)

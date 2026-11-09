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

# 全局共享状态字典 (供算法后台线程与 Web 前端交互)
shared_system_state = {
    "state": "EMPTY",
    "sample_rate": 30.0,
    "variance": 0.00012,
    "jitter": 8.5,
    "spr": 0.18,
    "is_pet": False,
    "security_mode": "FALL_GUARD",
    "recent_events": [
        {"time": datetime.now().strftime("%H:%M:%S"), "text": "SafeHalo 系统启动，完成基线自适应校准"}
    ]
}

def log_event(text: str):
    logger.log_event("WEB_PORTAL", text, level="INFO")

class SafeHaloWebHandler(http.server.SimpleHTTPRequestHandler):
    HTML_PATH = os.path.join(os.path.dirname(__file__), "web", "index.html")

    def do_GET(self):
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

        if self.path == "/api/status":
            events = logger.get_recent_events(limit=15)
            if events:
                shared_system_state["recent_events"] = [{"time": e["time_short"], "text": e["message"]} for e in events]
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(shared_system_state).encode("utf-8"))
            return

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

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            payload = {
                "events": logger.get_recent_events(limit=80),
                "tail": logger.get_log_tail(lines=80),
                "log_file_size": os.path.getsize(logger.log_file) if os.path.exists(logger.log_file) else 0
            }
            self.wfile.write(json.dumps(payload).encode("utf-8"))
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

        if self.path == "/api/test_push":
            token = data.get("pushplus_token", "").strip()
            notifier = AlertNotifier(pushplus_token=token if token else None)
            success = notifier.send_fall_alarm(room_name="家庭测试区域", details="用户点击控制台【发送测试告警】")
            log_event(f"已触发手机测试告警推送 (结果: {'成功' if success else '模拟发送'})")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"success": success, "message": "测试告警已发送，请检查微信通知！"}).encode("utf-8"))
            return

        if self.path == "/api/trigger_inspect":
            log_event("用户手动请求【启动定向深度排查】(激振提频至 100Hz)")
            shared_system_state["state"] = "FALL_SUSPECT"
            shared_system_state["sample_rate"] = 100.0
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "message": "已向射频节点下发指令：提频至 100Hz，正在定向聚焦地面生命呼吸体征..."
            }).encode("utf-8"))
            return

        if self.path == "/api/set_mode":
            mode = data.get("mode", "FALL_GUARD")
            shared_system_state["security_mode"] = mode
            log_event(f"安防守护模式已切换为: {mode}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        if self.path == "/api/logs/clear":
            logger.clear_logs()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "message": "系统与事件日志已成功清空"}).encode("utf-8"))
            return

        self.send_error(404, "Not Found")

def start_portal_server(port: int = 8080):
    """启动本地 Web 配置服务"""
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", port), SafeHaloWebHandler) as httpd:
        print(f"\n{'='*60}")
        print(f"🌐 SafeHalo 配置与监控网页门户已启动: http://localhost:{port}")
        print(f"如果部署在路由器上，请通过手机/电脑浏览器访问: http://192.168.1.1:{port}")
        print(f"{'='*60}\n")
        httpd.serve_forever()

if __name__ == "__main__":
    start_portal_server(8080)

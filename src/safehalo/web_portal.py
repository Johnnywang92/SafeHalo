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
import secrets
import socketserver
import time
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional
from .notifier import AlertNotifier
from .logger import logger
from .auth_manager import auth_manager
from .placement_adapter import PlacementAdapter, RoomPlacementConfig, TunedParameters
from .pilot_telemetry import PilotHouseholdManager
from .miniprogram_bridge import IncidentFeedbackManager, WeChatMiniProgramBridge
from .sensor_fusion_ecosystem import SensorFusionEcosystem, SensorType, LinkageAction

MAX_POST_BODY_BYTES = 65536


def _is_command_allowed(cmd: str) -> bool:
    if cmd.startswith("BURST_RATE:"):
        return cmd[11:] in ("20", "30", "50", "100")
    if cmd.startswith("CHALLENGE_BEEP:"):
        return cmd[15:] in ("0", "1")
    if cmd.startswith("CHALLENGE_VOICE:") or cmd.startswith("PLAY_TTS:"):
        return True
    return cmd in ("PING", "REBOOT", "STATUS", "ALARM_SIREN")

# 全局摆放自适应协调器与阶段 2 试点/事件管理实例
placement_adapter = PlacementAdapter()
pilot_manager = PilotHouseholdManager()
incident_manager = IncidentFeedbackManager()

def _on_eco_fall_confirmed(room: str, confidence: float, details: str):
    logger.log_fall_confirmed(details, bpm=0.0, confidence=confidence)
    incident_manager.record_incident(
        room_name=room,
        confidence=confidence,
        details=details,
        respiration_bpm=0.0,
    )
    shared_system_state["state"] = "FALL_CONFIRMED"

def _on_eco_challenge_dismissed(details: str):
    logger.info(f"[ECOSYSTEM] {details}")
    shared_system_state["state"] = "RESTING"

def _on_eco_security_alert(alert_type: str, details: str, meta: Dict):
    logger.log_event("SECURITY_INTRUSION", f"{alert_type}: {details}", level="ALARM")

sensor_ecosystem = SensorFusionEcosystem(
    on_fall_confirmed=_on_eco_fall_confirmed,
    on_challenge_dismissed=_on_eco_challenge_dismissed,
    on_security_alert=_on_eco_security_alert,
)

# 全局共享状态字典 (供算法后台线程与 Web 前端交互)
SERVICE_START_TIME = time.time()

shared_system_state = {
    "state": "EMPTY",
    "sample_rate": 30.0,
    "variance": 0.00012,
    "jitter": 8.5,
    "spr": 0.18,
    "is_pet": False,
    "security_mode": "FALL_GUARD",
    "security_alert": False,
    "radar_online": False,
    "radar_protocol": "None",
    "radar_z": 0.0,
    "radar_bpm": 0.0,
    "fused_state": "CALIBRATING",
    "fused_confidence": 1.0,
    "fused_reason": "单模 Wi-Fi CSI 运行中",
    "placement": {
        "room_type": placement_adapter.current_config.room_type,
        "room_name": placement_adapter.current_config.room_name,
        "mount_height": placement_adapter.current_config.mount_height,
        "is_configured": placement_adapter.current_config.is_configured,
        "description": placement_adapter.current_tuned_params.description,
    },
    "routine": {
        "wake_time": "尚未起床",
        "bed_time": "待定",
        "typical_wake": "07:30",
        "typical_bed": "22:30",
        "night_restless_count": 0,
        "total_active_minutes": 0.0,
        "hourly_activity": [0] * 24,
        "active_anomalies": [],
        "has_anomaly": False,
    },
    "neighbor_filter": {
        "channel_purity_percent": 100.0,
        "rejected_neighbor_packets": 0,
        "neighbor_nodes_detected": 0,
        "channel_advice": "当前无线信道纯净，无显著同频邻居串扰。",
    },
    "multilink": {
        "enabled": False,
        "supported": True,
        "active_room": placement_adapter.current_config.room_name,
        "participating_links": 1,
        "fused_energy": 0.0,
        "links": {},
    },
    "auto_calibration": {
        "learning_days": 1,
        "quiet_windows_learned": 0,
        "maturity_percent": 25.0,
        "boot_contamination_rejected": 0,
    },
    "pilot": pilot_manager.get_pilot_dashboard(),
    "recent_events": [
        {"time": datetime.now().strftime("%H:%M:%S"), "text": "SafeHalo 系统启动，完成基线自适应校准"}
    ]
}

def log_event(text: str):
    logger.log_event("WEB_PORTAL", text, level="INFO")


class _RateLimiter:
    """按 key 的滑动窗口限速器（内存有上限，防 key 洪泛导致膨胀）。"""

    def __init__(self):
        self._hits: Dict[str, tuple] = {}

    def allow(self, key: str, limit: int, window_secs: float) -> bool:
        now = time.time()
        if len(self._hits) > 4096:
            self._hits = {k: v for k, v in self._hits.items() if now - v[0] < 60.0}
            if len(self._hits) > 8192:
                self._hits.clear()
        t, n = self._hits.get(key, (now, 0))
        if now - t >= window_secs:
            t, n = now, 0
        n += 1
        self._hits[key] = (t, n)
        return n <= limit

    def reset(self, key: str):
        self._hits.pop(key, None)


_rate_limiter = _RateLimiter()


def _origin_ok(headers) -> bool:
    """校验 Origin/Referer 与 Host 一致（防御 CSRF）；无浏览器来源头的非浏览器客户端放行。"""
    host = (headers.get("Host") or "").lower()
    for name in ("Origin", "Referer"):
        v = headers.get(name)
        if not v:
            continue
        rest = v.split("://", 1)[1] if "://" in v else v
        authority = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        return bool(host) and authority.lower() == host
    return True


def _must_change_exempt(method: str, path: str) -> bool:
    """强制改密期间仍可访问的公开/改密接口。"""
    if path == "/api/auth/update_password":
        return True
    if method == "GET":
        return path in (
            "/", "/index.html", "/api/auth/current", "/api/ha/status", "/api/routine",
            "/api/neighbor_spectrum", "/api/placement", "/api/multilink",
            "/api/phase2/pilot", "/api/phase2/pilot/report", "/api/incidents", "/api/devices", "/api/health",
            "/api/sensors/status",
        )
    return path in ("/api/auth/login", "/api/auth/logout", "/api/challenge/acknowledge", "/api/sensors/event")


class SafeHaloWebHandler(http.server.BaseHTTPRequestHandler):
    HTML_PATH = os.path.join(os.path.dirname(__file__), "web", "index.html")
    timeout = 5
    routine_history_provider = None
    devices_provider = None
    on_device_command = None
    on_sensor_actuate = None
    cookie_secure = False
    api_token = os.environ.get("SAFEHALO_API_TOKEN", "")

    def _is_machine_ok(self, query: Optional[Dict[str, List[str]]] = None) -> bool:
        tok = (self.api_token or "").strip()
        if not tok:
            return False
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            candidate = auth_header[7:].strip()
            if secrets.compare_digest(candidate, tok):
                return True
        if query and "token" in query and query["token"]:
            if secrets.compare_digest(query["token"][0], tok):
                return True
        return False

    def _get_session(self, query: Optional[Dict[str, List[str]]] = None) -> Optional[Dict]:
        """从机器凭据、Authorization 标头或 Cookie 中提取并验证会话"""
        if self._is_machine_ok(query):
            return {
                "username": "api-token",
                "display_name": "Machine Token",
                "role": "machine",
                "must_change_password": False,
            }

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

    def _send_text(self, status_code: int, content_type: str, text: str):
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def do_GET(self):
        parsed_url = urllib.parse.urlsplit(self.path)
        self.raw_path = self.path
        self.path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

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
            sess = self._get_session(query)
            if sess and sess.get("role") != "machine":
                self._send_json(200, {"authenticated": True, "user": sess})
            else:
                self._send_json(200, {"authenticated": False, "user": None})
            return

        # 1.1 Home Assistant RESTful 传感器接口 (公开局域网遥测)
        if self.path == "/api/ha/status":
            is_fall = shared_system_state.get("state") == "FALL_CONFIRMED" or shared_system_state.get("fused_state") == "fall_confirmed"
            is_moving = shared_system_state.get("state") == "MOVING"
            is_presence = shared_system_state.get("state") not in ("EMPTY", "CALIBRATING")
            is_pet = bool(shared_system_state.get("is_pet", False))
            routine_data = shared_system_state.get("routine", {})

            ha_payload = {
                "fall_detected": is_fall,
                "presence": is_presence,
                "motion": is_moving,
                "pet_activity": is_pet,
                "human_state": shared_system_state.get("state", "EMPTY"),
                "fused_state": shared_system_state.get("fused_state", "CALIBRATING"),
                "security_mode": shared_system_state.get("security_mode", "FALL_GUARD"),
                "radar_height_m": shared_system_state.get("radar_z", 0.0),
                "respiration_bpm": shared_system_state.get("radar_bpm", 0.0),
                "confidence": shared_system_state.get("fused_confidence", 1.0),
                "decision_reason": shared_system_state.get("fused_reason", ""),
                "routine_has_anomaly": routine_data.get("has_anomaly", False),
                "wake_time": routine_data.get("wake_time", ""),
                "bed_time": routine_data.get("bed_time", ""),
                "night_restless_count": routine_data.get("night_restless_count", 0),
            }
            self._send_json(200, ha_payload)
            return

        # 1.2 作息画像与偏离异常分析接口 (公开)
        if self.path == "/api/routine":
            payload = dict(shared_system_state.get("routine", {}))
            provider = getattr(SafeHaloWebHandler, "routine_history_provider", None)
            payload["history"] = provider() if provider else []
            self._send_json(200, payload)
            return

        # 1.3 邻居 Wi-Fi 隔离与信道纯净度接口 (公开)
        if self.path == "/api/neighbor_spectrum":
            self._send_json(200, shared_system_state.get("neighbor_filter", {}))
            return

        # 1.4 路由器摆放位置与算法适配状态 (公开向导查询)
        if self.path == "/api/placement":
            from dataclasses import asdict
            self._send_json(200, {
                "config": asdict(placement_adapter.current_config),
                "tuned_parameters": asdict(placement_adapter.current_tuned_params),
                "is_configured": placement_adapter.current_config.is_configured,
            })
            return

        # 1.5 多链路空间分集与房间级微定位状态 (公开)
        if self.path == "/api/multilink":
            self._send_json(200, shared_system_state.get("multilink", {}))
            return

        # 1.6 阶段 2：3~5 户入户试点月度误报统计与多日自校准看板 (公开遥测)
        if self.path == "/api/phase2/pilot":
            if query.get("format", [""])[0] == "markdown":
                self._send_text(200, "text/markdown; charset=utf-8", pilot_manager.export_markdown_report())
                return
            self._send_json(200, {
                "pilot_dashboard": pilot_manager.get_pilot_dashboard(),
                "auto_calibration": shared_system_state.get("auto_calibration", {}),
                "recent_incidents": incident_manager.list_recent_incidents(limit=10),
            })
            return

        if self.path == "/api/phase2/pilot/report":
            self._send_text(200, "text/markdown; charset=utf-8", pilot_manager.export_markdown_report())
            return

        # 1.7 阶段 2：告警事件与反馈列表查询 (公开/小程序端拉取)
        if self.path == "/api/incidents":
            self._send_json(200, {
                "incidents": incident_manager.list_recent_incidents(limit=30),
            })
            return

        # 1.8 硬件感知与作动设备列表 (公开遥测)
        if self.path == "/api/devices":
            provider = getattr(SafeHaloWebHandler, "devices_provider", None)
            dev_list = provider() if provider else [
                {
                    "id": "csi_probe_0",
                    "type": "serial",
                    "role": "sensor_actuator",
                    "path": "auto",
                    "baud": 921600,
                    "status": "online",
                    "last_seen": 0.0,
                }
            ]
            self._send_json(200, {"devices": dev_list})
            return

        # 1.9 服务健康检查与运维探针 (公开遥测)
        if self.path == "/api/health":
            uptime = max(0.0, time.time() - SERVICE_START_TIME)
            self._send_json(200, {
                "status": "healthy",
                "uptime_seconds": round(uptime, 1),
                "state": shared_system_state.get("state", "EMPTY"),
                "security_mode": shared_system_state.get("security_mode", "FALL_GUARD"),
                "fused_state": shared_system_state.get("fused_state", "CALIBRATING"),
                "radar_online": shared_system_state.get("radar_online", False),
            })
            return

        # 1.10 多模态传感器生态状态与联动快照 (公开遥测)
        if self.path == "/api/sensors/status":
            self._send_json(200, sensor_ecosystem.get_status())
            return

        # --- 以下接口均需要登录 ---
        sess = self._get_session(query)
        if not sess:
            self._send_json(401, {"error": "Unauthorized", "message": "未登录或登录已过期，请重新登录"})
            return
        if sess.get("must_change_password"):
            self._send_json(403, {"error": "PasswordChangeRequired", "message": "首次登录请先修改初始密码，再进行其他操作"})
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
        parsed_url = urllib.parse.urlsplit(self.path)
        self.raw_path = self.path
        self.path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "BadRequest", "message": "Invalid Content-Length"})
            return
        if content_length < 0:
            self._send_json(400, {"error": "BadRequest", "message": "Negative Content-Length"})
            return
        if content_length > MAX_POST_BODY_BYTES:
            self._send_json(413, {"error": "PayloadTooLarge", "message": "请求体超过 64KB 限制"})
            return

        post_data = self.rfile.read(content_length) if content_length > 0 else b""
        data = {}
        try:
            data = json.loads(post_data.decode("utf-8")) if post_data else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            pass

        client_ip = self.client_address[0] if self.client_address else "未知"

        # 机器 Token 权限边界校验
        if self._is_machine_ok(query) and self.path not in (
            "/api/incidents/feedback",
            "/api/challenge/acknowledge",
            "/api/set_mode",
            "/api/sensors/event",
        ):
            self._send_json(403, {"error": "Forbidden", "message": "机器 token 仅支持状态读取、传感器上报、事件反馈/应答与模式切换"})
            return

        # CSRF 防护：仅对携带 Cookie 的非 Bearer 浏览器请求校验来源
        auth_header = self.headers.get("Authorization", "")
        if (
            not auth_header.startswith("Bearer ")
            and not self._is_machine_ok(query)
            and self.headers.get("Cookie")
            and not _origin_ok(self.headers)
        ):
            self._send_json(403, {"error": "Forbidden", "message": "跨站请求被拒绝"})
            return

        # 1. 用户登录接口 (公开，带限速与审计)
        if self.path == "/api/auth/login":
            username = data.get("username", "").strip()
            password = data.get("password", "")
            limit_key = f"login:{client_ip}"
            if not _rate_limiter.allow(limit_key, 5, 60.0):
                logger.log_event("AUTH", f"登录尝试过于频繁，已临时限制（来源 {client_ip}）", level="WARN")
                self._send_json(429, {"success": False, "message": "登录尝试过于频繁，请 60 秒后再试"},
                                extra_headers={"Retry-After": "60"})
                return
            ok, token_or_msg, user_info = auth_manager.login(username, password, client_ip=client_ip)
            if ok:
                _rate_limiter.reset(limit_key)
                logger.log_event("AUTH", f"成员 [{user_info['username']}] 登录成功（来源 {client_ip}）", level="INFO")
                secure = "; Secure" if self.cookie_secure else ""
                # 写入安全 Cookie (HttpOnly, SameSite=Lax, 保留 7 天)
                cookie_str = f"safehalo_session={token_or_msg}; Path=/; HttpOnly; SameSite=Lax; Max-Age={7*24*3600}{secure}"
                self._send_json(200, {
                    "success": True,
                    "token": token_or_msg,
                    "user": user_info,
                    "message": "登录成功"
                }, extra_headers={"Set-Cookie": cookie_str})
            else:
                logger.log_event("AUTH", f"登录失败：用户 [{username}]（来源 {client_ip}）", level="WARN")
                self._send_json(401, {"success": False, "message": token_or_msg})
            return

        # 1.1 首次安装向导 / 摆放位置保存与算法即时适配 (未完成初始化时免登，配置完成后需管理员)
        if self.path == "/api/placement/save":
            if placement_adapter.current_config.is_configured:
                sess = self._get_session()
                if not sess:
                    self._send_json(401, {"error": "Unauthorized", "message": "初始化已完成，请先登录管理员账号再修改守护方案"})
                    return
                if sess.get("must_change_password"):
                    self._send_json(403, {"error": "PasswordChangeRequired", "message": "首次登录请先修改初始密码，再进行其他操作"})
                    return
                if sess.get("role") != "admin":
                    self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可修改守护方案"})
                    return
            r_type = data.get("room_type", "LIVING_ROOM")
            r_name = data.get("room_name", "客厅")
            m_height = data.get("mount_height", "MID_DESK")
            area = float(data.get("room_area_sqm", 25.0))
            pets = bool(data.get("has_pets", False))
            water = bool(data.get("has_water_splash", False))
            care_nickname = (data.get("care_nickname") or "家里长辈").strip() or "家里长辈"
            living_arrangement = data.get("living_arrangement", "SOLO")
            mobility_pace = data.get("mobility_pace", "BRISK")
            wake_habit = data.get("wake_habit", "REGULAR")
            enable_gentle_challenge = bool(data.get("enable_gentle_challenge", True))
            pushplus_token = (data.get("pushplus_token") or "").strip()
            webhook_url = (data.get("webhook_url") or "").strip()
            bark_key = (data.get("bark_key") or "").strip()

            cfg, params = placement_adapter.save_config(
                room_type=r_type,
                room_name=r_name,
                mount_height=m_height,
                room_area_sqm=area,
                has_pets=pets,
                has_water_splash=water,
                care_nickname=care_nickname,
                living_arrangement=living_arrangement,
                mobility_pace=mobility_pace,
                wake_habit=wake_habit,
                enable_gentle_challenge=enable_gentle_challenge,
            )

            # 若用户在向导第 3 步填写了通知凭证，立即持久化保存并同步给通知引擎
            if pushplus_token or webhook_url or bark_key:
                notifier = AlertNotifier(
                    pushplus_token=pushplus_token if pushplus_token else None,
                    webhook_url=webhook_url if webhook_url else None,
                    bark_key=bark_key if bark_key else None,
                )
                notifier.update_config(
                    pushplus_token=pushplus_token if pushplus_token else None,
                    webhook_url=webhook_url if webhook_url else None,
                    bark_key=bark_key if bark_key else None,
                )
                if hasattr(SafeHaloWebHandler, "on_notifier_updated") and SafeHaloWebHandler.on_notifier_updated:
                    try:
                        SafeHaloWebHandler.on_notifier_updated(notifier)
                    except Exception as e:
                        logger.error(f"向导同步通知配置失败: {e}")

            # 更新全局遥测状态
            shared_system_state["placement"] = {
                "room_type": cfg.room_type,
                "room_name": cfg.room_name,
                "mount_height": cfg.mount_height,
                "care_nickname": cfg.care_nickname,
                "living_arrangement": cfg.living_arrangement,
                "mobility_pace": cfg.mobility_pace,
                "wake_habit": cfg.wake_habit,
                "enable_gentle_challenge": cfg.enable_gentle_challenge,
                "is_configured": cfg.is_configured,
                "description": params.description,
                "friendly_summary": params.friendly_summary,
            }

            # 触发引擎重新校准与算法更新回调
            if hasattr(SafeHaloWebHandler, "on_placement_updated") and SafeHaloWebHandler.on_placement_updated:
                try:
                    SafeHaloWebHandler.on_placement_updated(cfg, params)
                except Exception as e:
                    logger.error(f"下发摆放自适应回调失败: {e}")

            from dataclasses import asdict
            self._send_json(200, {
                "success": True,
                "message": f"已为【{cfg.care_nickname}】开启【{r_name}】安心守护方案，正在进行 5 秒静默环境自适应！",
                "config": asdict(cfg),
                "tuned_parameters": asdict(params),
            })
            return

        # 1.15 首次安装向导：发送温和的“平安测试通知”到手机 (未完成初始化时免登，配置完成后需管理员)
        if self.path == "/api/onboarding/test_push":
            if placement_adapter.current_config.is_configured:
                sess = self._get_session()
                if not sess:
                    self._send_json(401, {"error": "Unauthorized", "message": "初始化已完成，请先登录管理员账号再发送测试通知"})
                    return
                if sess.get("must_change_password"):
                    self._send_json(403, {"error": "PasswordChangeRequired", "message": "首次登录请先修改初始密码，再进行其他操作"})
                    return
                if sess.get("role") != "admin":
                    self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可发送测试通知"})
                    return
            token = (data.get("pushplus_token") or "").strip()
            webhook_url = (data.get("webhook_url") or "").strip()
            bark_key = (data.get("bark_key") or "").strip()
            nickname = (data.get("care_nickname") or placement_adapter.current_config.care_nickname or "家里长辈").strip()
            room_name = (data.get("room_name") or placement_adapter.current_config.room_name or "客厅").strip()
            notifier = AlertNotifier(
                pushplus_token=token if token else None,
                webhook_url=webhook_url if webhook_url else None,
                bark_key=bark_key if bark_key else None,
            )
            if token or webhook_url or bark_key:
                notifier.update_config(
                    pushplus_token=token if token else None,
                    webhook_url=webhook_url if webhook_url else None,
                    bark_key=bark_key if bark_key else None,
                )
                if hasattr(SafeHaloWebHandler, "on_notifier_updated") and SafeHaloWebHandler.on_notifier_updated:
                    try:
                        SafeHaloWebHandler.on_notifier_updated(notifier)
                    except Exception:
                        pass
            success = notifier.send_routine_anomaly_alert(
                title=f"【平安联结测试】{nickname}的{room_name}守护已就绪",
                description=f"这是一条来自 SafeHalo 的温和测试消息。全屋无摄像头纯无线电波守护已开启，遇到需要关心的情况时会第一时间通知您。",
                severity="INFO",
            )
            self._send_json(200, {
                "success": success,
                "message": f"平安测试消息已发送（守护对象：{nickname}），请查看手机微信或推送终端！",
            })
            return

        # 1.2 阶段 2：本地交互质询安全应答接口 (保留免登以便老人一键按键/语音网关应答，但限速并审计来源)
        if self.path == "/api/challenge/acknowledge":
            if not _rate_limiter.allow(f"ack:{client_ip}", 1, 5.0):
                self._send_json(429, {"success": False, "message": "操作过于频繁，请稍后再试"})
                return
            source = data.get("source", "本地按钮/语音安全应答")
            logger.log_event("CHALLENGE_ACK", f"收到本地安全确认应答（{source}，来源 {client_ip}），疑似跌倒警报已本地解除", level="INFO")
            if hasattr(SafeHaloWebHandler, "on_challenge_acknowledge") and SafeHaloWebHandler.on_challenge_acknowledge:
                SafeHaloWebHandler.on_challenge_acknowledge(source)
            pilot_manager.record_challenge_intercept(household_id=data.get("household_id"))
            shared_system_state["state"] = "MOVING"
            shared_system_state["pilot"] = pilot_manager.get_pilot_dashboard()
            self._send_json(200, {
                "success": True,
                "message": "已收到安全确认应答，疑似跌倒警报已本地解除，未打扰家属。",
            })
            return

        # 1.3 阶段 2：小程序端/家属端事件确认与误报反馈回调 (需登录态或机器凭据)
        if self.path == "/api/incidents/feedback":
            sess = self._get_session(query)
            if not sess:
                self._send_json(401, {"error": "Unauthorized", "message": "未登录或登录已过期，请重新登录"})
                return
            if sess.get("must_change_password"):
                self._send_json(403, {"error": "PasswordChangeRequired", "message": "首次登录请先修改初始密码，再进行其他操作"})
                return
            evt_id = data.get("event_id")
            is_false = bool(data.get("is_false_alarm", True))
            cat = data.get("category", "other")
            notes = data.get("notes", "")
            h_id = data.get("household_id", pilot_manager.active_household_id)
            ok, msg, rec = incident_manager.submit_feedback(
                event_id=evt_id,
                is_false_alarm=is_false,
                category=cat,
                notes=notes,
                operator=data.get("operator", "miniprogram_family"),
                placement_adapter=placement_adapter,
            )
            if is_false:
                pilot_manager.record_false_alarm(household_id=h_id, category=cat)
                if hasattr(SafeHaloWebHandler, "on_false_alarm_feedback") and SafeHaloWebHandler.on_false_alarm_feedback:
                    SafeHaloWebHandler.on_false_alarm_feedback(f"[{cat}] {notes}")
            else:
                pilot_manager.record_true_alarm(household_id=h_id)
            shared_system_state["pilot"] = pilot_manager.get_pilot_dashboard()
            from dataclasses import asdict
            self._send_json(200, {
                "success": ok,
                "message": msg,
                "incident": asdict(rec) if rec else None,
                "pilot_dashboard": shared_system_state["pilot"],
            })
            return

        # 多传感器生态事件上报与融合联动 (带限速；当启用了 api_token 时要求认证或机器凭据)
        if self.path == "/api/sensors/event":
            if (self.api_token or "").strip() and not self._get_session(query):
                self._send_json(401, {"error": "Unauthorized", "message": "传感器事件上报需要有效登录态或 api_token"})
                return
            if not _rate_limiter.allow(f"sensor_ev:{client_ip}", 30, 10.0):
                self._send_json(429, {"success": False, "message": "传感器事件上报过于频繁，请稍后再试"})
                return
            device_id = str(data.get("device_id", "")).strip()
            event_name = str(data.get("event", data.get("state", ""))).strip()
            if not device_id or not event_name:
                self._send_json(400, {"success": False, "message": "Missing device_id or event"})
                return
            ev_norm = event_name.lower()
            if ev_norm in ("voice_dismiss", "dismiss", "button_press", "voice_help", "help", "sos", "sos_pressed"):
                if not _rate_limiter.allow(f"sensor_crit:{client_ip}", 2, 5.0):
                    self._send_json(429, {"success": False, "message": "关键语音/SOS操作过于频繁，请稍后再试"})
                    return
            timestamp = data.get("timestamp")
            extra = data.get("extra") or {}
            if "room" in data:
                extra["room"] = data["room"]
            action, msg = sensor_ecosystem.handle_event(
                device_id=device_id,
                event_name=event_name,
                timestamp=timestamp,
                extra=extra,
            )
            self._send_json(200, {
                "success": True,
                "action": action.value,
                "message": msg,
                "device_id": device_id,
            })
            return

        # --- 以下 POST 操作均需要有效登录态 ---
        sess = self._get_session(query)
        if not sess:
            self._send_json(401, {"error": "Unauthorized", "message": "未登录或登录已过期，请重新登录"})
            return
        if sess.get("must_change_password") and not _must_change_exempt("POST", self.path):
            self._send_json(403, {"error": "PasswordChangeRequired", "message": "首次登录请先修改初始密码，再进行其他操作"})
            return

        # 2. 用户登出
        if self.path == "/api/auth/logout":
            cookie_header = self.headers.get("Cookie", "")
            for part in cookie_header.split(";"):
                part = part.strip()
                if part.startswith("safehalo_session="):
                    auth_manager.logout(part[len("safehalo_session="):].strip())
            # 清除 Cookie
            secure = "; Secure" if self.cookie_secure else ""
            clear_cookie = f"safehalo_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}"
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

        # 6. 发送手机测试告警并保存配置 (仅管理员权限)
        if self.path == "/api/test_push":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可触发测试推送"})
                return
            if not _rate_limiter.allow(f"test_push:{sess['username']}", 1, 10.0):
                self._send_json(429, {"success": False, "message": "测试推送过于频繁，请 10 秒后再试"})
                return
            token = (data.get("pushplus_token") or "").strip()
            serverchan_key = (data.get("serverchan_key") or "").strip()
            webhook_url = (data.get("webhook_url") or "").strip()
            sms_webhook_url = (data.get("sms_webhook_url") or "").strip()
            bark_key = (data.get("bark_key") or "").strip()
            notifier = AlertNotifier(
                pushplus_token=token if token else None,
                serverchan_key=serverchan_key if serverchan_key else None,
                webhook_url=webhook_url if webhook_url else None,
                sms_webhook_url=sms_webhook_url if sms_webhook_url else None,
                bark_key=bark_key if bark_key else None,
            )
            if token or serverchan_key or webhook_url or sms_webhook_url or bark_key:
                notifier.update_config(
                    pushplus_token=token if token else None,
                    serverchan_key=serverchan_key if serverchan_key else None,
                    webhook_url=webhook_url if webhook_url else None,
                    sms_webhook_url=sms_webhook_url if sms_webhook_url else None,
                    bark_key=bark_key if bark_key else None,
                )
                if hasattr(SafeHaloWebHandler, "on_notifier_updated") and SafeHaloWebHandler.on_notifier_updated:
                    try:
                        SafeHaloWebHandler.on_notifier_updated(notifier)
                    except Exception as e:
                        logger.error(f"同步通知配置回调失败: {e}")
            success = notifier.send_fall_alarm(
                room_name="家庭测试区域",
                details=f"管理员 [{sess['username']}] 点击控制台【发送测试告警】",
                event_id="evt_test",
            )
            log_event(f"用户 [{sess['username']}] 触发手机测试告警推送 (结果: {'成功' if success else '模拟发送'})")
            self._send_json(200, {"success": success, "message": "测试告警已发送并保存配置，请检查微信通知！"})
            return

        # 6.5 仅保存通知与灵敏度配置（不触发模拟告警，仅管理员）
        if self.path == "/api/notifier/config":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可修改通知配置"})
                return
            notifier = AlertNotifier()
            notifier.update_config(
                pushplus_token=data["pushplus_token"] if "pushplus_token" in data else None,
                serverchan_key=data["serverchan_key"] if "serverchan_key" in data else None,
                webhook_url=data["webhook_url"] if "webhook_url" in data else None,
                ha_webhook_url=data["ha_webhook_url"] if "ha_webhook_url" in data else None,
                sms_webhook_url=data["sms_webhook_url"] if "sms_webhook_url" in data else None,
                bark_key=data["bark_key"] if "bark_key" in data else None,
            )
            if hasattr(SafeHaloWebHandler, "on_notifier_updated") and SafeHaloWebHandler.on_notifier_updated:
                try:
                    SafeHaloWebHandler.on_notifier_updated(notifier)
                except Exception as e:
                    logger.error(f"同步通知配置回调失败: {e}")
            log_event(f"管理员 [{sess['username']}] 更新了告警通知渠道配置")
            self._send_json(200, {"success": True, "message": "告警通知配置已成功保存！"})
            return

        # 7. 手动触发定向排查 (所有登录成员均可关心老人)
        if self.path == "/api/trigger_inspect":
            log_event(f"成员 [{sess['username']}] 手动请求【启动定向深度排查】(激振提频至 100Hz)")
            shared_system_state["state"] = "FALL_SUSPECT"
            shared_system_state["sample_rate"] = 100.0
            if hasattr(SafeHaloWebHandler, "on_trigger_inspect") and SafeHaloWebHandler.on_trigger_inspect:
                try:
                    SafeHaloWebHandler.on_trigger_inspect(f"成员 [{sess['username']}] 手动触发深度核验")
                except Exception as e:
                    logger.error(f"下发定向排查回调失败: {e}")
            self._send_json(200, {
                "success": True,
                "message": "已向射频节点下发指令：提频至 100Hz，正在定向聚焦地面生命呼吸体征..."
            })
            return

        # 8. 切换安防模式 (管理员或机器凭据权限)
        if self.path == "/api/set_mode":
            if sess.get("role") not in ("admin", "machine"):
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可修改安防守护模式"})
                return
            if not _rate_limiter.allow(f"set_mode:{sess['username']}", 1, 2.0):
                self._send_json(429, {"success": False, "message": "模式切换过于频繁，请稍后再试"})
                return
            mode = data.get("mode", "FALL_GUARD")
            shared_system_state["security_mode"] = mode
            if hasattr(SafeHaloWebHandler, "on_mode_changed") and SafeHaloWebHandler.on_mode_changed:
                try:
                    SafeHaloWebHandler.on_mode_changed(mode)
                except Exception as e:
                    logger.error(f"下发安防模式切换回调失败: {e}")
            log_event(f"安防守护模式已由管理员 [{sess['username']}] 切换为: {mode}")
            self._send_json(200, {"success": True, "message": f"模式已切换为 {mode}"})
            return

        # 8.5 家属提交误报反馈，触发算法阈值自适应上调 (登录成员可用)
        if self.path == "/api/feedback/false_alarm":
            reason = data.get("reason", "家属在控制台标记误报")
            if hasattr(SafeHaloWebHandler, "on_false_alarm_feedback") and SafeHaloWebHandler.on_false_alarm_feedback:
                tuned = SafeHaloWebHandler.on_false_alarm_feedback(reason)
            else:
                tuned = placement_adapter.record_false_alarm_feedback(reason=reason)
            shared_system_state["state"] = "EMPTY"
            from dataclasses import asdict
            self._send_json(200, {
                "success": True,
                "message": f"已记录误报反馈并自动优化抗扰门限（新冲击阈值: {tuned.fall_spike_threshold}）",
                "tuned_parameters": asdict(tuned),
            })
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

        # 10. 硬件设备白名单指令下发 (仅管理员)
        if self.path == "/api/devices/command":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可向硬件设备下发指令"})
                return
            cmd = str(data.get("command", "")).strip()
            dev_id = str(data.get("device_id", "csi_probe_0")).strip()
            if not _is_command_allowed(cmd):
                self._send_json(400, {"success": False, "message": "Command not permitted in whitelist"})
                return

            callback = getattr(SafeHaloWebHandler, "on_device_command", None)
            if not callback:
                self._send_json(503, {"success": False, "message": "设备作动回调未接线"})
                return
            try:
                sent = callback(dev_id, cmd)
            except Exception as e:
                logger.warning(f"[ACTUATION] 设备指令回调异常: {e}")
                self._send_json(503, {"success": False, "message": "设备指令执行异常"})
                return
            if not sent:
                self._send_json(503, {"success": False, "message": "设备离线或指令写入失败"})
                return
            logger.info(f"[ACTUATION] 管理员 [{sess['username']}] 向设备 [{dev_id}] 下发白名单指令: {cmd}")
            self._send_json(200, {
                "success": True,
                "status": "sent",
                "message": "指令已成功下发",
                "device_id": dev_id,
                "command": cmd,
            })
            return

        # 11. 注册新传感器设备 (仅管理员)
        if self.path == "/api/sensors/register":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可注册新传感器"})
                return
            device_id = str(data.get("device_id", "")).strip()
            sensor_type = str(data.get("sensor_type", "other")).strip()
            name = str(data.get("name", device_id)).strip()
            room = str(data.get("room", "默认房间")).strip()
            if not device_id:
                self._send_json(400, {"success": False, "message": "Missing device_id"})
                return
            dev = sensor_ecosystem.register_device(device_id, sensor_type, name, room, data.get("metadata"))
            self._send_json(200, {"success": True, "device": dev.to_dict()})
            return

        # 12. 执行作动测试或下发音箱语音 (仅管理员)
        if self.path == "/api/sensors/actuate":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可执行作动测试"})
                return
            device_id = str(data.get("device_id", "")).strip()
            cmd = str(data.get("command", "")).strip()
            if not cmd:
                self._send_json(400, {"success": False, "message": "Missing command"})
                return
            if not _is_command_allowed(cmd):
                self._send_json(400, {"success": False, "message": "Command not permitted in whitelist"})
                return
            callback = getattr(SafeHaloWebHandler, "on_sensor_actuate", None)
            if not callback:
                self._send_json(503, {"success": False, "message": "传感器作动回调未接线"})
                return
            try:
                sent = callback(device_id, cmd)
            except Exception as e:
                logger.warning(f"[ACTUATE] 作动回调异常: {e}")
                self._send_json(503, {"success": False, "message": "传感器作动执行异常"})
                return
            if not sent:
                self._send_json(503, {"success": False, "message": "传感器离线或作动写入失败"})
                return
            logger.info(f"[ACTUATE] 管理员向生态设备 [{device_id}] 发送作动指令: {cmd}")
            self._send_json(200, {"success": True, "status": "sent", "message": f"作动指令已下发: {cmd}", "device_id": device_id, "command": cmd})
            return

        self.send_error(404, "Not Found")

def start_portal_server(port: int = 8080, host: str = "0.0.0.0", cookie_secure: bool = False, api_token: str = ""):
    """
    启动 SafeHalo Web 配置与远程管理服务
    :param port: 监听端口 (默认 8080)
    :param host: 监听地址 (默认 0.0.0.0，允许局域网与远程穿透访问)
    :param cookie_secure: 经 HTTPS 反向代理部署时置 True，为会话 Cookie 添加 Secure 标记
    :param api_token: 机器凭据 Token (可选)
    """
    SafeHaloWebHandler.cookie_secure = cookie_secure
    if api_token:
        SafeHaloWebHandler.api_token = api_token
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer((host, port), SafeHaloWebHandler) as httpd:
        print(f"\n{'='*65}")
        print(f"🌐 SafeHalo Web 远程管理控制台已启动: http://0.0.0.0:{port}")
        print(f"本地局域网访问: http://localhost:{port} 或 http://<路由器LAN_IP>:{port}")
        print(f"远程公网访问: 支持 Cloudflare Tunnel / Tailscale / FRP 穿透")
        print(f"默认管理员账号: admin / admin123")
        print(f"{'='*65}\n")
        httpd.serve_forever()

if __name__ == "__main__":
    start_portal_server(8080)

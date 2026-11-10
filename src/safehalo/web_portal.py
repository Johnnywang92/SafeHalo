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
from .placement_adapter import PlacementAdapter, RoomPlacementConfig, TunedParameters
from .pilot_telemetry import PilotHouseholdManager
from .miniprogram_bridge import IncidentFeedbackManager, WeChatMiniProgramBridge

# 全局摆放自适应协调器与阶段 2 试点/事件管理实例
placement_adapter = PlacementAdapter()
pilot_manager = PilotHouseholdManager()
incident_manager = IncidentFeedbackManager()

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
            self._send_json(200, shared_system_state.get("routine", {}))
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
            self._send_json(200, {
                "pilot_dashboard": pilot_manager.get_pilot_dashboard(),
                "auto_calibration": shared_system_state.get("auto_calibration", {}),
                "recent_incidents": incident_manager.list_recent_incidents(limit=10),
            })
            return

        # 1.7 阶段 2：告警事件与反馈列表查询 (公开/小程序端拉取)
        if self.path == "/api/incidents":
            self._send_json(200, {
                "incidents": incident_manager.list_recent_incidents(limit=30),
            })
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

        # 1.1 首次安装向导 / 摆放位置保存与算法即时适配 (支持免登初始化配置)
        if self.path == "/api/placement/save":
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
            if pushplus_token or webhook_url:
                notifier = AlertNotifier(
                    pushplus_token=pushplus_token if pushplus_token else None,
                    webhook_url=webhook_url if webhook_url else None,
                )
                notifier.update_config(
                    pushplus_token=pushplus_token if pushplus_token else None,
                    webhook_url=webhook_url if webhook_url else None,
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

        # 1.15 首次安装向导：发送温和的“平安测试通知”到手机 (支持向导内直接验证)
        if self.path == "/api/onboarding/test_push":
            token = (data.get("pushplus_token") or "").strip()
            webhook_url = (data.get("webhook_url") or "").strip()
            nickname = (data.get("care_nickname") or placement_adapter.current_config.care_nickname or "家里长辈").strip()
            room_name = (data.get("room_name") or placement_adapter.current_config.room_name or "客厅").strip()
            notifier = AlertNotifier(
                pushplus_token=token if token else None,
                webhook_url=webhook_url if webhook_url else None,
            )
            if token or webhook_url:
                notifier.update_config(
                    pushplus_token=token if token else None,
                    webhook_url=webhook_url if webhook_url else None,
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

        # 1.2 阶段 2：本地交互质询安全应答接口 (支持老人一键按键/语音网关免登取消误报)
        if self.path == "/api/challenge/acknowledge":
            source = data.get("source", "本地按钮/语音安全应答")
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

        # 1.3 阶段 2：小程序端/家属端事件确认与误报反馈回调 (支持小程序 Webhook 直接回调)
        if self.path == "/api/incidents/feedback":
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

        # 6. 发送手机测试告警并保存配置 (仅管理员权限)
        if self.path == "/api/test_push":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可触发测试推送"})
                return
            token = data.get("pushplus_token", "").strip()
            webhook_url = data.get("webhook_url", "").strip()
            sms_webhook_url = data.get("sms_webhook_url", "").strip()
            notifier = AlertNotifier(
                pushplus_token=token if token else None,
                webhook_url=webhook_url if webhook_url else None,
                sms_webhook_url=sms_webhook_url if sms_webhook_url else None,
            )
            if token or webhook_url or sms_webhook_url:
                notifier.update_config(
                    pushplus_token=token if token else None,
                    webhook_url=webhook_url if webhook_url else None,
                    sms_webhook_url=sms_webhook_url if sms_webhook_url else None,
                )
                if hasattr(SafeHaloWebHandler, "on_notifier_updated") and SafeHaloWebHandler.on_notifier_updated:
                    try:
                        SafeHaloWebHandler.on_notifier_updated(notifier)
                    except Exception as e:
                        logger.error(f"同步通知配置回调失败: {e}")
            success = notifier.send_fall_alarm(room_name="家庭测试区域", details=f"管理员 [{sess['username']}] 点击控制台【发送测试告警】")
            log_event(f"用户 [{sess['username']}] 触发手机测试告警推送 (结果: {'成功' if success else '模拟发送'})")
            self._send_json(200, {"success": success, "message": "测试告警已发送并保存配置，请检查微信通知！"})
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

        # 8. 切换安防模式 (仅管理员权限)
        if self.path == "/api/set_mode":
            if sess.get("role") != "admin":
                self._send_json(403, {"error": "Forbidden", "message": "权限不足，仅管理员可修改安防守护模式"})
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

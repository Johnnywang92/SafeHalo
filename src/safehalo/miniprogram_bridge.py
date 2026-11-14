"""
SafeHalo 阶段 2：家属端小程序订阅消息对接与事件/误报反馈闭环模块 (miniprogram_bridge.py)
对应 docs/phase1_plan.md 阶段 2 目标 3:
「家属端告警：对接现有小程序订阅消息和后端，记录事件与误报反馈」

核心功能:
1. 微信小程序订阅消息双模式对接 (WeChatMiniProgramBridge):
   - 模式 A (对接现有小程序后端 Webhook): 向小程序业务后端 POST 结构化告警事件 (含唯一 event_id、老人户号、房间、呼吸率、小程序跳转路径)。
   - 模式 B (微信官方 API 直推): 使用 AppID + AppSecret 自动获取 AccessToken，调用 `cgi-bin/message/subscribe/send` 向家属 OpenID 列表发送微信订阅消息卡片。
2. 事件生命周期与误报反馈管理 (IncidentFeedbackManager):
   - 每条告警或质询拦截事件分配唯一 `event_id` 并持久化至 `logs/incidents.jsonl`。
   - 支持家属从小程序或 Web 端提交反馈:
     * 确认真实报警 (`CONFIRMED_TRUE`)
     * 标记误报并选择归因 (`FALSE_ALARM`: `pet` 宠物、`curtain_fan` 窗帘风扇、`dropped_object` 物品滑落、`normal_sit_lie` 正常坐卧、`shower_water` 淋浴水流)
   - 根据误报归因定向微调算法参数，并自动同步至 3~5 户试点统计模块 (`PilotHouseholdManager`)。
"""

import json
import os
import secrets
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .logger import logger


class IncidentStatus:
    PENDING = "PENDING"                         # 已推送家属，待反馈确认
    CONFIRMED_TRUE = "CONFIRMED_TRUE"           # 家属确认为真实跌倒/真实异常
    FALSE_ALARM = "FALSE_ALARM"                 # 家属确认为误报
    CHALLENGE_INTERCEPTED = "CHALLENGE_INTERCEPTED" # 本地交互质询期内老人应答解除（未打扰家属）


FALSE_ALARM_CATEGORIES = {
    "pet": "宠物跑跳/猫狗干扰",
    "curtain_fan": "窗帘飘动/风扇空调摆风",
    "dropped_object": "枕头/被子/书本等物品掉落",
    "normal_sit_lie": "老人正常快速坐下/躺下",
    "shower_water": "卫生间淋浴花洒水流",
    "neighbor": "隔壁邻居穿墙信号干扰",
    "other": "其他日常活动误触",
}


@dataclass
class AlertIncidentRecord:
    """单次安全告警事件与反馈闭环记录"""
    event_id: str
    timestamp: float
    time_str: str
    household_id: str
    room_name: str
    event_type: str                             # "FALL_ALARM" | "ROUTINE_ANOMALY" | "SECURITY_INTRUSION" | "CHALLENGE_INTERCEPT"
    confidence: float
    respiration_bpm: float
    details: str
    status: str = IncidentStatus.PENDING
    feedback_category: str = ""
    feedback_notes: str = ""
    feedback_by: str = ""
    feedback_at: str = ""
    miniprogram_pushed: bool = False


class WeChatMiniProgramBridge:
    """微信小程序订阅消息与后端对接客户端（纯标准库实现）"""

    TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
    SUBSCRIBE_SEND_URL = "https://api.weixin.qq.com/cgi-bin/message/subscribe/send"

    def __init__(
        self,
        backend_api_url: Optional[str] = None,
        backend_api_token: Optional[str] = None,
        appid: Optional[str] = None,
        appsecret: Optional[str] = None,
        template_id: Optional[str] = None,
        subscriber_openids: Optional[List[str]] = None,
        page_path_prefix: str = "pages/alert/detail",
    ):
        self.backend_api_url = backend_api_url
        self.backend_api_token = backend_api_token
        self.appid = appid
        self.appsecret = appsecret
        self.template_id = template_id
        self.subscriber_openids = subscriber_openids or []
        self.page_path_prefix = page_path_prefix

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def is_configured(self) -> bool:
        return bool(self.backend_api_url or (self.appid and self.appsecret and self.template_id))

    def build_subscribe_payload(self, openid: str, incident: AlertIncidentRecord) -> Dict[str, Any]:
        """构建符合微信小程序 `subscribeMessage.send` 标准规范的 JSON 报文"""
        return {
            "touser": openid,
            "template_id": self.template_id or "SAFEHALO_FALL_ALERT_TPL",
            "page": f"{self.page_path_prefix}?event_id={incident.event_id}&household_id={incident.household_id}",
            "miniprogram_state": "formal",
            "lang": "zh_CN",
            "data": {
                "thing1": {"value": f"{incident.room_name}疑似跌倒预警"[:20]},
                "time2": {"value": incident.time_str},
                "thing3": {"value": f"呼吸约{incident.respiration_bpm:.0f}次/分,置信度{int(incident.confidence*100)}%"[:20]},
                "phrase4": {"value": "待家属确认"},
                "thing5": {"value": "点击查看详情或标记误报校准"[:20]},
            },
        }

    def push_incident(self, incident: AlertIncidentRecord) -> bool:
        """向小程序后端及微信订阅消息接口推送告警事件"""
        pushed = False

        # 模式 A: 推送至现有小程序云后端 API
        if self.backend_api_url:
            backend_payload = {
                "source": "SafeHalo_Phase2",
                "event_id": incident.event_id,
                "household_id": incident.household_id,
                "room_name": incident.room_name,
                "event_type": incident.event_type,
                "confidence": incident.confidence,
                "respiration_bpm": incident.respiration_bpm,
                "details": incident.details,
                "timestamp": incident.timestamp,
                "time_str": incident.time_str,
                "miniprogram_page": f"{self.page_path_prefix}?event_id={incident.event_id}",
                "feedback_callback_path": "/api/incidents/feedback",
            }
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if self.backend_api_token:
                headers["Authorization"] = f"Bearer {self.backend_api_token}"
            pushed = self._http_post(self.backend_api_url, backend_payload, headers=headers) or pushed

        # 模式 B: 直接调用微信服务端订阅消息接口
        if self.appid and self.appsecret and self.template_id and self.subscriber_openids:
            token = self._get_wechat_access_token()
            if token:
                url = f"{self.SUBSCRIBE_SEND_URL}?access_token={token}"
                for openid in self.subscriber_openids:
                    wx_payload = self.build_subscribe_payload(openid, incident)
                    pushed = self._http_post(url, wx_payload) or pushed

        if not self.is_configured():
            logger.info(
                f"[MiniProgramBridge 本地仿真] 已生成小程序订阅消息卡片: "
                f"event_id={incident.event_id}, 房间={incident.room_name}"
            )
            return True

        return pushed

    def _get_wechat_access_token(self) -> Optional[str]:
        now = time.time()
        if self._access_token and now < self._token_expires_at:
            return self._access_token
        try:
            url = (
                f"{self.TOKEN_URL}?grant_type=client_credential"
                f"&appid={self.appid}&secret={self.appsecret}"
            )
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if "access_token" in data:
                    self._access_token = data["access_token"]
                    self._token_expires_at = now + int(data.get("expires_in", 7200)) - 300
                    return self._access_token
        except Exception as e:
            logger.error(f"[MiniProgramBridge] 获取微信 AccessToken 失败: {e}")
        return None

    @staticmethod
    def _http_post(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> bool:
        try:
            req_headers = headers or {"Content-Type": "application/json; charset=utf-8"}
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=req_headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                _ = resp.read()
                return True
        except Exception as e:
            logger.error(f"[MiniProgramBridge] POST {url} 失败: {e}")
            return False


class IncidentFeedbackManager:
    """事件全生命周期记录与家属端误报反馈闭环管理器"""

    def __init__(
        self,
        storage_dir: Optional[str] = None,
        default_household_id: str = "H01",
        miniprogram_bridge: Optional[WeChatMiniProgramBridge] = None,
        on_feedback_recorded: Optional[Callable[[AlertIncidentRecord], None]] = None,
    ):
        self.storage_dir = storage_dir or os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "logs")
        )
        os.makedirs(self.storage_dir, exist_ok=True)
        self.incidents_file = os.path.join(self.storage_dir, "incidents.jsonl")
        self.default_household_id = default_household_id
        self.miniprogram_bridge = miniprogram_bridge or WeChatMiniProgramBridge()
        self.on_feedback_recorded = on_feedback_recorded

        self._lock = threading.Lock()
        self.incidents: Dict[str, AlertIncidentRecord] = {}
        self._load_incidents()

    def _load_incidents(self):
        if not os.path.exists(self.incidents_file):
            return
        try:
            with open(self.incidents_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    rec = AlertIncidentRecord(**d)
                    self.incidents[rec.event_id] = rec
        except Exception as e:
            logger.error(f"[IncidentManager] 加载历史告警事件失败: {e}")

    def _append_record(self, rec: AlertIncidentRecord):
        try:
            with open(self.incidents_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            try:
                os.chmod(self.incidents_file, 0o600)
            except OSError:
                pass
        except Exception as e:
            logger.error(f"[IncidentManager] 追加告警事件记录失败: {e}")

    def _save_all(self):
        try:
            with open(self.incidents_file, "w", encoding="utf-8") as f:
                for rec in self.incidents.values():
                    f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            try:
                os.chmod(self.incidents_file, 0o600)
            except OSError:
                pass
        except Exception as e:
            logger.error(f"[IncidentManager] 写入告警事件记录失败: {e}")

    def record_incident(
        self,
        room_name: str,
        event_type: str = "FALL_ALARM",
        confidence: float = 0.95,
        respiration_bpm: float = 16.0,
        details: str = "",
        household_id: Optional[str] = None,
        status: str = IncidentStatus.PENDING,
        timestamp: Optional[float] = None,
        push_to_miniprogram: bool = True,
    ) -> AlertIncidentRecord:
        """创建一条新的安全事件记录，并自动推送至微信小程序端"""
        now = timestamp or time.time()
        dt_str = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
        evt_id = f"evt_{datetime.fromtimestamp(now).strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"

        rec = AlertIncidentRecord(
            event_id=evt_id,
            timestamp=now,
            time_str=dt_str,
            household_id=household_id or self.default_household_id,
            room_name=room_name,
            event_type=event_type,
            confidence=round(confidence, 2),
            respiration_bpm=round(respiration_bpm, 1),
            details=details,
            status=status,
        )

        if push_to_miniprogram and status == IncidentStatus.PENDING:
            rec.miniprogram_pushed = self.miniprogram_bridge.push_incident(rec)

        with self._lock:
            self.incidents[evt_id] = rec
            if len(self.incidents) > 500:
                oldest_key = min(self.incidents.keys(), key=lambda k: self.incidents[k].timestamp)
                del self.incidents[oldest_key]
                self._save_all()
            else:
                self._append_record(rec)

        return rec

    def submit_feedback(
        self,
        event_id: Optional[str] = None,
        is_false_alarm: bool = True,
        category: str = "other",
        notes: str = "",
        operator: str = "family_miniprogram",
        placement_adapter: Optional[Any] = None,
        csi_engine: Optional[Any] = None,
        dual_engine: Optional[Any] = None,
    ) -> Tuple[bool, str, Optional[AlertIncidentRecord]]:
        """
        家属在小程序或控制台对事件进行确认或标记误报，驱动定向自适应调参
        """
        with self._lock:
            target: Optional[AlertIncidentRecord] = None
            if event_id and event_id in self.incidents:
                target = self.incidents[event_id]
            elif self.incidents:
                # 若未指定 event_id，默认针对最近一条告警事件进行反馈
                latest_key = max(self.incidents.keys(), key=lambda k: self.incidents[k].timestamp)
                target = self.incidents[latest_key]

            if not target:
                # 若尚无历史事件，自动创建一条反馈快照
                now = time.time()
                target = AlertIncidentRecord(
                    event_id=event_id or f"evt_manual_{int(now)}",
                    timestamp=now,
                    time_str=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
                    household_id=self.default_household_id,
                    room_name="默认监测区",
                    event_type="FALL_ALARM",
                    confidence=0.90,
                    respiration_bpm=16.0,
                    details="手动提交的误报反馈记录",
                )
                self.incidents[target.event_id] = target

            target.status = IncidentStatus.FALSE_ALARM if is_false_alarm else IncidentStatus.CONFIRMED_TRUE
            target.feedback_category = category if is_false_alarm else "confirmed_fall"
            target.feedback_notes = notes
            target.feedback_by = operator
            target.feedback_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_all()

        # 若为误报，根据误报分类 (category) 靶向优化算法参数
        tune_msg = ""
        if is_false_alarm and placement_adapter is not None:
            params = placement_adapter.current_tuned_params
            cat_desc = FALSE_ALARM_CATEGORIES.get(category, category)
            if category == "pet":
                params.pet_spr_threshold = round(min(0.65, params.pet_spr_threshold + 0.05), 2)
                tune_msg = f"已针对【{cat_desc}】将宠物载波抑制阈值提升至 {params.pet_spr_threshold}"
            elif category == "shower_water":
                params.water_flow_damping = True
                params.hampel_window_size = max(7, params.hampel_window_size)
                tune_msg = f"已针对【{cat_desc}】开启水流微动低通阻尼滤波"
            elif category == "curtain_fan":
                params.hampel_window_size = min(11, params.hampel_window_size + 2)
                params.fall_spike_threshold = round(min(3.5, params.fall_spike_threshold * 1.08), 2)
                tune_msg = f"已针对【{cat_desc}】扩大时域滤波窗至 {params.hampel_window_size} 并上调冲击阈值至 {params.fall_spike_threshold}"
            else:
                placement_adapter.record_false_alarm_feedback(
                    reason=f"{cat_desc}: {notes}",
                    csi_engine=csi_engine,
                    dual_engine=dual_engine,
                )
                tune_msg = f"已针对【{cat_desc}】上调冲击阈值至 {params.fall_spike_threshold}、静止确认至 {params.fall_stillness_seconds}s"

            placement_adapter.apply_to_engines(csi_engine=csi_engine, dual_engine=dual_engine)

        if self.on_feedback_recorded:
            try:
                self.on_feedback_recorded(target)
            except Exception as e:
                logger.error(f"[IncidentManager] 反馈回调执行异常: {e}")

        msg = (
            f"事件 [{target.event_id}] 已标记为【误报 ({FALSE_ALARM_CATEGORIES.get(category, category)})】。{tune_msg}"
            if is_false_alarm
            else f"事件 [{target.event_id}] 已确认为【真实告警】，感谢家属反馈！"
        )
        return True, msg, target

    def list_recent_incidents(self, limit: int = 30, household_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self.incidents.values())
            if household_id:
                items = [x for x in items if x.household_id == household_id]
            items.sort(key=lambda x: x.timestamp, reverse=True)
            return [asdict(x) for x in items[:limit]]

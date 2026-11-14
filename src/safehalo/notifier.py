"""
SafeHalo 移动端通知与告警模块 (notifier.py)
支持以下通道向用户手机推送实时告警与状态:
1. 微信服务号 / 模板消息推送 (PushPlus / Server酱) - 免装 App，全家微信即时弹窗
2. 自定义 Webhook (支持飞书/企业微信/钉钉群机器人)
3. 短信 / 语音外呼 Webhook 接口 (如阿里云、腾讯云 SMS)
4. iOS Bark 关键警告强提醒 (可突破 iPhone 静音开关与勿扰专注模式，夜间必达)
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime
from typing import Dict, Optional
from .logger import logger

NOTIFIER_CONFIG_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "logs", "notifier_config.json")
)

class AlertNotifier:
    def __init__(
        self,
        pushplus_token: Optional[str] = None,
        serverchan_key: Optional[str] = None,
        webhook_url: Optional[str] = None,
        ha_webhook_url: Optional[str] = None,
        sms_webhook_url: Optional[str] = None,
        bark_key: Optional[str] = None,
        config_file: str = NOTIFIER_CONFIG_PATH,
    ):
        """
        :param pushplus_token: PushPlus (微信关注推送加公众号即可推送，免费支持多人接收)
        :param serverchan_key: Server酱 SendKey (微信推送)
        :param webhook_url: 企业微信/飞书/钉钉机器人 Webhook 地址
        :param ha_webhook_url: Home Assistant 原生自动化 Webhook 地址 (如 http://homeassistant.local:8123/api/webhook/safehalo_fall)
        :param sms_webhook_url: 短信 / 语音外呼网关 Webhook 地址 (如阿里云、腾讯云或 Twilio 语音外呼触发接口)
        :param bark_key: iOS Bark 设备推送 Key (可突破 iPhone 专注模式与静音开关发出警报声)
        """
        self.config_file = config_file
        self.pushplus_token = pushplus_token
        self.serverchan_key = serverchan_key
        self.webhook_url = webhook_url
        self.ha_webhook_url = ha_webhook_url
        self.sms_webhook_url = sms_webhook_url
        self.bark_key = bark_key
        self._last_alert_room: str = ""
        self._last_alert_time: float = 0.0

        # 若未显式传入，尝试加载已持久化的配置
        self._load_persisted_config()

    def _load_persisted_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    if not self.pushplus_token:
                        self.pushplus_token = cfg.get("pushplus_token") or None
                    if not self.serverchan_key:
                        self.serverchan_key = cfg.get("serverchan_key") or None
                    if not self.webhook_url:
                        self.webhook_url = cfg.get("webhook_url") or None
                    if not self.ha_webhook_url:
                        self.ha_webhook_url = cfg.get("ha_webhook_url") or None
                    if not self.sms_webhook_url:
                        self.sms_webhook_url = cfg.get("sms_webhook_url") or None
                    if not self.bark_key:
                        self.bark_key = cfg.get("bark_key") or None
            except Exception as e:
                logger.error(f"[Notifier] 读取通知配置失败: {e}")

    def update_config(
        self,
        pushplus_token: Optional[str] = None,
        serverchan_key: Optional[str] = None,
        webhook_url: Optional[str] = None,
        ha_webhook_url: Optional[str] = None,
        sms_webhook_url: Optional[str] = None,
        bark_key: Optional[str] = None,
    ) -> Dict[str, str]:
        """更新并持久化移动端通知凭据"""
        if pushplus_token is not None:
            self.pushplus_token = pushplus_token.strip() or None
        if serverchan_key is not None:
            self.serverchan_key = serverchan_key.strip() or None
        if webhook_url is not None:
            self.webhook_url = webhook_url.strip() or None
        if ha_webhook_url is not None:
            self.ha_webhook_url = ha_webhook_url.strip() or None
        if sms_webhook_url is not None:
            self.sms_webhook_url = sms_webhook_url.strip() or None
        if bark_key is not None:
            self.bark_key = bark_key.strip() or None

        payload = {
            "pushplus_token": self.pushplus_token or "",
            "serverchan_key": self.serverchan_key or "",
            "webhook_url": self.webhook_url or "",
            "ha_webhook_url": self.ha_webhook_url or "",
            "sms_webhook_url": self.sms_webhook_url or "",
            "bark_key": self.bark_key or "",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            try:
                os.chmod(self.config_file, 0o600)
            except OSError:
                pass
        except Exception as e:
            logger.error(f"[Notifier] 保存通知配置失败: {e}")
        return payload

    def send_fall_alarm(
        self,
        room_name: str = "老人卧室",
        confidence: str = "高",
        details: str = "",
        bpm: float = 18.0,
        event_id: str = "",
        household_id: str = "H01",
    ) -> bool:
        """
        发送高优先级跌倒警报至家属手机 (支持去重与各通道统一契约)
        """
        import time

        # 去重保护：30秒内同房间相同级别报警不重复弹窗打扰家属 (手动测试除外)
        now_ts = time.time()
        if (
            room_name == self._last_alert_room
            and (now_ts - self._last_alert_time) < 30.0
            and event_id != "evt_test"
        ):
            logger.info(f"[Notifier 去重] 30秒内忽略房间 [{room_name}] 的重复跌倒推送")
            return True
        self._last_alert_room = room_name
        self._last_alert_time = now_ts

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title = f"🚨【紧急预警】SafeHalo 检测到疑似跌倒！"
        bpm_line = f"- **呼吸估计**: {bpm:.0f} bpm\n" if bpm > 0 else ""
        evt_line = f"- **事件编号**: {event_id}\n" if event_id else ""
        content = (
            f"### 紧急安全事件通知\n\n"
            f"- **监测位置**: {room_name}\n"
            f"- **发生时间**: {now_str}\n"
            f"- **预警级别**: 红色紧急（检测到剧烈冲击并持续倒地静止）\n"
            f"- **置信度**: {confidence}\n"
            f"{bpm_line}"
            f"{evt_line}"
            f"- **附带信息**: {details}\n\n"
            f"> 请立即通过家中监控或电话确认老人安全！如无法取得联系，请尽快前往或联系邻居协助。"
        )

        success = False

        # 1. PushPlus 微信推送 (推荐，微信直接弹服务通知)
        if self.pushplus_token:
            success = self._send_pushplus(title, content) or success

        # 2. Server酱 微信推送
        if self.serverchan_key:
            success = self._send_serverchan(title, content) or success

        # 3. 群机器人 Webhook
        if self.webhook_url:
            success = self._send_webhook(title, content) or success

        # 4. Home Assistant Webhook (联动全屋智能灯光变红、智能音箱语音播报)
        if self.ha_webhook_url:
            success = self._send_ha_webhook(
                event="fall_alarm",
                room_name=room_name,
                confidence=confidence,
                details=details,
                bpm=bpm,
                event_id=event_id,
                household_id=household_id,
            ) or success

        # 5. 短信 / 语音电话强提醒外呼网关 Webhook
        if self.sms_webhook_url:
            success = self._send_sms_voice_webhook(
                event="fall_alarm",
                room_name=room_name,
                title=title,
                details=details,
                confidence=confidence,
                bpm=bpm,
                event_id=event_id,
                household_id=household_id,
            ) or success

        # 6. iOS Bark 关键警报强提醒 (突破静音/勿扰)
        if self.bark_key:
            bark_body = f"监测位置: {room_name}\n置信度: {confidence} | 呼吸: {bpm:.0f} bpm\n事件编号: {event_id}\n{details}\n请立即确认老人安全！"
            success = self._send_bark(title, bark_body, critical=True, sound="alarm") or success

        if not (self.pushplus_token or self.serverchan_key or self.webhook_url or self.ha_webhook_url or self.sms_webhook_url or self.bark_key):
            logger.info(f"[Notifier 本地模拟推送] {title} - {details}")
            return True

        return success

    def send_intruder_alarm(self, room_name: str = "家中客厅", details: str = "离家布防模式下探测到人体大幅走动") -> bool:
        """
        发送高优先级防盗入侵警报至家属手机
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title = f"🛡️【防盗告警】SafeHalo 检测到非法人员入侵！"
        content = (
            f"### 房屋安防入侵警告\n\n"
            f"- **告警位置**: {room_name}\n"
            f"- **发现时间**: {now_str}\n"
            f"- **安防状态**: 离家布防中 (家中本应无人)\n"
            f"- **触发详情**: {details} (已自动排除地面宠物)\n\n"
            f"> 警告: 屋内检测到明确的人体反射信号，请注意核实家中安全或查看外部监控！"
        )

        success = False
        if self.pushplus_token:
            success = self._send_pushplus(title, content) or success
        if self.serverchan_key:
            success = self._send_serverchan(title, content) or success
        if self.webhook_url:
            success = self._send_webhook(title, content) or success
        if self.bark_key:
            bark_body = f"告警位置: {room_name}\n安防状态: 离家布防中\n{details}\n屋内检测到明确人员移动信号！"
            success = self._send_bark(title, bark_body, critical=True, sound="alarm") or success

        if not (self.pushplus_token or self.serverchan_key or self.webhook_url or self.bark_key):
            print(f"[Notifier 本地模拟防盗推送] {title}\n{content}")
            return True

        return success

    def send_routine_anomaly_alert(self, title: str, description: str, severity: str = "ALERT") -> bool:
        """
        发送老人日常作息严重偏离通报 (如晨间未按时起床、日间异常长时间静止无活动、夜间起夜过多)
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        icon = "⚠️" if severity == "WARNING" else "🚨"
        formatted_title = f"{icon}{title}"
        content = (
            f"### 老人日常作息异常偏离通报\n\n"
            f"- **事件类型**: {title}\n"
            f"- **通报时间**: {now_str}\n"
            f"- **偏离详情**: {description}\n\n"
            f"> 建议: 慢性作息剧烈突变常为心脑血管意外、低血糖或感染前兆。请及时致电老人或上门核实日常起居状况！"
        )

        success = False
        if self.pushplus_token:
            success = self._send_pushplus(formatted_title, content) or success
        if self.serverchan_key:
            success = self._send_serverchan(formatted_title, content) or success
        if self.webhook_url:
            success = self._send_webhook(formatted_title, content) or success
        if self.ha_webhook_url:
            success = self._send_ha_webhook(
                event="routine_anomaly",
                room_name="家庭全域",
                confidence="高",
                details=description,
            ) or success
        if self.bark_key:
            bark_body = f"偏离详情: {description}\n建议: 请核实老人起居状况。"
            is_critical = (severity == "ALERT")
            success = self._send_bark(formatted_title, bark_body, critical=is_critical, sound="anticipate") or success

        if not (self.pushplus_token or self.serverchan_key or self.webhook_url or self.ha_webhook_url or self.bark_key):
            logger.info(f"[Notifier 本地模拟作息偏离通报] {formatted_title} - {description}")
            return True

        return success

    def _send_bark(
        self,
        title: str,
        body: str,
        critical: bool = True,
        sound: str = "alarm",
    ) -> bool:
        """通过 iOS Bark 客户端发送推送（支持 critical 突破静音与勿扰）"""
        if not self.bark_key:
            return False
        if self.bark_key.startswith("http://") or self.bark_key.startswith("https://"):
            url = self.bark_key
        else:
            url = f"https://api.day.app/{self.bark_key}"
        payload = {
            "title": title,
            "body": body,
            "level": "critical" if critical else "timeSensitive",
            "volume": 10 if critical else 5,
            "sound": sound,
            "group": "SafeHalo",
        }
        return self._http_post_json(url, payload)

    def _send_pushplus(self, title: str, markdown_content: str) -> bool:
        url = "http://www.pushplus.plus/send"
        payload = {
            "token": self.pushplus_token,
            "title": title,
            "content": markdown_content,
            "template": "markdown",
        }
        return self._http_post_json(url, payload)

    def _send_serverchan(self, title: str, markdown_content: str) -> bool:
        url = f"https://sctapi.ftqq.com/{self.serverchan_key}.send"
        payload = {
            "title": title,
            "desp": markdown_content,
        }
        return self._http_post_json(url, payload)

    def _send_webhook(self, title: str, markdown_content: str) -> bool:
        # 兼容企业微信/飞书/钉钉的通用 Markdown 格式
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"## {title}\n{markdown_content}"
            }
        }
        return self._http_post_json(self.webhook_url, payload)

    def _send_ha_webhook(
        self,
        event: str,
        room_name: str,
        confidence: str,
        details: str,
        bpm: float = 0.0,
        event_id: str = "",
        household_id: str = "H01",
    ) -> bool:
        """向 Home Assistant 发送自动化触发载荷"""
        payload = {
            "source": "SafeHalo",
            "event": event,
            "household_id": household_id,
            "room": room_name,
            "confidence": confidence,
            "respiration_bpm": bpm,
            "event_id": event_id,
            "details": details,
            "timestamp": datetime.now().isoformat(),
        }
        return self._http_post_json(self.ha_webhook_url, payload)

    def _send_sms_voice_webhook(
        self,
        event: str,
        room_name: str,
        title: str,
        details: str,
        confidence: str = "高",
        bpm: float = 0.0,
        event_id: str = "",
        household_id: str = "H01",
    ) -> bool:
        """向短信/语音外呼网关发送告警触发载荷"""
        payload = {
            "source": "SafeHalo",
            "event": event,
            "household_id": household_id,
            "room": room_name,
            "title": title,
            "confidence": confidence,
            "respiration_bpm": bpm,
            "event_id": event_id,
            "details": details,
            "priority": "CRITICAL",
            "timestamp": datetime.now().isoformat(),
        }
        return self._http_post_json(self.sms_webhook_url, payload)

    def _http_post_json(self, url: str, data_dict: dict) -> bool:
        try:
            req_data = json.dumps(data_dict).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                res = response.read().decode("utf-8")
                logger.info(f"[Notifier 推送成功] 响应: {res[:100]}")
                return True
        except Exception as e:
            logger.error(f"[Notifier 推送失败] 请求 {url} 出错: {e}")
            return False

if __name__ == "__main__":
    # 测试用例 (本地模拟推送)
    notifier = AlertNotifier()
    notifier.send_fall_alarm(room_name="主卧卫生间", details="持续静止 8 秒未见活动")

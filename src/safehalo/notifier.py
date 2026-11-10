"""
SafeHalo 移动端通知与告警模块 (notifier.py)
支持以下通道向用户手机推送实时告警与状态:
1. 微信服务号 / 模板消息推送 (PushPlus / Server酱) - 免装 App，全家微信即时弹窗
2. 自定义 Webhook (支持飞书/企业微信/钉钉群机器人)
3. 短信 / 语音外呼 Webhook 接口 (如阿里云、腾讯云 SMS)
"""

import json
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional
from .logger import logger

class AlertNotifier:
    def __init__(
        self,
        pushplus_token: Optional[str] = None,
        serverchan_key: Optional[str] = None,
        webhook_url: Optional[str] = None,
        ha_webhook_url: Optional[str] = None,
    ):
        """
        :param pushplus_token: PushPlus (微信关注推送加公众号即可推送，免费支持多人接收)
        :param serverchan_key: Server酱 SendKey (微信推送)
        :param webhook_url: 企业微信/飞书/钉钉机器人 Webhook 地址
        :param ha_webhook_url: Home Assistant 原生自动化 Webhook 地址 (如 http://homeassistant.local:8123/api/webhook/safehalo_fall)
        """
        self.pushplus_token = pushplus_token
        self.serverchan_key = serverchan_key
        self.webhook_url = webhook_url
        self.ha_webhook_url = ha_webhook_url

    def send_fall_alarm(self, room_name: str = "老人卧室", confidence: str = "高", details: str = "") -> bool:
        """
        发送高优先级跌倒警报至家属手机
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title = f"🚨【紧急预警】SafeHalo 检测到疑似跌倒！"
        content = (
            f"### 紧急安全事件通知\n\n"
            f"- **监测位置**: {room_name}\n"
            f"- **发生时间**: {now_str}\n"
            f"- **预警级别**: 红色紧急（检测到剧烈冲击并持续倒地静止）\n"
            f"- **置信度**: {confidence}\n"
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
                details=details
            ) or success

        if not (self.pushplus_token or self.serverchan_key or self.webhook_url):
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

        if not (self.pushplus_token or self.serverchan_key or self.webhook_url):
            print(f"[Notifier 本地模拟防盗推送] {title}\n{content}")
            return True

        return success

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

    def _send_ha_webhook(self, event: str, room_name: str, confidence: str, details: str) -> bool:
        """向 Home Assistant 发送自动化触发载荷"""
        payload = {
            "source": "SafeHalo",
            "event": event,
            "room": room_name,
            "confidence": confidence,
            "details": details,
            "timestamp": datetime.now().isoformat()
        }
        return self._http_post_json(self.ha_webhook_url, payload)

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

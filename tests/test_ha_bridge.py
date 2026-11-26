"""
SafeHalo Home Assistant 互联与 MQTT Discovery 单元测试 (test_ha_bridge.py)
"""

import json
import unittest
from src.safehalo.ha_discovery import MiniMQTTClient, HomeAssistantBridge
from src.safehalo.web_portal import shared_system_state


class MockMQTTClient(MiniMQTTClient):
    """用于测试的 Mock MQTT 客户端，记录所有 publish 的消息"""
    def __init__(self):
        super().__init__(client_id="mock_client", host="127.0.0.1")
        self.connected = True
        self.published_messages = []

    def publish(self, topic: str, payload: str, retain: bool = False) -> bool:
        self.published_messages.append((topic, payload, retain))
        return True

    def connect(self) -> bool:
        self.connected = True
        return True

    def ping(self) -> bool:
        return True


class TestHomeAssistantBridge(unittest.TestCase):
    def setUp(self):
        self.bridge = HomeAssistantBridge(
            mqtt_host="192.168.1.50",
            mqtt_port=1883,
            device_id="safehalo_test_device",
            device_name="SafeHalo 测试网关",
        )
        self.mock_client = MockMQTTClient()
        self.bridge.client = self.mock_client

    def test_mqtt_remaining_length_encoding(self):
        """测试 MQTT 协议可变长度字节编码算法"""
        client = MiniMQTTClient(client_id="test", host="127.0.0.1")
        self.assertEqual(bytes(client._encode_remaining_len(64)), bytes([64]))
        self.assertEqual(bytes(client._encode_remaining_len(127)), bytes([127]))
        self.assertEqual(bytes(client._encode_remaining_len(128)), bytes([0x80, 0x01]))
        self.assertEqual(bytes(client._encode_remaining_len(16383)), bytes([0xFF, 0x7F]))

    def test_ha_discovery_registration(self):
        """测试向 Home Assistant 注册实体配置"""
        self.bridge._publish_discovery()

        published = {topic: json.loads(payload) for topic, payload, _ in self.mock_client.published_messages}

        # 1. 验证跌倒报警二值传感器
        fall_topic = "homeassistant/binary_sensor/safehalo/fall/config"
        self.assertIn(fall_topic, published)
        fall_cfg = published[fall_topic]
        self.assertEqual(fall_cfg["device_class"], "safety")
        self.assertEqual(fall_cfg["state_topic"], "safehalo/binary_sensor/fall/state")
        self.assertEqual(fall_cfg["unique_id"], "safehalo_test_device_fall")
        self.assertEqual(fall_cfg["device"]["name"], "SafeHalo 测试网关")

        # 2. 验证人体存在传感器
        presence_topic = "homeassistant/binary_sensor/safehalo/presence/config"
        self.assertIn(presence_topic, published)
        self.assertEqual(published[presence_topic]["device_class"], "presence")

        # 3. 验证空间高度传感器
        height_topic = "homeassistant/sensor/safehalo/radar_height/config"
        self.assertIn(height_topic, published)
        self.assertEqual(published[height_topic]["unit_of_measurement"], "m")

        # 4. 验证呼吸微动传感器
        resp_topic = "homeassistant/sensor/safehalo/respiration_rate/config"
        self.assertIn(resp_topic, published)
        self.assertEqual(published[resp_topic]["unit_of_measurement"], "bpm")

        # 5. 验证主动扫描按钮
        btn_topic = "homeassistant/button/safehalo/inspect_now/config"
        self.assertIn(btn_topic, published)
        self.assertEqual(published[btn_topic]["command_topic"], "safehalo/command/inspect")

        # 6. 验证作息偏离预警二值传感器
        routine_topic = "homeassistant/binary_sensor/safehalo/routine_anomaly/config"
        self.assertIn(routine_topic, published)
        self.assertEqual(published[routine_topic]["device_class"], "problem")

        # 7. 验证起床时间传感器
        wake_topic = "homeassistant/sensor/safehalo/wake_time/config"
        self.assertIn(wake_topic, published)

        # 8. 验证多传感器生态安防警报
        security_topic = "homeassistant/binary_sensor/safehalo/security/config"
        self.assertIn(security_topic, published)
        self.assertEqual(published[security_topic]["state_topic"], "safehalo/binary_sensor/security/state")
        self.assertEqual(published[security_topic]["device_class"], "safety")

    def test_ha_state_publishing(self):
        """测试状态流转时发布到对应 Home Assistant state topics"""
        self.mock_client.published_messages.clear()

        # 发布跌倒与作息状态
        self.bridge.publish_state(
            human_state="FALL_CONFIRMED",
            is_fall=True,
            is_presence=True,
            is_moving=False,
            is_pet=False,
            radar_z=0.22,
            respiration_bpm=16.8,
            confidence=0.98,
            routine_anomaly=True,
            wake_time="07:20",
            night_restless_count=2,
            is_security_alert=True,
        )

        published_dict = {topic: payload for topic, payload, _ in self.mock_client.published_messages}
        self.assertEqual(published_dict.get("safehalo/binary_sensor/fall/state"), "ON")
        self.assertEqual(published_dict.get("safehalo/binary_sensor/security/state"), "ON")
        self.assertEqual(published_dict.get("safehalo/binary_sensor/presence/state"), "ON")
        self.assertEqual(published_dict.get("safehalo/binary_sensor/motion/state"), "OFF")
        self.assertEqual(published_dict.get("safehalo/binary_sensor/pet/state"), "OFF")
        self.assertEqual(published_dict.get("safehalo/sensor/state/state"), "FALL_CONFIRMED")
        self.assertEqual(published_dict.get("safehalo/sensor/radar_z/state"), "0.22")
        self.assertEqual(published_dict.get("safehalo/sensor/respiration/state"), "16.8")
        self.assertEqual(published_dict.get("safehalo/sensor/confidence/state"), "98")
        self.assertEqual(published_dict.get("safehalo/binary_sensor/routine_anomaly/state"), "ON")
        self.assertEqual(published_dict.get("safehalo/sensor/wake_time/state"), "07:20")
        self.assertEqual(published_dict.get("safehalo/sensor/night_restless/state"), "2")


if __name__ == "__main__":
    unittest.main()

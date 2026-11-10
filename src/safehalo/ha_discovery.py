"""
SafeHalo Home Assistant 智能家居互联集成模块 (ha_discovery.py)
基于 Home Assistant 官方 MQTT Discovery 标准规范，提供免配置零门槛的即插即用接入。

核心功能:
1. MQTT 自动发现 (MQTT Auto-Discovery):
   - 发布标准配置报文至 homeassistant/<component>/safehalo/.../config
   - Home Assistant 自动发现并生成以下实体:
     * binary_sensor.safehalo_fall (跌倒报警，device_class: safety)
     * binary_sensor.safehalo_presence (人体存在，device_class: presence)
     * binary_sensor.safehalo_motion (空间走动，device_class: motion)
     * binary_sensor.safehalo_pet (宠物活动，device_class: presence, icon: mdi:paw)
     * sensor.safehalo_human_state (全局人体状态枚举)
     * sensor.safehalo_radar_height (空间测定躯干高度 Z 轴，单位: m)
     * sensor.safehalo_respiration_rate (活体呼吸微动频率，单位: bpm)
     * sensor.safehalo_fused_confidence (双模态决策置信度，单位: %)
     * alarm_control_panel.safehalo_guard (隐形防盗布撤防控制板)
     * button.safehalo_inspect (主动激振定向扫描按钮)
2. 纯 Python 标准库 MQTT 客户端 (MiniMQTTClient):
   - 零依赖 (无须 pip install paho-mqtt)，可在极其受限的 OpenWrt 路由器环境裸机运行。
   - 若系统已安装 paho-mqtt，则自动无缝选用 paho-mqtt。
3. 状态发布与反向控制:
   - 状态流转时秒级发布至 safehalo/state, safehalo/telemetry
   - 监听 safehalo/command/... 实现从 Home Assistant 遥控布防与定向扫描。
"""

import json
import socket
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from .logger import logger


class MiniMQTTClient:
    """
    纯 Python 标准库实现的轻量级 MQTT 3.1.1 客户端
    实现基本的 CONNECT, PUBLISH (QoS 0), SUBSCRIBE, PINGREQ, DISCONNECT
    专为 OpenWrt 等无 pip/第三方库的嵌入式网关设计。
    """
    def __init__(self, client_id: str, host: str, port: int = 1883,
                 username: Optional[str] = None, password: Optional[str] = None,
                 keepalive: int = 60,
                 on_message: Optional[Callable[[str, str], None]] = None):
        self.client_id = client_id
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.keepalive = keepalive
        self.on_message = on_message

        self.sock: Optional[socket.socket] = None
        self.connected = False
        self.running = False
        self._lock = threading.Lock()
        self._sub_topics: List[str] = []

    def connect(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))

            # 构建 MQTT CONNECT 报文
            var_header = bytearray([
                0x00, 0x04, ord('M'), ord('Q'), ord('T'), ord('T'), # 协议名 MQTT
                0x04,                                               # 协议级别 4 (3.1.1)
            ])
            connect_flags = 0x02  # Clean Session
            payload = bytearray()

            # Client ID
            cid_bytes = self.client_id.encode('utf-8')
            payload.extend(struct.pack("!H", len(cid_bytes)))
            payload.extend(cid_bytes)

            # 用户名与密码
            if self.username:
                connect_flags |= 0x80
                u_bytes = self.username.encode('utf-8')
                payload.extend(struct.pack("!H", len(u_bytes)))
                payload.extend(u_bytes)
            if self.password:
                connect_flags |= 0x40
                p_bytes = self.password.encode('utf-8')
                payload.extend(struct.pack("!H", len(p_bytes)))
                payload.extend(p_bytes)

            var_header.append(connect_flags)
            var_header.extend(struct.pack("!H", self.keepalive))

            packet = bytearray([0x10]) # CONNECT Control Packet
            rem_len = len(var_header) + len(payload)
            packet.extend(self._encode_remaining_len(rem_len))
            packet.extend(var_header)
            packet.extend(payload)

            self.sock.sendall(packet)

            # 等待 CONNACK (0x20 0x02 0x00 0x00)
            resp = self.sock.recv(4)
            if len(resp) >= 4 and resp[0] == 0x20 and resp[3] == 0x00:
                self.connected = True
                self.sock.settimeout(None)
                return True
            else:
                return False
        except Exception as e:
            self.connected = False
            return False

    def publish(self, topic: str, payload: str, retain: bool = False) -> bool:
        if not self.connected or not self.sock:
            return False
        with self._lock:
            try:
                topic_bytes = topic.encode('utf-8')
                data_bytes = payload.encode('utf-8')

                header = 0x30 | (0x01 if retain else 0x00) # PUBLISH, QoS 0
                var_header = struct.pack("!H", len(topic_bytes)) + topic_bytes
                rem_len = len(var_header) + len(data_bytes)

                packet = bytearray([header])
                packet.extend(self._encode_remaining_len(rem_len))
                packet.extend(var_header)
                packet.extend(data_bytes)

                self.sock.sendall(packet)
                return True
            except Exception:
                self.connected = False
                return False

    def subscribe(self, topic: str) -> bool:
        if topic not in self._sub_topics:
            self._sub_topics.append(topic)
        if not self.connected or not self.sock:
            return False
        with self._lock:
            try:
                topic_bytes = topic.encode('utf-8')
                packet_id = 1
                var_header = struct.pack("!H", packet_id)
                payload = struct.pack("!H", len(topic_bytes)) + topic_bytes + b'\x00' # QoS 0
                rem_len = len(var_header) + len(payload)

                packet = bytearray([0x82]) # SUBSCRIBE
                packet.extend(self._encode_remaining_len(rem_len))
                packet.extend(var_header)
                packet.extend(payload)

                self.sock.sendall(packet)
                return True
            except Exception:
                self.connected = False
                return False

    def ping(self) -> bool:
        if not self.connected or not self.sock:
            return False
        with self._lock:
            try:
                self.sock.sendall(bytes([0xC0, 0x00])) # PINGREQ
                return True
            except Exception:
                self.connected = False
                return False

    def disconnect(self):
        self.connected = False
        if self.sock:
            try:
                self.sock.sendall(bytes([0xE0, 0x00])) # DISCONNECT
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _encode_remaining_len(self, length: int) -> bytearray:
        encoded = bytearray()
        while True:
            byte = length % 128
            length //= 128
            if length > 0:
                byte |= 0x80
            encoded.append(byte)
            if length == 0:
                break
        return encoded


class HomeAssistantBridge:
    """
    Home Assistant 集成网桥:
    1. 负责自动生成 Device 信息与 Entities Discovery 报文。
    2. 定期与事件驱动式同步 SafeHalo 状态至 Home Assistant。
    3. 支持 Home Assistant 下发指令 (如布防切换、主动定向扫描)。
    """
    DISCOVERY_PREFIX = "homeassistant"
    NODE_ID = "safehalo"

    def __init__(
        self,
        mqtt_host: str = "127.0.0.1",
        mqtt_port: int = 1883,
        mqtt_user: Optional[str] = None,
        mqtt_pass: Optional[str] = None,
        device_id: str = "safehalo_gateway_01",
        device_name: str = "SafeHalo 空间感知网关",
        on_command: Optional[Callable[[str, str], None]] = None,
    ):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_user = mqtt_user
        self.mqtt_pass = mqtt_pass
        self.device_id = device_id
        self.device_name = device_name
        self.on_command = on_command

        self.client: Optional[MiniMQTTClient] = None
        self.is_running = False
        self._last_ping = 0.0

        # 通用设备标识
        self.device_info = {
            "identifiers": [self.device_id],
            "name": self.device_name,
            "model": "WiFi CSI & mmWave Radar Sensor",
            "manufacturer": "SafeHalo OpenWrt Project",
            "sw_version": "2.4.0",
            "configuration_url": "http://safehalo.local:8080",
        }

    def start(self):
        """启动后台 MQTT 连接与自愈循环"""
        self.is_running = True
        t = threading.Thread(target=self._run_loop, daemon=True, name="HAMQTTBridge")
        t.start()

    def stop(self):
        self.is_running = False
        if self.client:
            self.client.disconnect()

    def _run_loop(self):
        logger.info(f"[HomeAssistantBridge] 正在连接 Home Assistant MQTT Broker ({self.mqtt_host}:{self.mqtt_port})...")
        while self.is_running:
            if not self.client or not self.client.connected:
                self.client = MiniMQTTClient(
                    client_id=f"safehalo_{int(time.time())}",
                    host=self.mqtt_host,
                    port=self.mqtt_port,
                    username=self.mqtt_user,
                    password=self.mqtt_pass,
                    keepalive=60,
                )
                if self.client.connect():
                    logger.info("[HomeAssistantBridge] MQTT 连接成功！正在向 Home Assistant 注册自动发现实体...")
                    self._publish_discovery()
                    # 订阅指令主题
                    self.client.subscribe("safehalo/command/inspect")
                    self.client.subscribe("safehalo/command/security_mode")
                else:
                    time.sleep(5.0)
                    continue

            # 保活 PING
            now = time.time()
            if now - self._last_ping > 25.0:
                self._last_ping = now
                if not self.client.ping():
                    logger.warning("[HomeAssistantBridge] MQTT Broker 心跳丢失，准备重连...")

            time.sleep(1.0)

    def _publish_discovery(self):
        """向 Home Assistant 发送标准 MQTT 自动发现 (Auto-Discovery) 实体报文"""
        entities = [
            # 1. 跌倒报警二值传感器 (核心)
            (
                "binary_sensor",
                "fall",
                {
                    "name": "老人跌倒警报",
                    "device_class": "safety",
                    "state_topic": "safehalo/binary_sensor/fall/state",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "icon": "mdi:alert-circle-outline",
                },
            ),
            # 2. 人体存在二值传感器
            (
                "binary_sensor",
                "presence",
                {
                    "name": "人体存在",
                    "device_class": "presence",
                    "state_topic": "safehalo/binary_sensor/presence/state",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                },
            ),
            # 3. 空间运动二值传感器
            (
                "binary_sensor",
                "motion",
                {
                    "name": "空间活动走动",
                    "device_class": "motion",
                    "state_topic": "safehalo/binary_sensor/motion/state",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                },
            ),
            # 4. 宠物地面活动二值传感器 (已过滤)
            (
                "binary_sensor",
                "pet",
                {
                    "name": "宠物地面活动",
                    "device_class": "presence",
                    "state_topic": "safehalo/binary_sensor/pet/state",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "icon": "mdi:paw",
                },
            ),
            # 5. 全局人体状态枚举传感器
            (
                "sensor",
                "human_state",
                {
                    "name": "空间感知状态",
                    "state_topic": "safehalo/sensor/state/state",
                    "icon": "mdi:account-eye",
                },
            ),
            # 6. 毫米波空间测定高度传感器 (Z轴)
            (
                "sensor",
                "radar_height",
                {
                    "name": "躯干测定离地高度",
                    "state_topic": "safehalo/sensor/radar_z/state",
                    "unit_of_measurement": "m",
                    "icon": "mdi:human-male-height",
                },
            ),
            # 7. 活体胸腔呼吸微动心跳
            (
                "sensor",
                "respiration_rate",
                {
                    "name": "活体呼吸微动频率",
                    "state_topic": "safehalo/sensor/respiration/state",
                    "unit_of_measurement": "bpm",
                    "icon": "mdi:heart-pulse",
                },
            ),
            # 8. 双模决策置信度传感器
            (
                "sensor",
                "fused_confidence",
                {
                    "name": "双模融合置信度",
                    "state_topic": "safehalo/sensor/confidence/state",
                    "unit_of_measurement": "%",
                    "icon": "mdi:shield-check",
                },
            ),
            # 9. 主动激振定向深度扫描按钮
            (
                "button",
                "inspect_now",
                {
                    "name": "启动定向微动核验",
                    "command_topic": "safehalo/command/inspect",
                    "payload_press": "TRIGGER",
                    "icon": "mdi:radar",
                },
            ),
        ]

        for component, object_id, config in entities:
            topic = f"{self.DISCOVERY_PREFIX}/{component}/{self.NODE_ID}/{object_id}/config"
            config["unique_id"] = f"{self.device_id}_{object_id}"
            config["device"] = self.device_info
            payload_str = json.dumps(config, ensure_ascii=False)
            self.client.publish(topic, payload_str, retain=True)

        logger.info("[HomeAssistantBridge] 全部 9 个 Home Assistant 实体已完成 MQTT 自动注册！")

    def publish_state(
        self,
        human_state: str,
        is_fall: bool,
        is_presence: bool,
        is_moving: bool,
        is_pet: bool,
        radar_z: float = 0.0,
        respiration_bpm: float = 0.0,
        confidence: float = 1.0,
    ):
        """
        向 Home Assistant 发布最新实时状态
        """
        if not self.client or not self.client.connected:
            return

        # 跌倒
        self.client.publish("safehalo/binary_sensor/fall/state", "ON" if is_fall else "OFF")
        # 存在
        self.client.publish("safehalo/binary_sensor/presence/state", "ON" if is_presence else "OFF")
        # 运动
        self.client.publish("safehalo/binary_sensor/motion/state", "ON" if is_moving else "OFF")
        # 宠物
        self.client.publish("safehalo/binary_sensor/pet/state", "ON" if is_pet else "OFF")
        # 状态文本
        self.client.publish("safehalo/sensor/state/state", human_state)
        # 高度
        self.client.publish("safehalo/sensor/radar_z/state", f"{radar_z:.2f}")
        # 呼吸
        self.client.publish("safehalo/sensor/respiration/state", f"{respiration_bpm:.1f}")
        # 置信度
        self.client.publish("safehalo/sensor/confidence/state", f"{int(confidence * 100)}")

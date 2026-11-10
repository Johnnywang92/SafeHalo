"""
SafeHalo 毫米波雷达数据解析器与协议定义 (mmwave_parser.py)
预留多品牌/多协议毫米波雷达 (24GHz / 60GHz FMCW) 的数据入口。

支持协议:
1. HLK-LD2410 (24GHz 人体存在与距离微动雷达，智能家居最普及型号)
2. HLK-LD2450 (24GHz 轨迹跟踪雷达，上报最多 3 个目标的 X, Y 坐标与速度)
3. Seeed Studio MR60FDA1 / 60GHz 跌倒姿态雷达 (专业跌倒检测，上报高度、姿态与呼吸微动)
4. Generic JSON 雷达数据流 (用于网络 Socket、MQTT、OpenWrt 边缘网关适配)

纯 Python 标准库实现，零第三方依赖。
"""

import enum
import json
import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union


class RadarPosture(enum.Enum):
    """人体空间姿态枚举"""
    UNKNOWN = "unknown"
    STAND = "stand"           # 站立
    SIT = "sit"               # 坐姿
    LYING_FLOOR = "lying"     # 倒地/平躺
    MOVING = "moving"         # 移动中
    EMPTY = "empty"           # 无人


@dataclass
class RadarTarget:
    """单个雷达跟踪目标"""
    target_id: int = 1
    x: float = 0.0          # 水平横向坐标 (米), 负为左, 正为右
    y: float = 0.0          # 纵深距离坐标 (米), 距雷达前向距离
    z: float = 0.0          # 垂直高度坐标 (米), 离地高度或相对于雷达垂高
    speed: float = 0.0      # 径向速度 (米/秒), 正为远离, 负为靠近
    distance: float = 0.0   # 目标距雷达绝对几何距离 (米)
    energy: float = 0.0     # 目标反射强度 / 能量门限 (0 - 100)
    posture: RadarPosture = RadarPosture.UNKNOWN
    respiration_bpm: float = 0.0  # 呼吸心率微动 (次/分, 0 表示未检测到)


@dataclass
class RadarFrame:
    """毫米波雷达单帧完整结构"""
    timestamp: float
    sensor_id: str                      # 传感器标识，如 "radar_living_room"
    protocol: str                       # 协议名称: "ld2410", "ld2450", "mr60fda1", "json"
    frame_id: int = 0
    human_presence: bool = False        # 是否有人体存在
    fall_flag: bool = False             # 硬件雷达内置算法跌倒报警标志
    motion_state: str = "stationary"    # "motion", "stationary", "empty"
    targets: List[RadarTarget] = field(default_factory=list)
    raw_payload: Optional[bytes] = None


class BaseRadarParser:
    """雷达解析器抽象基类"""
    def parse_bytes(self, data: bytes) -> List[RadarFrame]:
        raise NotImplementedError

    def parse_line(self, line: str) -> Optional[RadarFrame]:
        raise NotImplementedError


class LD2410Parser(BaseRadarParser):
    """
    HLK-LD2410 24GHz 人体存在感应雷达解析器
    报文格式 (基本数据帧):
    帧头: FD FC FB FA
    帧长: 2 字节 (小端)
    数据类型: 0x01 / 0x02
    目标状态:
      0x00: 无人
      0x01: 运动目标
      0x02: 静止微动目标
      0x03: 运动与静止目标同时存在
    运动目标距离: 2 字节 (cm)
    运动目标能量: 1 字节 (0-100)
    静止目标距离: 2 字节 (cm)
    静止目标能量: 1 字节 (0-100)
    探测距离: 2 字节
    帧尾: 04 03 02 01
    """
    FRAME_HEADER = bytes([0xFD, 0xFC, 0xFB, 0xFA])
    FRAME_TAIL = bytes([0x04, 0x03, 0x02, 0x01])

    def __init__(self, sensor_id: str = "ld2410_radar"):
        self.sensor_id = sensor_id
        self._buffer = bytearray()
        self.frame_counter = 0

    def parse_bytes(self, chunk: bytes) -> List[RadarFrame]:
        self._buffer.extend(chunk)
        frames: List[RadarFrame] = []

        while len(self._buffer) >= 14:
            # 寻找帧头
            header_idx = self._buffer.find(self.FRAME_HEADER)
            if header_idx == -1:
                # 丢弃非头部残留 (保留最后 3 个字节以防帧头跨包截断)
                if len(self._buffer) > 3:
                    self._buffer = self._buffer[-3:]
                break

            if header_idx > 0:
                self._buffer = self._buffer[header_idx:]

            if len(self._buffer) < 8:
                break

            # 帧内数据长度 (2 字节小端，不含头尾和长度本身)
            data_len = struct.unpack("<H", self._buffer[4:6])[0]
            total_frame_len = 4 + 2 + data_len + 4

            if len(self._buffer) < total_frame_len:
                # 等待接收完整帧
                break

            frame_bytes = bytes(self._buffer[:total_frame_len])
            # 校验帧尾
            if frame_bytes[-4:] == self.FRAME_TAIL:
                frame = self._decode_frame(frame_bytes[6:-4], frame_bytes)
                if frame:
                    frames.append(frame)

            # 移出已消费的帧
            self._buffer = self._buffer[total_frame_len:]

        return frames

    def _decode_frame(self, payload: bytes, raw: bytes) -> Optional[RadarFrame]:
        if len(payload) < 8:
            return None

        self.frame_counter += 1
        data_type = payload[0]  # 通常为 0x01 (工程模式) 或 0x02 (目标数据)
        target_state = payload[1]

        presence = target_state != 0x00
        motion_state = "empty"
        if target_state == 0x01:
            motion_state = "motion"
        elif target_state == 0x02:
            motion_state = "stationary"
        elif target_state == 0x03:
            motion_state = "motion_and_stationary"

        move_dist_cm = struct.unpack("<H", payload[2:4])[0] if len(payload) >= 4 else 0
        move_energy = payload[4] if len(payload) >= 5 else 0
        stat_dist_cm = struct.unpack("<H", payload[5:7])[0] if len(payload) >= 7 else 0
        stat_energy = payload[7] if len(payload) >= 8 else 0

        targets: List[RadarTarget] = []
        if presence:
            # 综合计算主目标距离与能量
            best_dist_m = (stat_dist_cm if target_state == 0x02 else move_dist_cm) / 100.0
            best_energy = max(move_energy, stat_energy)
            t = RadarTarget(
                target_id=1,
                x=0.0,
                y=best_dist_m,
                z=1.2 if motion_state == "motion" else 0.8,
                speed=0.0,
                distance=best_dist_m,
                energy=float(best_energy),
                posture=RadarPosture.MOVING if motion_state == "motion" else RadarPosture.SIT,
            )
            targets.append(t)

        return RadarFrame(
            timestamp=time.time(),
            sensor_id=self.sensor_id,
            protocol="ld2410",
            frame_id=self.frame_counter,
            human_presence=presence,
            fall_flag=False,
            motion_state=motion_state,
            targets=targets,
            raw_payload=raw,
        )


class LD2450Parser(BaseRadarParser):
    """
    HLK-LD2450 24GHz 多目标人体轨迹跟踪雷达解析器
    报文格式:
    帧头: AA FF 03 00
    每帧包含 3 个目标数据槽 (每个目标 8 字节):
      - X 坐标: int16 (单位 mm, 补码符号, 负数为左侧, 正数为右侧)
      - Y 坐标: int16 (单位 mm, 前向纵深)
      - 速度: int16 (单位 cm/s, 负为靠近, 正为远离)
      - 距离分辨率/门限: uint16 (mm)
    帧尾: 55 CC
    总长度: 4 (头) + 3 * 8 (目标) + 2 (尾) = 30 字节
    """
    FRAME_HEADER = bytes([0xAA, 0xFF, 0x03, 0x00])
    FRAME_TAIL = bytes([0x55, 0xCC])
    FRAME_LEN = 30

    def __init__(self, sensor_id: str = "ld2450_radar"):
        self.sensor_id = sensor_id
        self._buffer = bytearray()
        self.frame_counter = 0

    def parse_bytes(self, chunk: bytes) -> List[RadarFrame]:
        self._buffer.extend(chunk)
        frames: List[RadarFrame] = []

        while len(self._buffer) >= self.FRAME_LEN:
            idx = self._buffer.find(self.FRAME_HEADER)
            if idx == -1:
                if len(self._buffer) > 4:
                    self._buffer = self._buffer[-4:]
                break

            if idx > 0:
                self._buffer = self._buffer[idx:]

            if len(self._buffer) < self.FRAME_LEN:
                break

            frame_bytes = bytes(self._buffer[:self.FRAME_LEN])
            if frame_bytes[-2:] == self.FRAME_TAIL:
                frame = self._decode_frame(frame_bytes)
                if frame:
                    frames.append(frame)

            self._buffer = self._buffer[self.FRAME_LEN:]

        return frames

    def _decode_frame(self, raw: bytes) -> Optional[RadarFrame]:
        self.frame_counter += 1
        targets: List[RadarTarget] = []

        # 3 个目标插槽，从第 4 字节开始
        offset = 4
        for tid in range(1, 4):
            x_raw, y_raw, speed_raw, dist_res = struct.unpack("<hhhH", raw[offset : offset + 8])
            offset += 8

            # LD2450 坐标使用最高位标识符号 (或标准 int16)
            # mm 转米
            x_m = x_raw / 1000.0
            y_m = y_raw / 1000.0
            speed_mps = speed_raw / 100.0  # cm/s -> m/s

            # 目标有效性过滤: y_m > 0 且在合理室内范围 (0.1m - 8.0m)
            if 0.1 <= y_m <= 8.0:
                geom_dist = (x_m ** 2 + y_m ** 2) ** 0.5
                posture = RadarPosture.MOVING if abs(speed_mps) > 0.15 else RadarPosture.SIT
                t = RadarTarget(
                    target_id=tid,
                    x=round(x_m, 3),
                    y=round(y_m, 3),
                    z=1.2,  # 2D 雷达无 Z 轴，设置默认躯干标称高度
                    speed=round(speed_mps, 3),
                    distance=round(geom_dist, 3),
                    energy=float(dist_res),
                    posture=posture,
                )
                targets.append(t)

        presence = len(targets) > 0
        motion_state = "empty"
        if presence:
            is_moving = any(abs(t.speed) > 0.15 for t in targets)
            motion_state = "motion" if is_moving else "stationary"

        return RadarFrame(
            timestamp=time.time(),
            sensor_id=self.sensor_id,
            protocol="ld2450",
            frame_id=self.frame_counter,
            human_presence=presence,
            fall_flag=False,
            motion_state=motion_state,
            targets=targets,
            raw_payload=raw,
        )


class MR60FDA1Parser(BaseRadarParser):
    """
    Seeed MR60FDA1 / 60GHz 跌倒与人体姿态雷达解析器
    报文格式 (标准 UART TLV 帧):
    帧头: 53 59 (ASCII 'SY')
    控制字: 1 字节 (0x80: 存在, 0x83: 跌倒检测, 0x84: 呼吸心跳)
    命令字: 1 字节
    数据长: 2 字节 (大端)
    数据载荷: N 字节
    校验和: 1 字节 (和校验)
    帧尾: 54 43 (ASCII 'TC')
    """
    FRAME_HEADER = bytes([0x53, 0x59])
    FRAME_TAIL = bytes([0x54, 0x43])

    def __init__(self, sensor_id: str = "mr60fda1_radar"):
        self.sensor_id = sensor_id
        self._buffer = bytearray()
        self.frame_counter = 0

    def parse_bytes(self, chunk: bytes) -> List[RadarFrame]:
        self._buffer.extend(chunk)
        frames: List[RadarFrame] = []

        while len(self._buffer) >= 8:
            idx = self._buffer.find(self.FRAME_HEADER)
            if idx == -1:
                if len(self._buffer) > 2:
                    self._buffer = self._buffer[-2:]
                break

            if idx > 0:
                self._buffer = self._buffer[idx:]

            if len(self._buffer) < 6:
                break

            # 数据长度 (大端)
            data_len = (self._buffer[4] << 8) | self._buffer[5]
            total_len = 2 + 1 + 1 + 2 + data_len + 1 + 2

            if len(self._buffer) < total_len:
                break

            frame_bytes = bytes(self._buffer[:total_len])
            if frame_bytes[-2:] == self.FRAME_TAIL:
                frame = self._decode_frame(frame_bytes)
                if frame:
                    frames.append(frame)

            self._buffer = self._buffer[total_len:]

        return frames

    def _decode_frame(self, raw: bytes) -> Optional[RadarFrame]:
        self.frame_counter += 1
        ctrl = raw[2]
        cmd = raw[3]
        data_len = (raw[4] << 8) | raw[5]
        payload = raw[6 : 6 + data_len]

        presence = True
        fall_flag = False
        posture = RadarPosture.STAND
        respiration_bpm = 0.0
        z_height = 1.6  # 默认站姿高度

        # 控制字 0x83: 跌倒检测信息
        if ctrl == 0x83:
            if cmd == 0x01:  # 跌倒报警
                # 0x00: 正常, 0x01: 跌倒发生
                state_code = payload[0] if len(payload) >= 1 else 0
                fall_flag = (state_code == 0x01)
                posture = RadarPosture.LYING_FLOOR if fall_flag else RadarPosture.STAND
                z_height = 0.2 if fall_flag else 1.6
            elif cmd == 0x05:  # 人体体位高度/姿态
                # 0x01: 站立, 0x02: 坐着, 0x03: 躺平
                posture_code = payload[0] if len(payload) >= 1 else 1
                if posture_code == 0x03:
                    posture = RadarPosture.LYING_FLOOR
                    z_height = 0.25
                elif posture_code == 0x02:
                    posture = RadarPosture.SIT
                    z_height = 0.85
                else:
                    posture = RadarPosture.STAND
                    z_height = 1.65

        # 控制字 0x80: 人体存在与距离信息
        elif ctrl == 0x80:
            if cmd == 0x01:  # 存在状态 (0: 无人, 1: 有人)
                presence = (payload[0] == 0x01) if len(payload) >= 1 else False
                if not presence:
                    posture = RadarPosture.EMPTY

        # 控制字 0x84: 呼吸体征监测
        elif ctrl == 0x84:
            if cmd == 0x02 and len(payload) >= 1:
                respiration_bpm = float(payload[0])  # 呼吸频率 (次/分)

        target = RadarTarget(
            target_id=1,
            x=0.0,
            y=2.0,
            z=z_height,
            speed=0.0,
            distance=2.0,
            energy=85.0 if presence else 0.0,
            posture=posture,
            respiration_bpm=respiration_bpm,
        )

        return RadarFrame(
            timestamp=time.time(),
            sensor_id=self.sensor_id,
            protocol="mr60fda1",
            frame_id=self.frame_counter,
            human_presence=presence,
            fall_flag=fall_flag,
            motion_state="stationary" if posture != RadarPosture.MOVING else "motion",
            targets=[target] if presence else [],
            raw_payload=raw,
        )


class JsonRadarParser(BaseRadarParser):
    """
    通用 JSON 格式雷达数据流解析器 (网络 Socket、MQTT 或 Mock 测试)
    支持格式示例:
    {
      "sensor_id": "radar_hall",
      "timestamp": 1710000000.123,
      "fall_flag": false,
      "targets": [
        {"id": 1, "x": 0.5, "y": 2.1, "z": 0.28, "speed": 0.02, "posture": "lying", "bpm": 16.5}
      ]
    }
    """
    def __init__(self, default_sensor_id: str = "json_radar"):
        self.default_sensor_id = default_sensor_id
        self.frame_counter = 0

    def parse_line(self, line: str) -> Optional[RadarFrame]:
        line = line.strip()
        if not line or not line.startswith("{"):
            return None

        try:
            doc = json.loads(line)
        except Exception:
            return None

        self.frame_counter += 1
        sensor_id = doc.get("sensor_id", self.default_sensor_id)
        ts = doc.get("timestamp", time.time())
        fall_flag = bool(doc.get("fall_flag", False))

        targets: List[RadarTarget] = []
        for t_doc in doc.get("targets", []):
            posture_str = t_doc.get("posture", "unknown").lower()
            try:
                posture = RadarPosture(posture_str)
            except ValueError:
                posture = RadarPosture.UNKNOWN

            x = float(t_doc.get("x", 0.0))
            y = float(t_doc.get("y", 0.0))
            z = float(t_doc.get("z", 1.2))
            speed = float(t_doc.get("speed", 0.0))
            dist = float(t_doc.get("distance", (x**2 + y**2 + z**2)**0.5))

            targets.append(RadarTarget(
                target_id=int(t_doc.get("id", 1)),
                x=x,
                y=y,
                z=z,
                speed=speed,
                distance=dist,
                energy=float(t_doc.get("energy", 80.0)),
                posture=posture,
                respiration_bpm=float(t_doc.get("bpm", 0.0)),
            ))

        presence = len(targets) > 0
        motion_state = "empty"
        if presence:
            motion_state = "motion" if any(abs(t.speed) > 0.15 for t in targets) else "stationary"

        return RadarFrame(
            timestamp=ts,
            sensor_id=sensor_id,
            protocol="json",
            frame_id=self.frame_counter,
            human_presence=presence,
            fall_flag=fall_flag,
            motion_state=motion_state,
            targets=targets,
            raw_payload=line.encode("utf-8"),
        )


class UnifiedRadarParser:
    """
    统一雷达输入分发器:
    自动辨识字节流或文本流，并路由至对应雷达解析器。
    """
    def __init__(self, default_protocol: str = "auto", sensor_id: str = "radar_node_1"):
        self.sensor_id = sensor_id
        self.default_protocol = default_protocol
        self.ld2410 = LD2410Parser(sensor_id=sensor_id)
        self.ld2450 = LD2450Parser(sensor_id=sensor_id)
        self.mr60fda1 = MR60FDA1Parser(sensor_id=sensor_id)
        self.json_parser = JsonRadarParser(default_sensor_id=sensor_id)

    def feed_bytes(self, chunk: bytes, protocol: Optional[str] = None) -> List[RadarFrame]:
        proto = protocol or self.default_protocol
        if proto == "ld2410":
            return self.ld2410.parse_bytes(chunk)
        elif proto == "ld2450":
            return self.ld2450.parse_bytes(chunk)
        elif proto == "mr60fda1":
            return self.mr60fda1.parse_bytes(chunk)
        else:
            # 自动探测帧头
            if LD2410Parser.FRAME_HEADER in chunk:
                return self.ld2410.parse_bytes(chunk)
            elif LD2450Parser.FRAME_HEADER in chunk:
                return self.ld2450.parse_bytes(chunk)
            elif MR60FDA1Parser.FRAME_HEADER in chunk:
                return self.mr60fda1.parse_bytes(chunk)
            return []

    def feed_line(self, line: str) -> Optional[RadarFrame]:
        # 优先尝试 JSON
        if line.strip().startswith("{"):
            return self.json_parser.parse_line(line)
        return None

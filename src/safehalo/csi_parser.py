"""
SafeHalo CSI 报文解析器 (csi_parser.py)
用于解析 ESP32 / ESP32-S3 esp-csi 固件输出的串口 CSI 文本行。
"""

import math
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

@dataclass
class CSIPacket:
    raw_type: str = ""
    seq: int = 0
    mac: str = ""
    rssi: int = 0
    channel: int = 0
    local_timestamp: int = 0
    noise_floor: int = 0
    subcarrier_count: int = 0
    amplitudes: List[float] = field(default_factory=list)
    phases: List[float] = field(default_factory=list)
    raw_i: List[int] = field(default_factory=list)
    raw_q: List[int] = field(default_factory=list)

class CSIParser:
    """
    负责从 ESP32-S3 串口日志中解析 CSI_DATA 报文。
    典型格式:
    CSI_DATA,type,seq,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,len,first_word,"[12, -4, 15, -2, ...]"
    """

    DATA_ARRAY_REGEX = re.compile(r"\[([0-9\s,\-]+)\]")

    def __init__(self, imaginary_first: bool = True):
        """
        :param imaginary_first: 乐鑫 esp-csi 规范中，ESP32-S3 通常第一个字节为虚部(imag)，第二个字节为实部(real)。
        """
        self.imaginary_first = imaginary_first

    def parse_line(self, line: str) -> Optional[CSIPacket]:
        line = line.strip()
        if not line or not ("CSI_DATA" in line):
            return None

        # 匹配括号内的原始字节数组
        match = self.DATA_ARRAY_REGEX.search(line)
        if not match:
            return None

        data_str = match.group(1)
        try:
            raw_vals = [int(v.strip()) for v in data_str.split(",") if v.strip()]
        except ValueError:
            return None

        if len(raw_vals) < 4:
            return None

        # 截取前半部分的元数据 (用逗号分割，排除数组部分)
        prefix = line[:match.start()].strip()
        parts = [p.strip() for p in prefix.split(",") if p.strip()]

        packet = CSIPacket()
        # 兼容不同固件版本的元数据位置
        if len(parts) >= 5:
            packet.raw_type = parts[0]
            try:
                packet.seq = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                packet.mac = parts[3] if len(parts) > 3 else ""
                packet.rssi = int(parts[4]) if len(parts) > 4 and parts[4].lstrip("-").isdigit() else 0
            except Exception:
                pass

        # 查找 noise_floor, channel, timestamp (如果存在)
        for idx, p in enumerate(parts):
            if idx == 15 and p.lstrip("-").isdigit():
                packet.noise_floor = int(p)
            elif idx == 17 and p.isdigit():
                packet.channel = int(p)
            elif idx == 19 and p.isdigit():
                packet.local_timestamp = int(p)

        # 解码 I/Q 分量与幅值/相位
        # 每两个字节组成一个子载波: (imag, real) 或 (real, imag)
        num_subcarriers = len(raw_vals) // 2
        packet.subcarrier_count = num_subcarriers
        packet.amplitudes = [0.0] * num_subcarriers
        packet.phases = [0.0] * num_subcarriers
        packet.raw_i = [0] * num_subcarriers
        packet.raw_q = [0] * num_subcarriers

        for i in range(num_subcarriers):
            v1 = raw_vals[2 * i]
            v2 = raw_vals[2 * i + 1]

            if self.imaginary_first:
                imag, real = v1, v2
            else:
                real, imag = v1, v2

            packet.raw_i[i] = real
            packet.raw_q[i] = imag

            # 计算幅值 A = sqrt(I^2 + Q^2)
            amp = math.sqrt(real * real + imag * imag)
            packet.amplitudes[i] = amp

            # 计算相位 Phase = atan2(Q, I)
            phase = math.atan2(imag, real)
            packet.phases[i] = phase

        return packet

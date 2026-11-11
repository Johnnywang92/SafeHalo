"""
SafeHalo 统一日志与事件审计模块 (logger.py)
专为边缘路由器 (OpenWrt) 与本地运行环境设计:
1. 双重输出: 格式化控制台输出 + 自动轮转日志文件 (RotatingFileHandler)，严格限制文件大小防路由器 Flash 存满。
2. 结构化事件日志 (JSONL): 记录关键安全事件 (状态切换、跌倒确诊、定向核验体征、宠物过滤、安防告警)。
3. 环形内存缓冲: 保留最近事件供 Web 控制台实时拉取与下载。
"""

import os
import json
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from collections import deque
from typing import Dict, List, Optional, Any

# 默认日志目录
LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))

class SafeHaloLogger:
    _instance = None

    def __init__(self, log_dir: str = LOG_DIR, max_bytes: int = 2 * 1024 * 1024, backup_count: int = 5):
        """
        :param log_dir: 日志存放目录
        :param max_bytes: 单个日志文件最大大小 (默认 2MB，轮转备份)
        :param backup_count: 轮转保留备份数 (默认 5 个，总占用最多 10MB)
        """
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)

        self.log_file = os.path.join(self.log_dir, "safehalo.log")
        self.events_file = os.path.join(self.log_dir, "events.jsonl")

        # 内存中保留最近 200 条结构化事件，供 Web 快速查询
        self.recent_events: deque = deque(maxlen=200)
        self._preload_recent_events(limit=50)

        # 初始化 Python 标准 logging
        self.logger = logging.getLogger("SafeHalo")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        # 避免重复添加 Handler
        if not self.logger.handlers:
            # 1. 轮转文件 Handler
            file_handler = RotatingFileHandler(
                self.log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8"
            )
            file_formatter = logging.Formatter(
                "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            )
            file_handler.setFormatter(file_formatter)
            self.logger.addHandler(file_handler)

            # 2. 控制台 Handler (输出格式精简明了)
            console_handler = logging.StreamHandler()
            console_formatter = logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(message)s",
                datefmt="%H:%M:%S"
            )
            console_handler.setFormatter(console_formatter)
            self.logger.addHandler(console_handler)

        self.info(f"SafeHalo 日志系统已就绪，日志目录: {self.log_dir}")

    def _preload_recent_events(self, limit: int = 50):
        """服务重启时，从本地 events.jsonl 文件预加载最近历史事件"""
        if not os.path.exists(self.events_file):
            return
        try:
            with open(self.events_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                for line in lines[-limit:]:
                    line = line.strip()
                    if line:
                        self.recent_events.append(json.loads(line))
        except Exception as e:
            self.error(f"预加载历史事件失败: {e}")

    @classmethod
    def get_instance(cls, log_dir: str = LOG_DIR):
        if cls._instance is None:
            cls._instance = SafeHaloLogger(log_dir)
        return cls._instance

    # 基础日志封装
    def info(self, msg: str):
        self.logger.info(msg)

    def warning(self, msg: str):
        self.logger.warning(msg)

    def error(self, msg: str):
        self.logger.error(msg)

    def critical(self, msg: str):
        self.logger.critical(msg)

    def debug(self, msg: str):
        self.logger.debug(msg)

    # 结构化安全与审计事件记录
    def log_event(self, event_type: str, message: str, level: str = "INFO", data: Optional[Dict[str, Any]] = None):
        """
        记录一条结构化安全事件并落盘到 events.jsonl
        :param event_type: 事件类型 (如 STATE_CHANGE, FALL_SUSPECT, FALL_CONFIRMED, PET_FILTER, SECURITY_ALARM, INSPECTION)
        :param message: 用户可读事件摘要
        :param level: 日志级别 (INFO, WARNING, ALERT, CRITICAL)
        :param data: 结构化元数据 (如方差、差分能量、呼吸频率 bpm 等)
        """
        now = datetime.now()
        event = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "time_short": now.strftime("%H:%M:%S"),
            "type": event_type,
            "level": level,
            "message": message,
            "data": data or {}
        }

        # 1. 存入内存队列
        self.recent_events.append(event)

        # 2. 同步写入文本常规日志
        log_msg = f"[{event_type}] {message}"
        if level in ("ALERT", "CRITICAL"):
            self.logger.critical(log_msg)
        elif level == "WARNING":
            self.logger.warning(log_msg)
        else:
            self.logger.info(log_msg)

        # 3. 追加写入 events.jsonl 审计文件
        try:
            with open(self.events_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            self.logger.error(f"写入 events.jsonl 失败: {e}")

    # 便捷语义化事件方法
    def log_state_change(self, old_state: str, new_state: str, details: str = "", metrics: Optional[Dict] = None):
        msg = f"状态切换: {old_state} -> {new_state}"
        if details:
            msg += f" ({details})"
        level = "WARNING" if "FALL" in new_state else "INFO"
        self.log_event("STATE_CHANGE", msg, level=level, data={"old": old_state, "new": new_state, "metrics": metrics or {}})

    def log_fall_suspect(self, spike_ratio: float, diff_energy: float, details: str = ""):
        self.log_event(
            "FALL_SUSPECT",
            f"初筛疑似跌倒冲击！(尖峰比: {spike_ratio:.2f}, 能量: {diff_energy:.5f}) - {details}",
            level="WARNING",
            data={"spike_ratio": spike_ratio, "diff_energy": diff_energy}
        )

    def log_inspection_progress(self, stage: str, details: str, bpm: Optional[float] = None):
        data = {"stage": stage, "details": details}
        if bpm:
            data["bpm"] = bpm
        self.log_event("INSPECTION", f"定向排查进展: [{stage}] {details}", level="INFO", data=data)

    def log_object_watch(self, phase: str, details: str, peaks: Optional[int] = None,
                         intervals: Optional[int] = None, cv: Optional[float] = None):
        """第二阶段物品确认观察（开关开启时才产生事件）。"""
        data = {"phase": phase, "details": details}
        if peaks is not None:
            data["peaks"] = peaks
        if intervals is not None:
            data["intervals"] = intervals
        if cv is not None:
            data["cv"] = cv
        self.log_event("OBJECT_WATCH", f"物品确认观察: [{phase}] {details}", level="INFO", data=data)

    def log_fall_confirmed(self, details: str, bpm: float, confidence: float):
        self.log_event(
            "FALL_CONFIRMED",
            f"🚨 确诊老人跌倒受困！地面呼吸 {bpm:.0f} bpm, 置信度 {confidence*100:.0f}%, 未见起身！",
            level="CRITICAL",
            data={"bpm": bpm, "confidence": confidence, "details": details}
        )

    def log_pet_activity(self, spr: float, diff_energy: float):
        self.log_event(
            "PET_FILTER",
            f"🐾 识别到地面宠物活动，已自动抑制误报 (载波扰动比 SPR: {spr*100:.1f}%)",
            level="INFO",
            data={"spr": spr, "diff_energy": diff_energy}
        )

    def log_security_intrusion(self, mode: str, diff_energy: float):
        self.log_event(
            "SECURITY_INTRUSION",
            f"🚨【安防告警】{mode} 模式下检测到异常入侵走动！(能量: {diff_energy:.5f})",
            level="CRITICAL",
            data={"mode": mode, "diff_energy": diff_energy}
        )

    # 供 Web API 查询与下载接口
    def get_recent_events(self, limit: int = 50, event_type: Optional[str] = None) -> List[Dict]:
        """获取最近事件列表 (按时间倒序)"""
        events = list(self.recent_events)
        if event_type and event_type != "ALL":
            events = [e for e in events if e["type"] == event_type]
        return list(reversed(events))[:limit]

    def get_log_tail(self, lines: int = 100) -> List[str]:
        """获取 safehalo.log 最后 N 行"""
        if not os.path.exists(self.log_file):
            return ["(暂无日志记录)"]
        try:
            with open(self.log_file, "r", encoding="utf-8", errors="ignore") as f:
                all_lines = f.readlines()
                return [line.rstrip() for line in all_lines[-lines:]]
        except Exception as e:
            return [f"读取日志文件失败: {e}"]

    def clear_logs(self):
        """清空日志文件与内存缓存"""
        self.recent_events.clear()
        try:
            open(self.log_file, "w").close()
            open(self.events_file, "w").close()
            self.info("日志与事件记录已手动清空。")
        except Exception as e:
            self.error(f"清空日志失败: {e}")

# 全局单例
logger = SafeHaloLogger.get_instance()

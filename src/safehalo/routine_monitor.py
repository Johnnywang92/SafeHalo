"""
SafeHalo 老人日常作息画像与异常偏离通报引擎 (routine_monitor.py)
专为独居老人与银发居家看护设计:

核心功能:
1. 日常作息规律自适应画像 (Circadian Routine Profiling):
   - 自动记录老人每日关键作息时序: 晨间起床时间 (Wake-up)、夜间入睡时间 (Bedtime)、日间活动动量、夜间起夜频次。
   - 动态学习 7 日滚动加权平均值 (EMA)，自动适应老人的个人作息习惯。
2. 异常偏离多维自动判别与通报 (Deviation Anomaly Detection):
   - 场景 A【晨间推迟/不起床预警】: 超过老人家常起时间 1.5 小时仍未见室内活动，防范卧床猝发疾病或昏迷。
   - 场景 B【日间长时间静止无活动预警】: 日间在室内连续超过 3.5 小时无任何有效走动（排除夜间睡眠），防范白天意外休克。
   - 场景 C【夜间频繁起夜/异动预警】: 深夜频繁走动超过阈值 (如一晚 > 4 次)，预警泌尿系统感染、跌倒高风险或夜间谵妄。
   - 场景 D【长时间离家未归预警】: 老人外出后超过预设安全时长 (如 > 5 小时) 仍未归家，防范认知障碍走失。
   - 场景 E【深夜严重未眠】: 凌晨 2 点以后仍在频繁翻动或大幅活动，预警严重失眠或突发身体不适。
3. 历史作息落盘与全通道告警通报:
   - 自动生成 `logs/routine_history.jsonl` 与 `logs/routine_profile.json`。
   - 异常触发时联动微信推送 (PushPlus / Server酱)、Webhook、Home Assistant 传感器。

纯 Python 标准库实现，零第三方依赖。
"""

import enum
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .logger import logger
from .detector import HumanState


class RoutineAnomalyType(enum.Enum):
    """作息偏离异常类型"""
    WAKEUP_DELAYED = "WAKEUP_DELAYED"                   # 晨间过晚/未起床预警
    PROLONGED_INACTIVITY = "PROLONGED_INACTIVITY"       # 日间异常长时间静止无活动
    NIGHT_RESTLESSNESS = "NIGHT_RESTLESSNESS"           # 夜间频繁起夜/走动
    IRREGULAR_LATE_SLEEP = "IRREGULAR_LATE_SLEEP"       # 深夜异常未眠
    PROLONGED_OUTING = "PROLONGED_OUTING"               # 长时间离家未归 (防走失)


@dataclass
class RoutineAnomalyEvent:
    """单条偏离预警事件"""
    anomaly_type: RoutineAnomalyType
    title: str
    description: str
    severity: str                           # "WARNING" / "ALERT"
    timestamp: float
    time_str: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RoutineProfileConfig:
    """作息基准与告警阈值配置"""
    typical_wake_hour: float = 7.5          # 典型起床时间: 07:30
    wake_delay_tolerance_hours: float = 1.5 # 允许晨间推迟浮动: 1.5 小时 (即 09:00 前必须起床)
    typical_bed_hour: float = 22.5          # 典型入睡时间: 22:30
    max_daytime_inactivity_hours: float = 3.5 # 日间最大连续静止时长 (小时)
    max_night_restless_count: int = 4       # 夜间最大允许起夜/走动频次
    max_outing_hours: float = 5.0           # 外出最大安全归家时长 (小时)
    auto_learn_enabled: bool = True         # 是否启用 7 天自适应画像学习


@dataclass
class TodayCircadianMetrics:
    """当天作息指标快照"""
    date_str: str = ""
    wake_time_str: Optional[str] = None     # 实际起床时间，如 "07:15"
    bed_time_str: Optional[str] = None      # 昨夜入睡时间，如 "22:40"
    woken_up: bool = False                  # 今天是否已起床
    went_to_bed: bool = False               # 今夜是否已入睡
    total_active_seconds: float = 0.0       # 累计走动/大动作秒数
    night_restless_count: int = 0           # 昨夜至今晨起夜频次
    hourly_activity: List[int] = field(default_factory=lambda: [0] * 24) # 24 小时活跃频次分布
    active_anomalies: List[Dict[str, Any]] = field(default_factory=list)


class RoutineMonitorEngine:
    """日常作息监测与异常偏离诊断引擎"""

    def __init__(
        self,
        config: Optional[RoutineProfileConfig] = None,
        storage_dir: Optional[str] = None,
        on_anomaly: Optional[Callable[[RoutineAnomalyEvent], None]] = None,
    ):
        self.config = config or RoutineProfileConfig()
        self.storage_dir = storage_dir or os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "logs")
        )
        os.makedirs(self.storage_dir, exist_ok=True)
        self.profile_path = os.path.join(self.storage_dir, "routine_profile.json")
        self.history_path = os.path.join(self.storage_dir, "routine_history.jsonl")

        self.on_anomaly = on_anomaly
        self._lock = threading.Lock()

        # 加载持久化配置
        self._load_profile()

        # 运行期状态
        self.current_date = datetime.now().strftime("%Y-%m-%d")
        self.metrics = TodayCircadianMetrics(date_str=self.current_date)

        # 内部时序记录
        self.last_motion_time: float = time.time()
        self.left_home_time: Optional[float] = None
        self.is_currently_home: bool = True
        self.last_night_motion_time: float = 0.0

        # 防重复报警抑制记录: {anomaly_type_name: last_alert_time}
        self.cooldown_map: Dict[str, float] = {}

    def _load_profile(self):
        """加载已学习的作息偏好基线"""
        if os.path.exists(self.profile_path):
            try:
                with open(self.profile_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.config.typical_wake_hour = data.get("typical_wake_hour", self.config.typical_wake_hour)
                    self.config.typical_bed_hour = data.get("typical_bed_hour", self.config.typical_bed_hour)
                    self.config.max_daytime_inactivity_hours = data.get("max_daytime_inactivity_hours", self.config.max_daytime_inactivity_hours)
                    self.config.max_night_restless_count = data.get("max_night_restless_count", self.config.max_night_restless_count)
                    self.config.max_outing_hours = data.get("max_outing_hours", self.config.max_outing_hours)
            except Exception as e:
                logger.error(f"[RoutineMonitor] 加载作息基线失败: {e}")

    def _save_profile(self):
        """持久化作息画像配置"""
        try:
            with open(self.profile_path, "w", encoding="utf-8") as f:
                json.dump(asdict(self.config), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[RoutineMonitor] 保存作息基线失败: {e}")

    def apply_onboarding_preferences(
        self,
        wake_habit: str = "REGULAR",
        living_arrangement: str = "SOLO",
    ) -> RoutineProfileConfig:
        """
        根据首次安装向导中用户点选的早起习惯与居住形态，温和初始化作息基线，
        避免安装首周因不了解老人睡懒觉或双人交替活动而触发误报。
        """
        with self._lock:
            habit = (wake_habit or "REGULAR").upper()
            if habit == "EARLY":
                self.config.typical_wake_hour = 6.5      # 06:30 起床
                self.config.wake_delay_tolerance_hours = 1.5
            elif habit == "LEISURE":
                self.config.typical_wake_hour = 9.0      # 09:00 晚起悠闲型
                self.config.wake_delay_tolerance_hours = 2.0
            else:
                self.config.typical_wake_hour = 7.5      # 07:30 常规型
                self.config.wake_delay_tolerance_hours = 1.5

            if (living_arrangement or "SOLO").upper() == "COHABIT":
                # 双人/多人同住时，放宽单次外出与日间安静容忍度
                self.config.max_outing_hours = 8.0
                self.config.max_daytime_inactivity_hours = 4.5
            else:
                self.config.max_outing_hours = 5.0
                self.config.max_daytime_inactivity_hours = 3.5

            self._save_profile()
            logger.info(
                f"[RoutineMonitor] 已应用人性化向导作息偏好: 早起习惯={habit} "
                f"(基准起床={self.config.typical_wake_hour:.1f}h), 居住形态={living_arrangement}"
            )
            return self.config

    def update_state(
        self,
        human_state: HumanState,
        timestamp: Optional[float] = None,
        is_presence: bool = True,
        is_motion: bool = False,
    ) -> List[RoutineAnomalyEvent]:
        """
        接收算法状态流，更新今日作息指标，并诊断是否存在作息偏离。
        """
        now_ts = timestamp or time.time()
        dt = datetime.fromtimestamp(now_ts)
        today_str = dt.strftime("%Y-%m-%d")
        current_hour_float = dt.hour + dt.minute / 60.0

        with self._lock:
            # 1. 跨天自动轮转归档
            if today_str != self.current_date:
                self._archive_day(self.current_date)
                self.current_date = today_str
                self.metrics = TodayCircadianMetrics(date_str=today_str)
                self.cooldown_map.clear()

            # 2. 状态与时间戳推移
            hour_idx = dt.hour
            if is_motion:
                self.metrics.hourly_activity[hour_idx] += 1
                self.metrics.total_active_seconds += 1.0
                self.last_motion_time = now_ts

            # 在家/离家判断
            was_home = self.is_currently_home
            is_home = (human_state != HumanState.EMPTY) and is_presence
            self.is_currently_home = is_home

            if was_home and not is_home:
                # 刚刚离家
                self.left_home_time = now_ts
                logger.info(f"[RoutineMonitor] 检测到老人离家外出，启动出行安全计时: {dt.strftime('%H:%M:%S')}")
            elif not was_home and is_home:
                # 刚刚归家
                if self.left_home_time:
                    out_hours = (now_ts - self.left_home_time) / 3600.0
                    logger.info(f"[RoutineMonitor] 老人安全归家，本次外出历时 {out_hours:.1f} 小时")
                self.left_home_time = None

            # 3. 晨间起床判定 (早上 05:00 - 11:30 之间的第一次持续走动)
            if (5.0 <= current_hour_float <= 11.5) and is_motion and not self.metrics.woken_up:
                self.metrics.woken_up = True
                self.metrics.wake_time_str = dt.strftime("%H:%M")
                logger.info(f"[RoutineMonitor] 记录老人今日起床: {self.metrics.wake_time_str}")
                # 动态自适应学习 (EMA 加权)
                if self.config.auto_learn_enabled:
                    self.config.typical_wake_hour = round(
                        self.config.typical_wake_hour * 0.85 + current_hour_float * 0.15, 2
                    )
                    self._save_profile()

            # 4. 夜间入睡判定 (晚上 20:30 - 04:00 长时间无走动转入静止)
            if (current_hour_float >= 20.5 or current_hour_float <= 4.0):
                if human_state == HumanState.STATIC and not is_motion:
                    # 进入静止睡眠
                    if not self.metrics.went_to_bed:
                        self.metrics.went_to_bed = True
                        self.metrics.bed_time_str = dt.strftime("%H:%M")
                        logger.info(f"[RoutineMonitor] 记录老人入睡休息: {self.metrics.bed_time_str}")
                elif is_motion and (now_ts - self.last_night_motion_time > 180.0):
                    # 两次夜间起夜间隔大于 3 分钟计为一次起夜事件
                    self.last_night_motion_time = now_ts
                    self.metrics.night_restless_count += 1
                    logger.info(f"[RoutineMonitor] 监测到夜间起夜/走动，今日累计第 {self.metrics.night_restless_count} 次")

            # 5. 异常偏离诊断
            anomalies = self._check_deviations(now_ts, dt, current_hour_float)

        return anomalies

    def _check_deviations(
        self, now_ts: float, dt: datetime, current_hour_float: float
    ) -> List[RoutineAnomalyEvent]:
        """作息偏离核心诊断逻辑"""
        detected: List[RoutineAnomalyEvent] = []

        # ------------------------------------------------------------------
        # 异常 1: 【晨间未按时起床预警】
        # 超过预期起床时间容忍上限，且老人确认在家，但尚未检测到任何走动
        # ------------------------------------------------------------------
        wake_deadline = self.config.typical_wake_hour + self.config.wake_delay_tolerance_hours
        if (
            current_hour_float >= wake_deadline
            and not self.metrics.woken_up
            and self.is_currently_home
        ):
            if self._can_alert(RoutineAnomalyType.WAKEUP_DELAYED, now_ts, cooldown_sec=3600 * 2):
                h_int = int(self.config.typical_wake_hour)
                m_int = int((self.config.typical_wake_hour - h_int) * 60)
                dl_h = int(wake_deadline)
                dl_m = int((wake_deadline - dl_h) * 60)

                evt = RoutineAnomalyEvent(
                    anomaly_type=RoutineAnomalyType.WAKEUP_DELAYED,
                    title="【作息偏离】晨间超期未见起床预警",
                    description=(
                        f"通常起床时间为 {h_int:02d}:{m_int:02d}。当前已达 {dt.strftime('%H:%M')}，"
                        f"已超期容忍上限 ({dl_h:02d}:{dl_m:02d})，屋内始终未检测到走动，请关注老人是否卧床不适或突发意外！"
                    ),
                    severity="ALERT",
                    timestamp=now_ts,
                    time_str=dt.strftime("%H:%M:%S"),
                    details={
                        "typical_wake": f"{h_int:02d}:{m_int:02d}",
                        "deadline": f"{dl_h:02d}:{dl_m:02d}",
                    },
                )
                detected.append(evt)

        # ------------------------------------------------------------------
        # 异常 2: 【日间长时间静止无活动预警】
        # 白天正常起居时间 (08:30 - 21:00) 且今天已经起床后，连续超过设定小时无走动
        # (若尚未起床，则由异常 1 [晨间未按时起床] 专门负责，避免双重告警)
        # ------------------------------------------------------------------
        if self.metrics.woken_up and (8.5 <= current_hour_float <= 21.0) and self.is_currently_home:
            inactivity_duration = (now_ts - self.last_motion_time) / 3600.0
            if inactivity_duration >= self.config.max_daytime_inactivity_hours:
                if self._can_alert(RoutineAnomalyType.PROLONGED_INACTIVITY, now_ts, cooldown_sec=3600 * 3):
                    evt = RoutineAnomalyEvent(
                        anomaly_type=RoutineAnomalyType.PROLONGED_INACTIVITY,
                        title="【作息偏离】日间异常长时间无活动预警",
                        description=(
                            f"室内人员已连续 {inactivity_duration:.1f} 小时无走动或体征大动作"
                            f"（设防上限: {self.config.max_daytime_inactivity_hours} 小时）。"
                            f"请核实老人是否处于昏睡、跌落隐秘死角或疾病失能状态！"
                        ),
                        severity="ALERT",
                        timestamp=now_ts,
                        time_str=dt.strftime("%H:%M:%S"),
                        details={"inactivity_hours": round(inactivity_duration, 1)},
                    )
                    detected.append(evt)

        # ------------------------------------------------------------------
        # 异常 3: 【夜间异常频繁起夜/异动预警】
        # 昨夜起夜频次超出设定阈值
        # ------------------------------------------------------------------
        if self.metrics.night_restless_count >= self.config.max_night_restless_count:
            if self._can_alert(RoutineAnomalyType.NIGHT_RESTLESSNESS, now_ts, cooldown_sec=3600 * 4):
                evt = RoutineAnomalyEvent(
                    anomaly_type=RoutineAnomalyType.NIGHT_RESTLESSNESS,
                    title="【健康隐患】夜间起夜/异常走动频次过高",
                    description=(
                        f"昨夜至今晨老人起夜走动已达 {self.metrics.night_restless_count} 次"
                        f"（参考上限: {self.config.max_night_restless_count} 次）。"
                        f"夜间频繁走动易诱发黑暗中绊倒，且提示可能存在夜尿频多或失眠谵妄风险。"
                    ),
                    severity="WARNING",
                    timestamp=now_ts,
                    time_str=dt.strftime("%H:%M:%S"),
                    details={"count": self.metrics.night_restless_count},
                )
                detected.append(evt)

        # ------------------------------------------------------------------
        # 异常 4: 【长时间离家未归预警】
        # 外出超过设定时长未见返回
        # ------------------------------------------------------------------
        if not self.is_currently_home and self.left_home_time:
            outing_hours = (now_ts - self.left_home_time) / 3600.0
            if outing_hours >= self.config.max_outing_hours:
                if self._can_alert(RoutineAnomalyType.PROLONGED_OUTING, now_ts, cooldown_sec=3600 * 3):
                    left_dt = datetime.fromtimestamp(self.left_home_time).strftime("%H:%M")
                    evt = RoutineAnomalyEvent(
                        anomaly_type=RoutineAnomalyType.PROLONGED_OUTING,
                        title="【出行安全】老人长时间离家未归预警",
                        description=(
                            f"老人自上午 {left_dt} 离家后，已累计外出 {outing_hours:.1f} 小时未归"
                            f"（参考阈值: {self.config.max_outing_hours} 小时）。建议电话联系老人确认其安全，防范走失！"
                        ),
                        severity="WARNING",
                        timestamp=now_ts,
                        time_str=dt.strftime("%H:%M:%S"),
                        details={"outing_hours": round(outing_hours, 1), "left_time": left_dt},
                    )
                    detected.append(evt)

        # ------------------------------------------------------------------
        # 异常 5: 【深夜严重未眠 / 异常活动预警】
        # 凌晨 02:00 - 04:30 尚未入睡且当前小时内持续发生大幅活动
        # ------------------------------------------------------------------
        if (2.0 <= current_hour_float <= 4.5) and self.is_currently_home and not self.metrics.went_to_bed:
            if self.metrics.hourly_activity[dt.hour] >= 3:
                if self._can_alert(RoutineAnomalyType.IRREGULAR_LATE_SLEEP, now_ts, cooldown_sec=3600 * 4):
                    evt = RoutineAnomalyEvent(
                        anomaly_type=RoutineAnomalyType.IRREGULAR_LATE_SLEEP,
                        title="【作息偏离】深夜严重未眠与异常活动预警",
                        description=(
                            f"当前已至凌晨 {dt.strftime('%H:%M')}（往常入睡基准: "
                            f"{int(self.config.typical_bed_hour):02d}:{int((self.config.typical_bed_hour % 1) * 60):02d}），"
                            f"老人仍未入睡且持续走动，请留意是否发生严重失眠、焦虑或突发身体不适！"
                        ),
                        severity="WARNING",
                        timestamp=now_ts,
                        time_str=dt.strftime("%H:%M:%S"),
                        details={"hour_activity": self.metrics.hourly_activity[dt.hour]},
                    )
                    detected.append(evt)

        # 派发回调与记录
        for evt in detected:
            self._dispatch_anomaly(evt)

        return detected

    def _can_alert(self, anomaly_type: RoutineAnomalyType, now_ts: float, cooldown_sec: float) -> bool:
        """防告警轰炸冷却计时"""
        key = anomaly_type.value
        last_t = self.cooldown_map.get(key, 0.0)
        if (now_ts - last_t) >= cooldown_sec:
            self.cooldown_map[key] = now_ts
            return True
        return False

    def _dispatch_anomaly(self, evt: RoutineAnomalyEvent):
        """派发异常通知与日志"""
        logger.log_event(
            event_type="ROUTINE_ANOMALY",
            message=f"{evt.title}: {evt.description}",
            level=evt.severity,
            data=evt.details,
        )
        # 记录到今日异常快照
        self.metrics.active_anomalies.append({
            "type": evt.anomaly_type.value,
            "title": evt.title,
            "time": evt.time_str,
            "description": evt.description,
            "severity": evt.severity,
        })
        # 外部回调 (通知微信/HA)
        if self.on_anomaly:
            try:
                self.on_anomaly(evt)
            except Exception as e:
                logger.error(f"[RoutineMonitor] 异常回调执行失败: {e}")

    def _archive_day(self, date_str: str):
        """将昨日作息记录追加归档到 history.jsonl"""
        try:
            record = {
                "date": date_str,
                "wake_time": self.metrics.wake_time_str,
                "bed_time": self.metrics.bed_time_str,
                "total_active_minutes": round(self.metrics.total_active_seconds / 60.0, 1),
                "night_restless_count": self.metrics.night_restless_count,
                "hourly_activity": self.metrics.hourly_activity,
                "anomalies_count": len(self.metrics.active_anomalies),
            }
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.info(f"[RoutineMonitor] 完成昨日 ({date_str}) 作息画像归档")
        except Exception as e:
            logger.error(f"[RoutineMonitor] 作息归档写入失败: {e}")

    def get_recent_history(self, days: int = 7) -> List[Dict[str, Any]]:
        """读取最近 days 天的已归档作息记录（不含今天），供家属看板趋势图使用"""
        if not os.path.exists(self.history_path):
            return []
        records = []
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return records[-days:]

    def get_summary(self) -> Dict[str, Any]:
        """获取当前前端 Web 与 Home Assistant 消费的作息总览 JSON"""
        with self._lock:
            h_int = int(self.config.typical_wake_hour)
            m_int = int((self.config.typical_wake_hour - h_int) * 60)
            bed_h = int(self.config.typical_bed_hour)
            bed_m = int((self.config.typical_bed_hour - bed_h) * 60)

            return {
                "date": self.current_date,
                "woken_up": self.metrics.woken_up,
                "wake_time": self.metrics.wake_time_str or "尚未起床",
                "bed_time": self.metrics.bed_time_str or "待定",
                "typical_wake": f"{h_int:02d}:{m_int:02d}",
                "typical_bed": f"{bed_h:02d}:{bed_m:02d}",
                "night_restless_count": self.metrics.night_restless_count,
                "total_active_minutes": round(self.metrics.total_active_seconds / 60.0, 1),
                "hourly_activity": self.metrics.hourly_activity,
                "active_anomalies": self.metrics.active_anomalies,
                "has_anomaly": len(self.metrics.active_anomalies) > 0,
            }

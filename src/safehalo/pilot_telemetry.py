"""
SafeHalo 阶段 2：3–5 户入户试点管理与月度误报统计引擎 (pilot_telemetry.py)
对应 docs/phase1_plan.md 阶段 2 目标 4:
「3–5 户试点，统计每户每月误报次数」

核心功能:
1. 多户试点档案管理 (3~5 户典型养老家庭场景建模):
   - H01: 独居高龄老人（客厅+卫生间，高危防滑场景）
   - H02: 独居携宠老人（客厅，猫狗高频跑动抗扰场景）
   - H03: 老年夫妇双人同住（主卧+客厅，多链路与夜间起夜场景）
   - H04: 慢病虚弱独居老人（卧室，床铺避让与作息异常监测场景）
   - H05: 老旧密集公寓独居老人（多邻居同频 Wi-Fi 穿墙抗扰场景）
2. 每户每月误报次数与核心指标自动汇总 (Monthly False Alarm Telemetry):
   - 按 `household_id` 与自然月 `YYYY-MM` 统计:
     * 月度累计误报次数 (`false_alarms_count`)
     * 月度真实告警次数 (`true_alarms_count`)
     * 本地声光质询无感拦截误报次数 (`challenge_intercepted_count`)
     * 误报原因分布 (`category_breakdown`: 宠物/风扇窗帘/物品掉落/正常坐卧/水流)
   - 自动评定是否符合阶段 2 试点通过标准（默认每户每月误报 <= 2 次）。
3. 支持导出试点评估 JSON 报表与 Markdown 报告。
"""

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .logger import logger


@dataclass
class PilotHouseholdProfile:
    """单户试点家庭档案"""
    household_id: str
    name: str
    scenario_desc: str
    rooms_covered: List[str]
    has_pets: bool = False
    elderly_count: int = 1
    installed_date: str = "2026-11-01"
    target_max_monthly_false_alarms: int = 2
    online: bool = True


@dataclass
class MonthlyHouseholdStats:
    """单户单月统计指标"""
    household_id: str
    month: str                                  # 格式: "YYYY-MM"
    false_alarms_count: int = 0                 # 每户每月误报次数 (核心考核指标)
    true_alarms_count: int = 0                  # 真实告警次数
    challenge_intercepted_count: int = 0        # 质询窗口主动应答拦截次数
    pending_events_count: int = 0               # 待确认事件数
    category_breakdown: Dict[str, int] = field(default_factory=dict)


DEFAULT_PILOT_HOUSEHOLDS: List[PilotHouseholdProfile] = [
    PilotHouseholdProfile(
        household_id="H01",
        name="1号试点户（张奶奶·78岁独居）",
        scenario_desc="一室一厅+独立卫浴，重点验证卫生间防滑与日常作息偏离",
        rooms_covered=["客厅", "卫生间"],
        has_pets=False,
        elderly_count=1,
    ),
    PilotHouseholdProfile(
        household_id="H02",
        name="2号试点户（王阿姨·72岁独居携猫狗）",
        scenario_desc="两室一厅，养有1只柯基与1只猫，重点验证宠物SPR过滤免疫",
        rooms_covered=["客厅", "主卧"],
        has_pets=True,
        elderly_count=1,
    ),
    PilotHouseholdProfile(
        household_id="H03",
        name="3号试点户（李爷爷夫妇·81岁/79岁）",
        scenario_desc="双人同住+双AP多链路覆盖，重点验证双人活动与夜间频繁起夜",
        rooms_covered=["客厅", "主卧", "次卧"],
        has_pets=False,
        elderly_count=2,
    ),
    PilotHouseholdProfile(
        household_id="H04",
        name="4号试点户（赵大爷·84岁慢病虚弱）",
        scenario_desc="卧室床头重点看护，验证床面翻身避让与晨间未按时起床通报",
        rooms_covered=["卧室"],
        has_pets=False,
        elderly_count=1,
    ),
    PilotHouseholdProfile(
        household_id="H05",
        name="5号试点户（陈奶奶·76岁老旧密集公寓）",
        scenario_desc="周边12个邻居同频Wi-Fi信号源，重点验证邻居穿墙信号隔离与抗扰",
        rooms_covered=["客厅", "走廊"],
        has_pets=False,
        elderly_count=1,
    ),
]


class PilotHouseholdManager:
    """3~5 户入户试点管理与每户每月误报统计引擎"""

    def __init__(
        self,
        storage_dir: Optional[str] = None,
        active_household_id: str = "H01",
    ):
        self.storage_dir = storage_dir or os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "logs")
        )
        os.makedirs(self.storage_dir, exist_ok=True)
        self.data_file = os.path.join(self.storage_dir, "pilot_telemetry.json")
        self.active_household_id = active_household_id
        self._lock = threading.Lock()

        self.households: Dict[str, PilotHouseholdProfile] = {
            h.household_id: h for h in DEFAULT_PILOT_HOUSEHOLDS
        }
        # key: "{household_id}_{YYYY-MM}" -> MonthlyHouseholdStats
        self.monthly_stats: Dict[str, MonthlyHouseholdStats] = {}
        self._load_data()

    def _load_data(self):
        if not os.path.exists(self.data_file):
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
                self.active_household_id = raw.get("active_household_id", self.active_household_id)
                for h_id, h_dict in raw.get("households", {}).items():
                    self.households[h_id] = PilotHouseholdProfile(**h_dict)
                for key, st_dict in raw.get("monthly_stats", {}).items():
                    self.monthly_stats[key] = MonthlyHouseholdStats(**st_dict)
        except Exception as e:
            logger.error(f"[PilotManager] 读取试点统计数据失败: {e}")

    def _save_data(self):
        try:
            payload = {
                "active_household_id": self.active_household_id,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "households": {k: asdict(v) for k, v in self.households.items()},
                "monthly_stats": {k: asdict(v) for k, v in self.monthly_stats.items()},
            }
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[PilotManager] 保存试点统计数据失败: {e}")

    def _get_or_create_month_stat(self, household_id: str, month_str: Optional[str] = None) -> MonthlyHouseholdStats:
        m = month_str or datetime.now().strftime("%Y-%m")
        key = f"{household_id}_{m}"
        if key not in self.monthly_stats:
            self.monthly_stats[key] = MonthlyHouseholdStats(household_id=household_id, month=m)
        return self.monthly_stats[key]

    def record_false_alarm(
        self,
        household_id: Optional[str] = None,
        category: str = "other",
        timestamp: Optional[float] = None,
    ) -> MonthlyHouseholdStats:
        """记录一次经核实的误报事件，累加该户当月误报计数"""
        h_id = household_id or self.active_household_id
        month_str = datetime.fromtimestamp(timestamp or time.time()).strftime("%Y-%m")
        with self._lock:
            st = self._get_or_create_month_stat(h_id, month_str)
            st.false_alarms_count += 1
            st.category_breakdown[category] = st.category_breakdown.get(category, 0) + 1
            self._save_data()
            logger.info(
                f"[PilotManager] 试点户 [{h_id}] ({month_str}) 误报计数 +1 -> 当前当月累计 {st.false_alarms_count} 次 (原因: {category})"
            )
            return st

    def record_true_alarm(
        self,
        household_id: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> MonthlyHouseholdStats:
        """记录一次真实有效告警"""
        h_id = household_id or self.active_household_id
        month_str = datetime.fromtimestamp(timestamp or time.time()).strftime("%Y-%m")
        with self._lock:
            st = self._get_or_create_month_stat(h_id, month_str)
            st.true_alarms_count += 1
            self._save_data()
            return st

    def record_challenge_intercept(
        self,
        household_id: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> MonthlyHouseholdStats:
        """记录一次通过本地交互质询窗口成功拦截的潜在误报"""
        h_id = household_id or self.active_household_id
        month_str = datetime.fromtimestamp(timestamp or time.time()).strftime("%Y-%m")
        with self._lock:
            st = self._get_or_create_month_stat(h_id, month_str)
            st.challenge_intercepted_count += 1
            self._save_data()
            return st

    def get_pilot_dashboard(self, month_str: Optional[str] = None) -> Dict[str, Any]:
        """生成 3~5 户试点全局月度统计看板数据"""
        target_month = month_str or datetime.now().strftime("%Y-%m")
        with self._lock:
            household_rows = []
            total_false_alarms = 0
            total_true_alarms = 0
            total_intercepts = 0
            compliant_count = 0

            for h_id, profile in sorted(self.households.items()):
                st = self._get_or_create_month_stat(h_id, target_month)
                within_target = st.false_alarms_count <= profile.target_max_monthly_false_alarms
                if within_target:
                    compliant_count += 1

                total_false_alarms += st.false_alarms_count
                total_true_alarms += st.true_alarms_count
                total_intercepts += st.challenge_intercepted_count

                household_rows.append({
                    "household_id": h_id,
                    "name": profile.name,
                    "scenario_desc": profile.scenario_desc,
                    "rooms_covered": profile.rooms_covered,
                    "has_pets": profile.has_pets,
                    "month": target_month,
                    "false_alarms_count": st.false_alarms_count,
                    "true_alarms_count": st.true_alarms_count,
                    "challenge_intercepted_count": st.challenge_intercepted_count,
                    "target_max_false_alarms": profile.target_max_monthly_false_alarms,
                    "passed_standard": within_target,
                    "category_breakdown": st.category_breakdown,
                })

            num_h = max(1, len(self.households))
            avg_fa = round(total_false_alarms / float(num_h), 2)
            return {
                "month": target_month,
                "active_household_id": self.active_household_id,
                "pilot_households_count": len(self.households),
                "compliant_households_count": compliant_count,
                "avg_monthly_false_alarms_per_household": avg_fa,
                "total_false_alarms": total_false_alarms,
                "total_true_alarms": total_true_alarms,
                "total_challenge_intercepts": total_intercepts,
                "pilot_overall_passed": compliant_count == len(self.households),
                "households": household_rows,
            }

    def export_markdown_report(self, month_str: Optional[str] = None) -> str:
        """导出阶段 2 入户试点月度误报统计 Markdown 报告"""
        dash = self.get_pilot_dashboard(month_str)
        lines = [
            f"# SafeHalo 阶段 2 入户试点月度评估报告 ({dash['month']})",
            "",
            f"- **试点户数**: {dash['pilot_households_count']} 户",
            f"- **达标户数 (月误报 ≤ 2 次)**: {dash['compliant_households_count']} / {dash['pilot_households_count']} 户",
            f"- **户均月度误报次数**: {dash['avg_monthly_false_alarms_per_household']} 次/户/月",
            f"- **本地质询窗口无感拦截次数**: {dash['total_challenge_intercepts']} 次",
            "",
            "| 户号 | 试点家庭画像 | 覆盖房间 | 当月误报次数 | 质询拦截次数 | 真实告警 | 考核结论 |",
            "|---|---|---|---|---|---|---|",
        ]
        for h in dash["households"]:
            rooms = "、".join(h["rooms_covered"])
            status_str = "✅ 达标" if h["passed_standard"] else "⚠️ 超标需调优"
            lines.append(
                f"| `{h['household_id']}` | {h['name']} | {rooms} | "
                f"**{h['false_alarms_count']}** (目标 ≤{h['target_max_false_alarms']}) | "
                f"{h['challenge_intercepted_count']} | {h['true_alarms_count']} | {status_str} |"
            )
        return "\n".join(lines)

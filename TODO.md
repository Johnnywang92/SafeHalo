# SafeHalo TODO（按优先级排序）

更新时间：2026-11-11

## 排序原则

- **P0：安全 / 正确性 / 试点阻断项**。不做完不应进入真实家庭试点，也不应宣称“可上线”。
- **P1：生产可用 / Python-Rust 能力对齐项**。决定 Rust 独立服务能否真正替代 Python 生产服务。
- **P2：扩展 / 工程化 / 文档项**。提升可维护性、可接入性和交付质量。
- **P3：研究 / 长期产品化项**。依赖数据、硬件选型或外部合作，不阻塞当前试点。

同一优先级内按“安全影响 → 依赖关系 → 实施成本”排序。

## 当前已完成基线（不重复列入 TODO）

- Python 生产服务：CSI 检测、四段式质询闭环、自动校准、毫米波双模、多链路、作息异常、安防模式、试点统计、小程序反馈、HA MQTT/Webhook。
- Rust daemon：检测内核与 Python 行为等价；golden fixture / 场景 / stage-2 开关验证通过。
- Rust 独立服务层：鉴权/RBAC、静态控制台、SSE/事件回填、告警落库、通知分发、串口作动、串口自愈、procd/UCI 部署。
- 访问控制加固（P0-4，双引擎）：强制首登改密 + 密码策略（≥8 位、禁默认密码、新旧不同）、登录/应答/推送/模式限速与失败审计、Cookie `Origin`/`Referer` CSRF 校验与 HTTPS `Secure` 标记、Rust 机器 `api_token` 降权（仅读 + 反馈/应答/切模式）、免登接口分策略（向导配置后需管理员、事件反馈需登录态）。
- 四架构 musl 静态交叉编译；`verify/run_service.sh` 53 项服务回归通过；Python `tests/test_web_security.py` 9 项安全用例通过。
- README 详细部署流程与 Agent 一键安装提示词。

---

## P0：安全 / 正确性 / 试点阻断

### P0-1 真实硬件联调与长稳验证

- **现状**：服务验证主要基于 pty、mock HTTP 和 replay；真实 ESP32 串口作动、断网/掉线/断电恢复、7×24 长稳尚未闭环。
- **任务**：
  - 在真实 ESP32-S3 + 2.4GHz 路由器上验证 `BURST_RATE:100/30`、`CHALLENGE_BEEP:1` 实际下发与探针响应。
  - 验证拔插串口、路由器重启、断网、断电恢复后的自动重连与状态恢复。
  - 在 OpenWrt 目标设备上跑 24–72 小时长稳，记录 RSS、fd、flash 写入、事件/告警文件增长。
- **完成标准**：真实硬件联调报告；长稳期间无内存泄漏、无异常退出、无 flash 写穿；掉线后自动恢复。
- **依赖**：硬件、现场环境、`experiments/rust-daemon/deploy/`。

### P0-2 现场数据标定与阈值落地

- **现状**：`object_confirm_cv=0.35` 等仍是占位值；日志统计只能“仅供参考”，不能写入默认阈值。
- **任务**：
  - 用 `scripts/csi_recorder.py` 采集 E1 空房、E2 静止、E3 运动、E4 干扰、E5 换房间、E6 跌倒模拟。
  - 基于真实录制标定冲击阈值、静止窗口、宠物 SPR、OBJECT 确认 CV/最少峰间隔。
  - 更新 golden fixture 与生成参数，Python/Rust 双引擎全量重跑。
- **完成标准**：阶段 1 指标达标；阶段 2 误报/漏报有真实数据支撑；默认阈值不再来自占位或日志粗统计。
- **依赖**：P0-1 硬件可用；`docs/phase1_plan.md`、`docs/phase2_plan.md`。

### P0-3 VERDICT_OBJECT 呼吸峰计数器噪声鲁棒性修复

- **现状**：`targeted_inspector` 呼吸峰计数对 CSI 量化噪声不鲁棒，`VERDICT_OBJECT` 在真实噪声下几乎不可达；当前误报排除主要依赖 challenge 应答。
- **任务**：
  - 用峰间隔规整度/准周期性检验替代单纯幅值峰计数。
  - 修复必须默认关闭或加开关，避免把真跌倒误判成物品造成漏报。
  - Python 生产引擎与 Rust daemon 同步修改，重跑等价套件与真实数据回放。
- **完成标准**：真实噪声下 OBJECT 分支可达且不误杀真跌倒；开关关闭时行为位级一致；有回归测试。
- **依赖**：P0-2 数据；`src/safehalo/targeted_inspector.py`、`experiments/rust-daemon/src/inspector.rs`。

### P0-5 告警可靠性与 payload 契约对齐

- **现状**：Rust 通知已支持多通道与退出 flush，但缺少失败重试/退避/持久化队列；小程序 payload 中 `confidence` 仍硬编码 `0.96`；Rust 告警使用检测器实测 bpm/confidence，Python 部分路径仍硬编码 `18/0.96`。
- **任务**：
  - 通知失败重试、退避、死信记录；进程重启后可恢复未发送告警。
  - 统一 Python/Rust 告警 payload：`event_id`、`confidence`、`respiration_bpm`、`timestamp`、反馈路径。
  - 告警去重、升级策略、家属确认/超时未处理升级。
- **完成标准**：断网/通道失败不丢告警；双引擎 payload 契约一致；小程序/HA/SMS 收到正确置信度与呼吸值。
- **依赖**：`experiments/rust-daemon/src/notify.rs`、`src/incidents.rs`、`src/safehalo/notifier.py`。

### P0-6 数据隐私、存储权限与 flash 寿命

- **现状**：事件日志有 rotation，告警文件上限 500 条但每次全量写盘；真实 `logs/` 数据不得提交；用户数据库权限未明确收紧。
- **任务**：
  - `users.json`、通知配置、告警/事件文件权限收紧到 `0600`/`0700`。
  - 告警/事件写入改为追加或批量落盘，减少 OpenWrt flash 磨损。
  - 增加数据脱敏/导出工具，真实录制提交前自动检查可识别家庭信息。
  - 明确备份/恢复与升级时数据保留策略。
- **完成标准**：敏感文件权限正确；长稳写入量可控；真实数据不经脱敏无法提交。
- **依赖**：P0-1 长稳数据；`experiments/rust-daemon/src/events.rs`、`src/incidents.rs`。

---

## P1：生产可用 / Python-Rust 能力对齐

### P1-1 Rust 状态契约补齐或明确降级

- **现状**：Rust `/api/status` 中 `spr=0.0`、`radar_online=false`、`fused_state` 直接等于 CSI state；`routine`、`multilink`、`pilot` 为静态 stub。
- **任务**：
  - 补齐真实 SPR、宠物判定、融合置信度/原因。
  - 对未实现能力返回明确 `unsupported`/`enabled=false`，避免前端误以为功能在线。
  - 保持 `--snapshot` 等价输出不被服务层字段污染。
- **完成标准**：前端展示与后端能力一致；等价 snapshot 仍位级一致；契约测试覆盖。
- **依赖**：`experiments/rust-daemon/src/state.rs`、`src/safehalo/web/index.html`。

### P1-2 安防模式实际生效

- **现状**：Rust `/api/set_mode` 只写入 `security_mode`，未改变检测/告警策略；Python 有 `SecurityGuardEngine`。
- **任务**：
  - 移植或对接 `FALL_GUARD` / `AWAY_SECURITY` / `NIGHT_GUARD` 策略。
  - 明确不同模式下的入侵告警、夜间巡航、老人防走失行为。
- **完成标准**：模式切换后检测/告警行为可验证；双引擎策略一致。
- **依赖**：P1-1；`src/safehalo/security_guard.py`。

### P1-3 误报反馈靶向调参闭环

- **现状**：Rust 记录 feedback category，但未把 `pet`、`shower_water`、`curtain_fan`、`dropped_object` 等归因转成实际参数调整。
- **任务**：
  - 实现 category → 参数调整映射，持久化并应用到检测器/placement。
  - 与 Python `PlacementAdapter` / `IncidentFeedbackManager` 对齐。
  - 调参后触发等价回归，避免反馈把安全阈值调偏。
- **完成标准**：家属反馈后参数真实变化；误报统计可追溯；调参有上下限保护。
- **依赖**：P0-2、P1-1；`src/safehalo/placement_adapter.py`。

### P1-4 试点统计真实化

- **现状**：Rust `pilot_json()` 静态返回 H01/0 计数；Python 有 `PilotHouseholdManager`。
- **任务**：
  - 记录真实告警、误报、质询拦截、响应时间、月度统计。
  - 支持多户画像、达标率（每户每月误报 ≤ 2）、Markdown 导出。
- **完成标准**：`/api/phase2/pilot` 返回真实统计；试点月报可生成。
- **依赖**：P0-5、P1-3；`src/safehalo/pilot_telemetry.py`。

### P1-5 作息异常监测移植或边界声明

- **现状**：Python `RoutineMonitorEngine` 已实现晨起/日间/夜间/出行异常；Rust `routine_json()` 为静态。
- **任务**：
  - 若 Rust 要独立替代 Python，移植作息画像、异常冷却、历史归档。
  - 若暂不移植，在 README/控制台明确 Rust 不支持作息异常，避免误导。
- **完成标准**：能力边界清晰；若移植，则异常触发与 Python 一致。
- **依赖**：P1-1；`src/safehalo/routine_monitor.py`。

### P1-6 多链路融合移植或边界声明

- **现状**：Python `MultiLinkFusionEngine` 支持多 AP 空间分集；Rust `multilink_json()` 只返回单链路占位。
- **任务**：
  - 移植多链路输入、子链路状态、房间级融合。
  - 或明确 Rust 当前单链路，多链路仍走 Python。
- **完成标准**：多 AP 场景有真实融合输出或明确降级提示。
- **依赖**：P1-1；`src/safehalo/multilink_fusion.py`、`docs/multilink_mesh_tuning.md`。

### P1-7 毫米波双模移植或边界声明

- **现状**：毫米波解析/融合仅在 Python；Rust 状态中 radar 字段恒为 false/0。
- **任务**：
  - 决策：移植 `mmwave_parser` / `mmwave_detector` / `dual_fusion_engine` 到 Rust，或保留 Python 双模服务。
  - 若移植，增加 `--radar-serial`、协议自动识别、融合真值表测试。
- **完成标准**：双模能力有明确归属；Rust 控制台不展示虚假雷达在线。
- **依赖**：P1-1；`docs/mmwave_integration_guide.md`。

### P1-8 HA MQTT Discovery 对齐

- **现状**：Python 支持 HA MQTT Discovery；Rust 当前主要走 webhook。
- **任务**：
  - 移植轻量 MQTT Discovery，或提供标准 HA REST/MQTT 桥接文档。
  - 保证实体命名、状态 topic、命令 topic 与 Python 一致。
- **完成标准**：HA 可自动发现 SafeHalo 实体；告警/模式/质询命令可联动。
- **依赖**：P0-5；`src/safehalo/ha_discovery.py`、`docs/home_assistant_integration.md`。

### P1-9 通用硬件接入框架

- **现状**：Rust 只有一个 CSI 串口；执行器命令只写回 CSI 口；UDP/回放模式无作动出口；无设备注册表。
- **任务**：
  - 增加 `config device` / `--device`：type、path、baud、protocol、role。
  - 传感器独立读取线程 + 统一事件总线；执行器 dispatcher 消费 Action。
  - 增加 `/api/devices` 状态与管理员白名单命令接口，禁止任意串口注入。
- **完成标准**：可接入第二路串口传感器/执行器；CSI 等价路径不被破坏。
- **依赖**：P0-1；`experiments/rust-daemon/src/serial.rs`、`src/main.rs`。

### P1-10 OpenWrt 安装与运行健壮性

- **现状**：部分 mipsel 设备 `uname -m` 报 `mips`，自动架构选择可能错；HTTPS 通知依赖系统 helper。
- **任务**：
  - `install.sh` 增加架构/ELF 校验，优先读取 OpenWrt `DISTRIB_ARCH`。
  - 安装前检查 `/tmp`、`/etc` 空间与 HTTPS helper。
  - 增加升级/回滚、配置备份、procd 日志排查命令。
- **完成标准**：常见 OpenWrt 平台一键安装成功率可复现；错架构二进制不会被安装。
- **依赖**：`experiments/rust-daemon/deploy/install.sh`、`cross/build-all.sh`。

### P1-11 CI / 回归自动化

- **现状**：验证脚本齐全，但无 CI 自动执行。
- **任务**：
  - 增加 CI：Python unittest、Rust cargo test、fixtures、scenarios、service_check、交叉编译。
  - 对算法改动强制双引擎等价门禁。
- **完成标准**：PR 自动跑回归；等价失败阻断合并。
- **依赖**：`experiments/rust-daemon/verify/`、`tests/`。

### P1-12 前端契约与浏览器自动化测试

- **现状**：嵌入式控制台已人工浏览器验证，但缺少自动化契约测试。
- **任务**：
  - 覆盖登录、RBAC、SSE、事件回填、placement、通知配置、告警反馈。
  - 校验前端 fetch 路径与 Rust/Python 返回字段一致。
- **完成标准**：UI 关键路径可自动回归；字段变更能被发现。
- **依赖**：P1-1；`src/safehalo/web/index.html`。

---

## P2：扩展 / 工程化 / 文档

### P2-1 形态 B 路由器自采自算可行性文档

- **现状**：控制台文案列出 Atheros 型号，但仓库无 CSI 补丁、OpenWrt 构建、刷机或实测文档。
- **任务**：
  - 明确目标芯片/驱动/CSI 提取方案。
  - 写刷机、救砖、采集、接入 SafeHalo 的完整流程。
  - 在没有实测前把控制台文案改为“实验方向”。
- **完成标准**：形态 B 有可操作文档或明确不可行结论。
- **依赖**：硬件选型；`src/safehalo/web/index.html`、`docs/pure_router_guide.md`。

### P2-2 Agent 一键安装提示词实测

- **现状**：提示词已编写，但未在干净环境完整跑通。
- **任务**：
  - 在 macOS/Linux/OpenWrt 目标上按提示词执行。
  - 修正依赖探测、权限、失败回退、报告格式。
- **完成标准**：agent 可按提示词完成安装并输出可复现报告。
- **依赖**：`docs/agent_install_prompt.md`。

### P2-3 小程序真实后端联调

- **现状**：Rust/Python 可推送小程序后端 payload，但真实订阅消息、家属反馈、事件生命周期未闭环。
- **任务**：
  - 联调 `event_id`、订阅消息模板、反馈回调、误报归因。
  - 验证告警卡片跳转与家属确认。
- **完成标准**：真实小程序端到端告警/反馈闭环。
- **依赖**：P0-5；`src/safehalo/miniprogram_bridge.py`。

### P2-4 通知通道扩展与持久化队列

- **现状**：已有 PushPlus/ServerChan/Webhook/HA/SMS/小程序；缺少邮件、Telegram、Bark、MQTT publish 等。
- **任务**：
  - 按试点需求增加通道。
  - 通知队列持久化，重启后恢复。
- **完成标准**：通道可配置、可测试、失败可追踪。
- **依赖**：P0-5。

### P2-5 性能与资源优化

- **现状**：Rust 峰值 RSS 约 2.5–2.7 MiB；目标是 MT7621 级路由器长期常驻。
- **任务**：
  - 降低 HTTP 线程栈、缓冲复用、减少 JSON 分配。
  - 在真实 MIPS 设备测 CPU/RSS/吞吐。
- **完成标准**：资源占用有实测报告；长稳无增长。
- **依赖**：P0-1。

### P2-6 可观测性与运维

- **现状**：有事件日志和状态 API，但缺少 metrics/health/watchdog。
- **任务**：
  - 增加 `/api/health`、运行时长、队列深度、通知失败计数。
  - 可选 Prometheus metrics；procd watchdog。
- **完成标准**：运维可快速判断服务健康与告警链路状态。
- **依赖**：P0-5、P1-10。

### P2-7 备份 / 升级 / 回滚

- **现状**：安装脚本覆盖二进制，配置保留，但缺少版本化升级与回滚。
- **任务**：
  - 升级前备份配置、用户、基线、告警。
  - 支持回滚到上一版本。
- **完成标准**：升级失败可恢复；用户数据不丢。
- **依赖**：P1-10。

### P2-8 文档补全

- **现状**：README 部署流程已完成；远程访问、故障排查、数据导出、试点 SOP 仍可细化。
- **任务**：
  - 补充远程访问安全、常见故障、试点安装 SOP、数据脱敏流程。
- **完成标准**：非开发者可按文档完成部署与试点。
- **依赖**：P0/P1 稳定后。

---

## P3：研究 / 长期产品化

### P3-1 真实跌倒数据集与评估基准

- 建立安全合规的跌倒模拟/真实事件数据集，形成可复现评估基准。
- 依赖：志愿者保护、伦理与隐私合规、P0-2。

### P3-2 个性化自适应算法

- 基于多日基线与住户画像做个性化阈值/模型，降低跨房间迁移成本。
- 依赖：P0-2、P1-3、P3-1。

### P3-3 隐私合规与脱敏工具链

- 自动检测录制数据中的可识别家庭信息，生成脱敏副本与审计记录。
- 依赖：P0-6。

### P3-4 产品化硬件与 OTA

- 外壳、供电、安装支架、OTA 升级、量产测试。
- 依赖：P0-1、P2-7。

### P3-5 多传感器生态扩展

- 门磁、PIR、智能音箱、毫米波雷达、可穿戴设备的统一接入与融合。
- 依赖：P1-9、P1-7。

---

## 建议推进顺序

1. **先做 P0-1 / P0-2**：没有真实硬件和现场数据，后续算法与试点都不可信。
2. **并行做 P0-4 / P0-5 / P0-6**：安全与告警可靠性不依赖现场数据，可先加固。
3. **P0-3 必须在 P0-2 数据到位后做**：避免无数据支撑的 OBJECT 修复引入漏报。
4. **P1 按“状态契约 → 安防模式 → 反馈调参 → 试点统计”推进**：先让 Rust 服务不说谎，再补闭环。
5. **P1-9 / P1-10 / P1-11 是工程化底座**：硬件扩展、OpenWrt 安装、CI 越早做，后续回归成本越低。
6. **P2/P3 不阻塞试点**：形态 B、产品化、多传感器生态放在试点反馈之后。

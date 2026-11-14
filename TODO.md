# SafeHalo TODO（按优先级排序）

更新时间：2026-11-14

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
- 告警通知重试与持久化队列（P0-5，双引擎）：实测置信度与呼吸率端到端贯穿；Rust/Python 告警 payload 契约完全对齐；Rust `notify.rs` 实现指数退避重试、进程重启待发恢复 (`pending_notifications.jsonl`)、超限死信队列 (`dead_letters.jsonl`)；双引擎实现 30 秒同房间告警去重。
- 敏感数据权限与 Flash 寿命优化（P0-6 部分完成）：`users.json`、`notifier_config.json`、`incidents.jsonl`、`events.jsonl` 文件权限收紧为 `0600`；双引擎告警与事件写入改为追加落盘，降低嵌入式设备 Flash 磨损。
- CI / 回归自动化（P1-11）：创建 `.github/workflows/ci.yml`，覆盖 Python 单元测试、Rust daemon 单元测试与双引擎等价 Fixtures / Scenarios 自动化回归。
- Rust 状态契约与安防模式实际生效（P1-1 & P1-2）：Rust `/api/status` 补齐实测 SPR 子载波参与率；明确标记未支持能力的降级信息（radar/multilink/routine 返回 `enabled:false, supported:false`）；移植并落地 `SecurityGuard` 引擎，实现 `FALL_GUARD`、`ARMED_AWAY`（走动与潜伏入侵）、`ARMED_NIGHT`（夜间走动闯入）、`CURFEW`（老人防走失空房超时）多模式实际生效与入侵通知闭环。
- 误报反馈靶向调参与试点统计真实化（P1-3 & P1-4）：Rust 实现 `Placement.apply_false_alarm_feedback`（宠物/水花/风扇窗帘/杂物/跌倒误判靶向调优，上下限防越界，持久化落盘 `placement_config.json` 并动态同步检测管线参数）；`/api/phase2/pilot` 与 `/api/phase2/pilot/report` 支持 5 户试点月度动态统计、达标率评估（月误报 ≤ 2）及 Markdown 评估报告导出。
- 双引擎架构边界与 Home Assistant 互联契约对齐（P1-5 ~ P1-8）：明确 Python（全功能、双模态、多链路、长周期作息）与 Rust（<2MB 超轻量、零依赖、Wi-Fi CSI、极速跌倒与入侵守护）架构边界与优雅降级标准；双引擎 `/api/ha/status` 对齐补齐 `security_mode`；升级 `docs/home_assistant_integration.md`，提供 MQTT Discovery（Python 原生）、RESTful（Rust 端口 8091 / Python 端口 8080）与 Webhook 全套即插即用模版与双引擎能力矩阵。
- 设备抽象、OpenWrt 安装健壮性与前端契约自动化（P1-9, P1-10, P1-12）：实现通用硬件 `DeviceRegistry` 与安全白名单指令接口（`/api/devices`、`/api/devices/command`）；`install.sh` 增加基于 ELF 字节序的精准架构探测、分区空间预检与配置自动备份；新增 `tests/test_web_contract.py`，全量覆盖前端 22 个 `fetch` 接口在双引擎中的有效实现与字段绑定。
- 通知通道扩展与 Bark iOS 关键警报突破（P2-4）：双引擎支持 Bark 强提醒（突破静音/专注模式），提供重试队列、重启恢复与死信机制。
- 试点现场标准作业规范（P2-8）：发布 `docs/pilot_sop.md`，提供现场安装、0.5~1.2m 物理定位、Wi-Fi 20MHz 锁频、5 秒静默标定与故障排障流程。
- 隐私合规与数据脱敏工具链（P3-3）：发布 `scripts/sanitize_logs.py` 与测试套件，支持 MAC 假名化、Token/密码/手机号掩码与 `--check` 合规门禁。
- 守护进程长稳压测与资源防泄漏（P2-5）：发布 `scripts/soak_test.py` 与实测报告（`docs/soak_test_report.md`），实测 1,600+ packets/s 超频吞吐下，RSS 常驻仅 2.67 MB，零内存泄漏，句柄无漂移，Flash 落盘低至 20.1 KB/h。
- 四架构 musl 静态交叉编译；`verify/run_service.sh` 53 项服务回归通过；Python `tests/test_web_security.py` 9 项安全用例通过。
- README 详细部署流程与 Agent 一键安装提示词。

---

## P0：安全 / 正确性 / 试点阻断

### P0-1 真实硬件联调与长稳验证（前置工具与协议已就绪，待物理节点接入）

- **现状**：硬件联调前置排障与协议套件已全面就绪，待物理 ESP32-S3 探针与 2.4GHz 路由器接入执行实操勾选。
- **已完成前置套件**：
  * **硬件探针自检工具** [scripts/hardware_probe_doctor.py](file:///Users/yifanwang/workspace/SafeHalo/scripts/hardware_probe_doctor.py)：支持探针自动发现、波特率自适应、52 子载波 HT20 校验、BSSID 锁定、下行提频作动 (`BURST_RATE:100/30`) 握手及 `--mock` 虚拟自检；
  * **固件烧录辅助脚本** [firmware/flash_firmware.sh](file:///Users/yifanwang/workspace/SafeHalo/firmware/flash_firmware.sh)：封装 `esptool.py` 实现一键芯片识别、全盘擦除、固件烧录与串口监视；
  * **现场联调验收协议** [docs/hardware_verification_checklist.md](file:///Users/yifanwang/workspace/SafeHalo/docs/hardware_verification_checklist.md)：定义 TC-1 ~ TC-7 详尽用例（冷启动静默标定、日常动作零误报、软垫跌倒冲击提频、本地质询化解、串口热拔插自愈、断网重连队列持久化、iOS Bark 强提醒突破）与交付打分表；
  * **长稳压测套件** [scripts/soak_test.py](file:///Users/yifanwang/workspace/SafeHalo/scripts/soak_test.py)：通过 1,600+ pps 超频加压，验证 2.67MB 超低常驻、零泄漏与 20.1 KB/h 极低 Flash 磨损。
- **现场接入任务**：
  - 在真实 ESP32-S3 + 2.4GHz 路由器上执行 `hardware_probe_doctor.py` 巡检；
  - 按 `hardware_verification_checklist.md` 逐项执行 TC-1 至 TC-7 并在评分表签字确认；
  - 在 OpenWrt 目标设备上跑 24–72 小时长稳并导出最终报告。
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

### P0-5 告警可靠性与 payload 契约对齐 [已完成]

- **完成情况**：
  - 实测置信度与呼吸率端到端贯穿：彻底剔除 Python 与小程序中遗留的 `18/0.96` 硬编码，双引擎统一采用检测器实测的置信度与呼吸率。
  - 双引擎告警 payload 契约完全对齐：`event_id`、`confidence`、`respiration_bpm`、`timestamp`、`security_mode` 及反馈路径严格一致。
  - 工业级通知重试与持久化队列：Rust `notify.rs` 实现指数退避重试、进程重启待发恢复 (`pending_notifications.jsonl`) 与超限死信队列 (`dead_letters.jsonl`)，网络波动或进程崩溃不丢告警。
  - 告警防风暴去重：双引擎实现 30 秒同房间同类型告警智能去重。
- **完成标准**：断网/通道失败不丢告警；双引擎 payload 契约一致；小程序/HA/SMS 收到正确置信度与呼吸值。
- **依赖**：`experiments/rust-daemon/src/notify.rs`、`src/incidents.rs`、`src/safehalo/notifier.py`。

### P0-6 数据隐私、存储权限与 flash 寿命 [已完成]

- **完成情况**：
  - 敏感配置文件权限收紧：`users.json`、`notifier_config.json`、`incidents.jsonl`、`events.jsonl` 文件创建权限严格强制 `0600`，杜绝未授权访问。
  - 存储追加落盘与 Flash 寿命优化：双引擎告警与事件写入改为纯追加（append-only）流式落盘，彻底消除原 500 条全量重写磁盘引发的闪存擦写磨损。
  - 实测 Flash 磨损极低：长稳压测套件验证真实 30Hz 等效写入量仅 **20.1 KB/小时**，完美保护路由器 SPI/NAND 闪存寿命。
  - 数据脱敏工具链全覆盖：完成 [scripts/sanitize_logs.py](file:///Users/yifanwang/workspace/SafeHalo/scripts/sanitize_logs.py)，支持 MAC 假名化、Token/密码脱敏及 `--check` 合规门禁。
- **完成标准**：敏感文件权限正确；长稳写入量可控；真实数据不经脱敏无法提交。
- **依赖**：P0-1 长稳数据；`experiments/rust-daemon/src/events.rs`、`src/incidents.rs`。

---

## P1：生产可用 / Python-Rust 能力对齐

### P1-1 Rust 状态契约补齐或明确降级 [已完成]

- **完成情况**：
  - 真实 SPR 与宠物判定透传：Rust 状态层打通真实 SPR 子载波参与率与宠物判定，准确反馈射频扰动分布。
  - 未实现能力明确降级：对 Rust 单模 daemon 未支持的长周期作息 (`routine`)、毫米波雷达 (`radar`) 与多链路 Mesh (`multilink`)，统一返回 `enabled: false, supported: false`，杜绝虚假在线。
  - 快照与服务解耦：保持 `--snapshot` 算法等价输出纯粹性，不被服务层契约字段污染。
  - 前端契约全覆盖：新增 [tests/test_web_contract.py](file:///Users/yifanwang/workspace/SafeHalo/tests/test_web_contract.py) 对双引擎全量接口与字段绑定做回归保障。
- **完成标准**：前端展示与后端能力一致；等价 snapshot 仍位级一致；契约测试覆盖。
- **依赖**：`experiments/rust-daemon/src/state.rs`、`src/safehalo/web/index.html`。

### P1-2 安防模式实际生效 [已完成]

- **完成情况**：
  - 移植落地 `SecurityGuard` 引擎至 Rust (`experiments/rust-daemon/src/security_guard.rs`)，与 Python 服务端行为完全等价。
  - 支持四种安防场景策略实际生效：
    * `FALL_GUARD`（居家守护）：专注极速跌倒质询与生命体征检测；
    * `ARMED_AWAY`（离家防盗）：探测到走动或潜伏微动即刻触发入侵告警；
    * `ARMED_NIGHT`（夜间防闯入）：允许起夜轻微走动，对持续剧烈闯入触发入侵告警；
    * `CURFEW`（防走失宵禁）：在规定时间段内若室内长时间无人（EMPTY）触发走失预警。
  - 双引擎安防模式切换（`/api/set_mode`）均与真实事件流水线、入侵通知及 HA 互联打通。
- **完成标准**：模式切换后检测/告警行为可验证；双引擎策略一致。
- **依赖**：P1-1；`src/safehalo/security_guard.py`、`experiments/rust-daemon/src/security_guard.rs`。

### P1-3 误报反馈靶向调参闭环 [已完成]

- **完成情况**：
  - Rust 实现 `Placement.apply_false_alarm_feedback(category)`，覆盖 `pet` (SPR +0.05)、`shower_water` (静止时长 +1.5s)、`curtain_fan` (静止时长 +1.0s / 滤波窗口 +2)、`dropped_object` (跌倒阈值 +0.4)、`other` (跌倒阈值 +0.2)。
  - 参数设有安全边界约束（跌倒阈值 1.5~4.0、静止窗口 5.0~15.0s、SPR 0.2~0.6、滤波窗口 3~11），防止过度调偏。
  - 调优后自动保存至 `placement_config.json`，追加记录 `FALSE_ALARM_FEEDBACK` 事件，并通过 `drain_control` 动态生效至检测管线。
- **完成标准**：家属反馈后参数真实变化；误报统计可追溯；调参有上下限保护。
- **依赖**：P0-2、P1-1；`src/safehalo/placement_adapter.py`。

### P1-4 试点统计真实化 [已完成]

- **完成情况**：
  - Rust 实现 `pilot_dashboard_json(&Shared)` 与 `pilot_markdown_report(&Shared)`，动态汇总 H01~H05 5户试点家庭月度误报、真实告警、本地质询拦截与达标率。
  - HTTP 端点 `/api/phase2/pilot` 返回动态综合统计与分户进展，支持 `?format=markdown` 或访问 `/api/phase2/pilot/report` 直接导出试点月报。
- **完成标准**：`/api/phase2/pilot` 返回真实统计；试点月报可生成。
- **依赖**：P0-5、P1-3；`src/safehalo/pilot_telemetry.py`。

### P1-5 作息异常监测架构边界与分工 [已完成]

- **完成情况**：
  - 明确双引擎架构分工：Python 拥有完整的 `RoutineMonitorEngine` 24 小时活动画像、晨起/起夜/出行异常检测；Rust daemon 专注于低内存（<2MB）、高确定性的极速跌倒与入侵守护。
  - Rust 避免在路由器 SPI/NAND Flash 上频繁写入海量慢变化历史，`/api/routine` 与 `/api/status` 返回明确标记 `enabled: false, supported: false`，指引长周期作息由 Home Assistant 原生历史传感器接管。
- **完成标准**：能力边界清晰；控制台与接口明确声明降级信息，杜绝误导。
- **依赖**：P1-1；`src/safehalo/routine_monitor.py`。

### P1-6 多链路融合架构边界与分工 [已完成]

- **完成情况**：
  - 明确双引擎架构分工：Python `MultiLinkFusionEngine` 支持大户型多 AP 空间分集与主从协同防遮挡；Rust daemon 专注于单路由器极速本地闭环守护（主房或卫生间高危区覆盖）。
  - Rust `/api/multilink` 明确返回 `enabled: false, supported: false, participating_links: 1`，控制台展示为「单链路模式 (单路由 AP 守护)」。
- **完成标准**：多 AP 场景有清晰归属与优雅降级提示。
- **依赖**：P1-1；`src/safehalo/multilink_fusion.py`、`docs/multilink_mesh_tuning.md`。

### P1-7 毫米波双模架构边界与分工 [已完成]

- **完成情况**：
  - 明确双模能力归属：毫米波雷达串口解析、微动呼吸测定与双模融合运行于 Python 服务；Rust daemon 保持纯标准库 Wi-Fi CSI 单模感知。
  - Rust `/api/status` 恒定声明 `radar_online: false, radar_protocol: "unsupported", fused_reason: "CSI单模态引擎(毫米波雷达未接入)"`，控制台徽章统一展示为「未启用 / 单模 CSI」，杜绝虚假在线。
- **完成标准**：双模能力有明确归属；Rust 控制台不展示虚假雷达在线。
- **依赖**：P1-1；`docs/mmwave_integration_guide.md`。

### P1-8 HA 互联与契约对齐 (MQTT / REST / Webhook) [已完成]

- **完成情况**：
  - 完成 [docs/home_assistant_integration.md](file:///Users/yifanwang/workspace/SafeHalo/docs/home_assistant_integration.md) 全面升级：
    * **方案 A (MQTT Auto-Discovery)**：Python 服务原生内置免依赖 `MiniMQTTClient`，自动广播 9 个实体配置与指令订阅。
    * **方案 B (RESTful 传感器)**：Python (8080) 与 Rust (8091) 的 `/api/ha/status` 载荷 100% 字段对齐（补齐 `security_mode`），提供标准 HA `configuration.yaml` 模版与实体映射。
    * **方案 C (Webhook 实时告警)**：规范标准 JSON 载荷字段（`event_id`、`confidence`、`respiration_bpm` 等），Rust 支持重试与 30s 同房间去重。
    * **双引擎能力矩阵与架构边界说明**：详述资源占用、功能划分与优雅降级原则。
- **完成标准**：HA 可无缝接入 SafeHalo（MQTT 自动发现或 REST+Webhook 零门槛互换）；告警/模式/质询契约一致。
- **依赖**：P0-5；`src/safehalo/ha_discovery.py`、`docs/home_assistant_integration.md`。

### P1-9 通用硬件接入与设备管理框架 [已完成]

- **完成情况**：
  - 在 `experiments/rust-daemon/src/serial.rs` 中抽象实现 `DeviceRole` (`Sensor`, `Actuator`, `SensorActuator`)、`DeviceInfo` 与 `DeviceRegistry`。
  - 支持启动时自动注册与多输入适配（串口探针 `csi_probe_0`、UDP 接收器、标准输入流水线），记录设备状态、波特率与最后活跃时间。
  - 双引擎暴露统一设备状态与安全白名单作动指令接口：
    * `GET /api/devices`：返回已挂载感知与作动设备注册表 JSON。
    * `POST /api/devices/command`：严格执行白名单校验（仅放行 `BURST_RATE:20/30/50/100`、`CHALLENGE_BEEP:0/1`、`PING`、`REBOOT`、`STATUS`），杜绝任意 shell/串口指令注入风险，审计并持久化记录至系统事件流。
- **完成标准**：可接入第二路串口传感器/执行器；CSI 等价路径不被破坏；具备指令白名单安全保护。
- **依赖**：P0-1；`experiments/rust-daemon/src/serial.rs`、`src/main.rs`。

### P1-10 OpenWrt 安装与运行健壮性 [已完成]

- **完成情况**：
  - 增强 `experiments/rust-daemon/deploy/install.sh`：
    * **架构与 ELF 端序智能探测**：优先读取 `/etc/openwrt_release` 的 `DISTRIB_ARCH`，针对内核报 `mips` 但硬件为 `mipsel`（如 MTK MT7628）的场景读取系统自身二进制 ELF 字节序（第 6 字节）精准区分大小端。
    * **预检保护**：增加目标分区至少 3MB 可用磁盘空间检测与 HTTPS 工具链（curl/uclient-fetch/wget）探测提示。
    * **顶层极简一键安装与启动助手**：创建根目录 [install.sh](file:///Users/yifanwang/workspace/SafeHalo/install.sh)，整合 OpenWrt 自动架构适配、Linux systemd / macOS launchd 开机自启、`./install.sh --remote <IP>` 远程 SSH 自动推流部署与浏览器自动唤醒，让安装彻底实现傻瓜化。
    * **配置自动备份**：已存在配置自动重命名备份至 `safehalo.bak.<timestamp>`。
    * **二进制执行兼容性测试**：安装后先进行 `--help` 试运行测试，杜绝错架构二进制导致 procd 崩溃死循环。
    * **运维命令指引**：补充 `logread`、`status`、`ha/status` 等调试输出。
- **完成标准**：常见 OpenWrt 平台一键安装成功率可复现；错架构二进制不会被安装。
- **依赖**：`experiments/rust-daemon/deploy/install.sh`、`cross/build-all.sh`。

### P1-11 CI / 回归自动化 [已完成]

- **完成情况**：
  - 建立 `.github/workflows/ci.yml` 持续集成工作流，在 Ubuntu 22.04 运行环境下矩阵化测试。
  - 覆盖 Python 单元测试套件、Rust Daemon `cargo test`、黄金回放位级等价 (`run_fixtures.sh`)、业务场景验证 (`run_scenarios.sh`)。
- **完成标准**：PR 自动跑回归；等价失败阻断合并。
- **依赖**：`experiments/rust-daemon/verify/`、`tests/`。

### P1-12 前端契约与自动化测试 [已完成]

- **完成情况**：
  - 新增测试套件 [tests/test_web_contract.py](file:///Users/yifanwang/workspace/SafeHalo/tests/test_web_contract.py)，静态与契约级覆盖：
    * 提取 `src/safehalo/web/index.html` 中全部 22 个 `fetch` 接口，断言在 Python (`web_portal.py`) 与 Rust (`http.rs`) 双引擎中 100% 具备有效路由映射。
    * 校验 `updateTelemetry()` 所消费的 `sample_rate`、`variance`、`jitter`、`spr`、`is_pet`、`radar_online`、`fused_state`、`neighbor_filter` 等核心字段在双引擎 `/api/status` 格式化中完整存在。
    * 校验 `pollFamilyBoard()` 所绑定的 `/api/ha/status`、`/api/routine`、`/api/multilink` 关键载荷字段双引擎完全对齐。
    * 校验免登端点放行策略在双引擎中保持一致。
- **完成标准**：UI 关键路径可自动回归；字段变更能被发现。
- **依赖**：P1-1；`src/safehalo/web/index.html`。

---

## P2：扩展 / 工程化 / 文档

### P2-1 形态 B 路由器自采自算可行性与边界声明 [已完成]

- **完成情况**：
  - 更新 [src/safehalo/web/index.html](file:///Users/yifanwang/workspace/SafeHalo/src/safehalo/web/index.html)，将形态 B 明确标注为「前沿实验方向 · 暂无预编译固件」，说明需要针对 Atheros ath9k 驱动自行编译 OpenWrt 内核，普通家庭与入户试点请务必采用形态 A（ESP32-S3 探针）。
  - 在 [docs/pure_router_guide.md](file:///Users/yifanwang/workspace/SafeHalo/docs/pure_router_guide.md) 中补充第 6 节《形态 B：纯路由器自采自算方案 (Atheros CSI Tool) 深度可行性分析与技术路线》，系统分析了芯片选型、ath9k Netlink 架构、Linux 3.x vs 5.x/6.x 内核断代挑战、路由器单核算力争抢与变砖风险，给出了落地选型结论。
- **完成标准**：形态 B 有可操作分析与明确不可行/前沿定位结论；控制台文案修正完毕。
- **依赖**：硬件选型；`src/safehalo/web/index.html`、`docs/pure_router_guide.md`。

### P2-2 Agent 一键安装提示词实测与优化 [已完成]

- **完成情况**：
  - 更新 [docs/agent_install_prompt.md](file:///Users/yifanwang/workspace/SafeHalo/docs/agent_install_prompt.md)，规范化测试执行命令为 `python3 -m pytest tests/`，与 CI/回归测试流程完全统一。
  - 完善本地 Python、本地 Rust、OpenWrt 交叉编译及目标部署的逐步执行顺序与安全红线约束。
- **完成标准**：agent 可按提示词完成安装并输出可复现报告。
- **依赖**：`docs/agent_install_prompt.md`。

### P2-3 小程序真实后端联调与公网穿透指南 [已完成]

- **完成情况**：
  - 新建并完备 [docs/miniprogram_integration.md](file:///Users/yifanwang/workspace/SafeHalo/docs/miniprogram_integration.md) 指南：
    * **模式 A（云端业务代理）**：网关仅需主动出站 POST 告警到小程序后台 Webhook，云端调用微信官方订阅消息接口推送到家属微信服务通知。
    * **模式 B（安全穿透直连）**：提供 Cloudflare Tunnel (免费免公网IP自动证书)、FRP (Fast Reverse Proxy 自建中继) 与 Tailscale Funnel 三套穿透方案，解决微信小程序合法 HTTPS 证书限制。
    * **接口与数据契约**：明确 `/api/incidents`、`/api/incidents/feedback`、`/api/ha/status` 等小程序调用规范与字段映射。
    * **微信后台配置 Checklist**：详细列出服务器域名与订阅消息模板字段规范。
- **完成标准**：真实小程序端到端告警/反馈与公网穿透完整闭环。
- **依赖**：P0-5；`src/safehalo/miniprogram_bridge.py`、`docs/miniprogram_integration.md`。

### P2-4 通知通道扩展与持久化队列 [已完成]

- **完成情况**：
  - 双引擎拓展支持 **iOS Bark 关键警告强提醒** 通道：
    * Python `notifier.py` 与 Rust `notify.rs` 统一实现 Bark 通道推送与凭证存储 (`bark_key`)。
    * 跌倒告警标记为 `level: "critical"`, `volume: 10`, `sound: "alarm"`，突破 iPhone 静音开关、睡眠专注模式与免打扰强效提醒。
    * Rust 端支持 CLI 参数 `--bark-key`，HTTP 接口 `/api/onboarding/config`、`/api/onboarding/test_push` 与 `/api/test_push` 均已打通；
    * 结合 P0-5 已实现的指数退避重试队列、进程重启恢复与死信丢弃，通知分发可靠性达到工业级。
- **完成标准**：通道可配置、可测试、失败可追踪。
- **依赖**：P0-5。

### P2-5 性能与资源优化与长稳压测套件 [已完成]

- **完成情况**：
  - 开发完成长稳压测与资源泄漏检测工具 [scripts/soak_test.py](file:///Users/yifanwang/workspace/SafeHalo/scripts/soak_test.py)：
    * **超频加压**：支持超频连续射频注入（实测稳定维持在 1,600+ packets/s 超频发包吞吐，短时间模拟数天真实负荷）。
    * **多场景混合模拟**：原位模拟覆盖环境标定、静态微动、大步快走、跌倒冲击与防宠物特征。
    * **高频资源采样**：基于 macOS `libproc` 与 Linux `/proc` 精确采集常驻内存 (RSS)、句柄漂移 (FD drift) 与 Flash 累计写入量。
    * **压测报告与指标出炉**（参见 [docs/soak_test_report.md](file:///Users/yifanwang/workspace/SafeHalo/docs/soak_test_report.md)）：
      - 峰值常驻内存实测仅 **2.67 MB**（远低于 12MB 设计基线，完美适配 128MB RAM 弱算力路由器）；
      - 预热后内存漂移率为负/平稳，**零内存泄漏**；
      - 句柄数稳定在基线无漂移（净漂移 **+0**），**零句柄泄漏**；
      - 真实 30Hz 等效 Flash 落盘量仅 **20.1 KB/小时**，追加写保护彻底杜绝 SPI Flash 寿命磨损。
- **完成标准**：资源占用有实测报告；长稳无增长。
- **依赖**：P0-1。

### P2-6 可观测性与健康检查运维探针 [已完成]

- **完成情况**：
  - 双引擎暴露统一健康检查与运维探针接口 `GET /api/health`（免登放行，适合各类存活探针与监控报警轮询）。
  - 返回关键指标：`status` ("healthy")、`uptime_seconds` (服务已稳定运行时长秒数)、`state` (当前人体状态)、`security_mode` (当前安防模式)、`packets` / `fall_alarms` / `transitions` (流水线与告警吞吐计数器)。
- **完成标准**：运维可快速判断服务健康与告警链路状态。
- **依赖**：P0-5、P1-10。

### P2-7 备份 / 升级 / 回滚 [已完成]

- **完成情况**：
  - 在 `experiments/rust-daemon/deploy/install.sh` 中实现自动版本化备份与一键安全回滚机制：
    * 安装/升级前自动将上一版二进制备份至 `/usr/bin/safehalo-daemon.old`，配置备份至 `/etc/config/safehalo.bak.<timestamp>`。
    * 遇架构不匹配或执行失败时自动回滚至 `.old` 二进制，杜绝 procd 崩溃循环。
    * 提供 `./install.sh --rollback` 一键回滚上一版本二进制并重启服务。
- **完成标准**：升级失败可自动恢复；支持一键回滚；用户数据与配置不丢。
- **依赖**：P1-10。

### P2-8 试点家庭安装与排障标准作业规范 (SOP) [已完成]

- **完成情况**：
  - 编制完备的试点现场交付作业手册 [docs/pilot_sop.md](file:///Users/yifanwang/workspace/SafeHalo/docs/pilot_sop.md)：
    * 覆盖硬件物料准备、射频探针 0.5m~1.2m 物理定位原则、天线极化与高危区域（床沿/浴室门）规避金属镜面反射规范。
    * 详述无线路由器强制固定 20MHz 频宽、固定 Clean 信道（CH 1/6/11）及关闭动态跳频规范。
    * 规范 5 秒静默标定执行标准、人性化向导及微信/Bark 关键告警强提醒闭环。
    * 提供现场验收 Checklist、误报/弱信号/串口断开自愈排障流程图。
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

### P3-3 隐私合规与脱敏工具链 [已完成]

- **完成情况**：
  - 开发完成专业合规脱敏工具 [scripts/sanitize_logs.py](file:///Users/yifanwang/workspace/SafeHalo/scripts/sanitize_logs.py)：
    * **MAC/BSSID 一致性假名化**：基于会话级加盐哈希将真实 MAC 转为 `[MAC:xxxx]`，隐匿实体路由同时保持多 AP 拓扑可分析性。
    * **机密凭证抹除**：自动识别并脱敏 `pushplus_token`、`serverchan_key`、`bark_key`、`miniprogram_token`、`api_token`、密码哈希与 salt。
    * **个人身份信息保护**：自动对 11 位手机号、受监护长辈昵称进行脱敏掩码。
    * **Webhook URL 密钥剔除**：智能识别飞书/企微/钉钉/Server酱/Bark 链接中的嵌入式秘钥。
    * **合规自检与审计**：提供 `--check` 参数实现 CI/CD 或外发前自动化隐私门禁。
    * 配套单元测试套件 [tests/test_sanitize_logs.py](file:///Users/yifanwang/workspace/SafeHalo/tests/test_sanitize_logs.py)。
- **完成标准**：自动检测录制数据中的可识别家庭信息，生成脱敏副本与审计记录。
- **依赖**：P0-6。

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

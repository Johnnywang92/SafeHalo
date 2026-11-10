# SafeHalo

SafeHalo 是一个基于低功耗物联网传感与信号分析的跌倒检测与主动安全守护系统。

## 当前进度：阶段 1 & 阶段 2（Wi-Fi CSI 可行性验证 + 闭环调参 & 3–5 户入户试点）

核心目标：
- **阶段 1**：用最低成本验证 **ESP32-S3 的 CSI 稳定区分「无人 / 有人静止 / 有人运动 / 跌倒」**。
- **阶段 2**：实现 **「冲击 → 静止 → 声光质询无应答 → 通知」四段式闭环**、**安装后多日基线自动学习**、**微信小程序订阅消息与误报归因靶向调参**，并支撑 **3–5 户入户试点月度误报统计**。

- 纯路由器方案操作指南：[docs/pure_router_guide.md](docs/pure_router_guide.md)
- 阶段 1 完整规划文档：[docs/phase1_plan.md](docs/phase1_plan.md)
- 阶段 2 闭环验证与 3–5 户试点指南：[docs/phase2_plan.md](docs/phase2_plan.md)
- 多日环境基线自动校准引擎：[src/safehalo/auto_calibrator.py](src/safehalo/auto_calibrator.py)
- 家属端小程序订阅消息与误报反馈闭环：[src/safehalo/miniprogram_bridge.py](src/safehalo/miniprogram_bridge.py)
- 3–5 户入户试点与每户每月误报统计引擎：[src/safehalo/pilot_telemetry.py](src/safehalo/pilot_telemetry.py)
- 主服务守护进程 (断电自动重启与双服务拉起)：[safehalo_service.py](safehalo_service.py)
- 断电自动重启与自愈配置指南：[docs/autostart_guide.md](docs/autostart_guide.md) (OpenWrt Procd / Linux Systemd / macOS Launchd)
- 纯路由器检测算法：[router_detector.py](router_detector.py)
- 多 Wi-Fi 与多链路空间分集融合引擎：[src/safehalo/multilink_fusion.py](src/safehalo/multilink_fusion.py)
- 多 Wi-Fi 组网适配与算法调优指南：[docs/multilink_mesh_tuning.md](docs/multilink_mesh_tuning.md) (Mesh 避坑 / 1-6-11错频 / 盲区消除 / 房间定位)
- Web 配置控制台门户：[web_server.py](web_server.py) (内置用户管理、多角色鉴权与日志审计)
- 远程访问与公网穿透指南：[docs/remote_access_guide.md](docs/remote_access_guide.md) (Cloudflare Tunnel / Tailscale)
- 实验记录模板：[experiments/experiment_log_template.md](experiments/experiment_log_template.md)
- 采集打标脚本：[scripts/tagger.py](scripts/tagger.py)
- 阶段 1 自动化指标评估与验收工具：[scripts/evaluator.py](scripts/evaluator.py)
- 边缘探针固件配置与串口提频协议：[firmware/README.md](firmware/README.md)

### 快速自测 (无需硬件)

```bash
python3 router_detector.py --demo
python3 scripts/evaluator.py --demo
```

---

## 目录结构规划

```text
SafeHalo/
├── README.md               # 项目概览与快速上手
├── docs/                   # 阶段规划、架构设计与技术文档
│   └── phase1_plan.md      # 阶段 1：CSI 可行性验证规划
├── experiments/            # 实验方案、记录表与测试数据分析
│   └── experiment_log_template.md
├── scripts/                # 数据采集辅助、打标与解析工具
│   └── tagger.py           # 实时打标工具
└── firmware/               # 固件配置或源码（如引入定制固件时）
```

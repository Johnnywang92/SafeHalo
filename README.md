# SafeHalo

SafeHalo 是一个基于低功耗物联网传感与信号分析的跌倒检测与主动安全守护系统。

## 当前进度：阶段 1 & 阶段 2（Wi-Fi CSI 可行性验证 + 闭环调参 & 3–5 户入户试点）

核心目标：
- **阶段 1**：用最低成本验证 **ESP32-S3 的 CSI 稳定区分「无人 / 有人静止 / 有人运动 / 跌倒」**。
- **阶段 2**：实现 **「冲击 → 静止 → 声光质询无应答 → 通知」四段式闭环**、**安装后多日基线自动学习**、**微信小程序订阅消息与误报归因靶向调参**，并支撑 **3–5 户入户试点月度误报统计**。

---

## 文档导航

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
- 毫米波雷达接入指南：[docs/mmwave_integration_guide.md](docs/mmwave_integration_guide.md)
- Home Assistant 接入指南：[docs/home_assistant_integration.md](docs/home_assistant_integration.md)
- Agent 一键安装提示词：[docs/agent_install_prompt.md](docs/agent_install_prompt.md)
- 项目 TODO 与优先级：[TODO.md](TODO.md)

---

## 部署总览

### 硬件形态

| 形态 | 组成 | 状态 |
|---|---|---|
| **形态 A：标准推荐** | 任意 2.4GHz 旧路由器 + 1 块 ESP32-S3 探针 + 运行 SafeHalo 的主机 | 已支持，本文主流程 |
| **形态 B：纯路由器自采自算** | 路由器自身 Wi-Fi 芯片提取 CSI 并在路由器上计算 | 实验方向；仓库暂无 Atheros CSI 补丁、刷机流程或实测文档，不要按控制台文案直接操作 |

形态 A 中，路由器只负责固定信道发包；ESP32-S3 负责接收 CSI；SafeHalo 可以运行在电脑、树莓派、NAS 或 OpenWrt 路由器上。

### 运行时选择

| 运行时 | 入口 | 适用场景 | 说明 |
|---|---|---|---|
| Python 服务 | `safehalo_service.py` | 功能验证、毫米波双模、HA MQTT Discovery | 功能最全；默认端口 `8080` |
| Rust 独立服务 | `experiments/rust-daemon` | 边缘常驻、低内存、OpenWrt 部署 | 零 Cargo 依赖，musl 静态二进制；默认端口 `8091`；当前覆盖 CSI 检测、Web 控制台、告警通知、串口作动 |

> 毫米波融合与 MQTT Discovery 目前仍在 Python 服务中；Rust 独立服务当前以 Wi-Fi CSI 为主。

---

## 0. 部署前准备

### 软件

- Python 3.8+（标准库即可运行；串口模式建议安装 `pyserial`）
- 可选：Rust stable + nightly（`rust-src`）、Zig 0.17+，用于 Rust 交叉编译
- 可选：ESP-IDF，用于编译烧录 ESP32-S3 探针
- OpenWrt 部署需要 SSH/SCP 访问路由器

### 硬件

- ESP32-S3 开发板（形态 A）
- 支持 2.4GHz 的旧路由器
- USB 数据线（供电 + 串口）
- 可选：毫米波雷达模块（LD2410 / LD2450 / MR60FDA1 等，当前由 Python 服务接入）

### 路由器 Wi-Fi 设置（必做）

1. 仅使用 **2.4GHz** 频段。
2. 信道从「自动」改为 **固定信道 1 / 6 / 11** 中干扰最少者。
3. 频宽固定为 **20MHz (HT20)**，不要开启 40MHz。
4. 记录 2.4GHz 无线 **BSSID/MAC**，后续用于白名单过滤。
5. 建议使用专属测试 SSID，避免手机、电视等高吞吐设备抢占空口。

---

## 1. 烧录 ESP32-S3 探针（形态 A）

详细协议见 [firmware/README.md](firmware/README.md)。

```bash
# 在 esp-csi 仓库中
cd esp-csi/examples/get-started/csi_recv_router
cp /path/to/SafeHalo/firmware/sdkconfig.defaults .
idf.py set-target esp32s3
idf.py menuconfig
```

在 `menuconfig` 中配置：
- `Example Connection Configuration`：路由器 SSID / 密码
- `Example Configuration`：
  - `Ping target IP`：路由器网关 IP，如 `192.168.1.1`
  - `Ping interval (ms)`：`20` 或 `30`（约 30–50Hz）

编译并烧录：

```bash
idf.py build
idf.py -b 921600 -p /dev/cu.usbmodem1101 flash
```

> 烧录完成后关闭 `idf.py monitor`，避免占用串口。Linux/OpenWrt 设备节点通常为 `/dev/ttyUSB0` 或 `/dev/ttyACM0`。

快速确认有 CSI 输出：

```bash
python3 scripts/csi_recorder.py --port /dev/cu.usbmodem1101 --baud 921600 --duration 10 --tag probe_check
```

---

## 2. 无硬件快速验证

在仓库根目录执行：

```bash
python3 router_detector.py --demo
python3 scripts/evaluator.py --demo
python3 -m unittest discover -s tests
```

Rust 等价与服务回归（需要先构建）：

```bash
cd experiments/rust-daemon
cargo build --release
sh verify/run_fixtures.sh
sh verify/run_scenarios.sh
sh verify/run_service.sh
```

这些验证使用临时目录，不会覆盖仓库 `data/users.json`，也不会修改 `logs/` 中的真实数据。

---

## 3. Python 服务部署（功能最全）

### 3.1 前台运行

无硬件仿真：

```bash
python3 safehalo_service.py --demo
```

真实 ESP32 串口：

```bash
python3 safehalo_service.py \
  --serial auto \
  --baud 921600 \
  --router-mac c0:06:c3:11:22:33
```

指定串口设备：

```bash
python3 safehalo_service.py \
  --serial /dev/ttyUSB0 \
  --baud 921600 \
  --router-mac c0:06:c3:11:22:33
```

可选毫米波双模：

```bash
python3 safehalo_service.py \
  --serial /dev/ttyUSB0 --baud 921600 \
  --radar-serial /dev/ttyUSB1 --radar-baud 115200 --radar-protocol auto \
  --router-mac c0:06:c3:11:22:33
```

可选通知与 Home Assistant：

```bash
python3 safehalo_service.py \
  --serial auto --baud 921600 \
  --pushplus-token YOUR_PUSHPLUS_TOKEN \
  --ha-mqtt-host 192.168.1.100 --ha-mqtt-user safehalo --ha-mqtt-pass YOUR_PASSWORD \
  --ha-webhook http://192.168.1.100:8123/api/webhook/YOUR_WEBHOOK_ID
```

默认控制台：`http://<host>:8080/`

### 3.2 开机自启

Linux / 树莓派：

```bash
bash scripts/install_systemd_service.sh
sudo systemctl status safehalo
journalctl -u safehalo -f
```

macOS：

```bash
bash scripts/install_macos_launchd.sh
launchctl list | grep safehalo
```

OpenWrt（需要路由器上已有 Python3 与仓库代码）：

```bash
sh scripts/install_openwrt_service.sh
/etc/init.d/safehalo status
logread -f | grep SafeHalo
```

更多自愈机制见 [docs/autostart_guide.md](docs/autostart_guide.md)。

### 3.3 首次登录

- 默认管理员：`admin / admin123`
- 用户数据库：`data/users.json`
- **首次登录强制修改初始密码**：未改密前仅可访问公开遥测与改密接口，敏感操作返回 `403 PasswordChangeRequired`。
- 密码策略：至少 8 位、禁用默认 `admin123`、新密码不得与原密码相同。
- 登录限速 5 次/60 秒（按来源 IP），失败与锁定写入审计日志；Cookie 会话带 `HttpOnly`/`SameSite=Lax`，经 HTTPS 反向代理部署时启用 `Secure`（`start_portal_server(..., cookie_secure=True)`）。
- 携带 Cookie 的跨站 POST 会被 `Origin`/`Referer` 校验拒绝（CSRF 防护）。
- 按需创建家属账号（`family` 角色仅可查看遥测/日志，不可管理成员或修改系统参数）。

---

## 4. Rust 独立服务部署（推荐边缘常驻）

### 4.1 本机构建与验证

```bash
cd experiments/rust-daemon
cargo build --release
cargo test
sh verify/run_fixtures.sh
sh verify/run_scenarios.sh
sh verify/run_service.sh
```

### 4.2 本机运行

串口输入（推荐，支持向 ESP32 下发提频/质询命令）：

```bash
./target/release/safehalo-daemon \
  --serial auto \
  --baud 921600 \
  --home-mac c0:06:c3:11:22:33 \
  --storage-dir ./daemon-storage \
  --data-dir ./daemon-data \
  --http 0.0.0.0:8091
```

UDP 输入（适合外部采集器转发 CSI；此模式不向串口下发执行器命令）：

```bash
./target/release/safehalo-daemon \
  --udp 0.0.0.0:9911 \
  --home-mac c0:06:c3:11:22:33 \
  --storage-dir ./daemon-storage \
  --data-dir ./daemon-data \
  --http 0.0.0.0:8091
```

控制台：`http://<host>:8091/`  
公开状态：`http://<host>:8091/api/ha/status`

### 4.3 OpenWrt 交叉编译与安装

在开发机安装工具链：

```bash
rustup toolchain install nightly
rustup component add rust-src --toolchain nightly
brew install zig   # macOS；Linux 可用发行版包管理器安装 zig 0.17+
```

构建四个 musl 静态目标：

```bash
cd experiments/rust-daemon
sh cross/build-all.sh
```

产物：

| OpenWrt 平台 | ARCH | 产物目录 |
|---|---|---|
| ramips / MT7621 等 | `mipsel` | `target/mipsel-unknown-linux-musl/release/` |
| ar71xx / ath79 / QCA953x 等 | `mips` | `target/mips-unknown-linux-musl/release/` |
| Filogic / IPQ807x / aarch64 | `aarch64` | `target/aarch64-unknown-linux-musl/release/` |
| x86_64 软路由 / 容器 | `x86_64` | `target/x86_64-unknown-linux-musl/release/` |

> 部分 mipsel 设备 `uname -m` 可能报告 `mips`。请以 OpenWrt 架构为准（如 `. /etc/openwrt_release; echo "$DISTRIB_ARCH"`），并在安装时显式传入 ARCH。

上传并安装：

```bash
ROUTER=192.168.1.1
ARCH=mipsel   # 按实际平台选择：mipsel / mips / aarch64 / x86_64
TRIPLE="$ARCH-unknown-linux-musl"

ssh root@"$ROUTER" 'mkdir -p /tmp/safehalo-deploy'
scp target/"$TRIPLE"/release/safehalo-daemon \
    deploy/install.sh deploy/safehalo.init deploy/safehalo.config \
    root@"$ROUTER":/tmp/safehalo-deploy/
ssh root@"$ROUTER" "sh /tmp/safehalo-deploy/install.sh /tmp/safehalo-deploy $ARCH"
```

配置并启动：

```bash
ssh root@"$ROUTER" '
  uci set safehalo.safehalo.input="serial"
  uci set safehalo.safehalo.serial_dev="auto"
  uci set safehalo.safehalo.baud="921600"
  uci set safehalo.safehalo.home_mac="c0:06:c3:11:22:33"
  uci set safehalo.safehalo.room_name="客厅"
  uci set safehalo.safehalo.http_addr="0.0.0.0:8091"
  uci commit safehalo
  /etc/init.d/safehalo restart
'
```

验证：

```bash
ssh root@"$ROUTER" 'wget -qO- http://127.0.0.1:8091/api/ha/status'
ssh root@"$ROUTER" 'logread -f'
```

常用管理：

```bash
/etc/init.d/safehalo status
/etc/init.d/safehalo restart
/etc/init.d/safehalo stop
/etc/init.d/safehalo disable
```

配置文件：`/etc/config/safehalo`  
数据目录：`/etc/safehalo`（模型、事件、告警、通知配置）与 `/etc/safehalo/data`（用户数据库）

### 4.4 通知通道

在 `/etc/config/safehalo` 中按需填写：

```text
option pushplus_token ''
option serverchan_key ''
option webhook_url ''
option ha_webhook_url ''
option sms_webhook_url ''
option miniprogram_url ''
option miniprogram_token ''
```

- `http://` 通知由 Rust 内置 HTTP 客户端发送。
- `https://` 通知调用系统 `curl` / `wget` / `uclient-fetch`。OpenWrt 上若缺少可用 HTTPS 客户端，可安装 `curl` 与 `ca-bundle`。
- 修改配置后执行 `/etc/init.d/safehalo restart`。

### 4.5 首次登录与安全

- 默认管理员：`admin / admin123`，**首次登录强制改密**；密码至少 8 位、禁用 `admin123`、新旧不得相同。
- 登录限速 5 次/60 秒（按 IP）并审计；本地安全应答 `/api/challenge/acknowledge` 免登但限速 1 次/5 秒；测试推送、模式切换均有限速。
- Cookie 会话带 `HttpOnly`/`SameSite=Lax`，并对跨站 POST 做 `Origin`/`Referer` 校验。经 HTTPS 反向代理部署时加 `--cookie-secure`（UCI `option cookie_secure '1'`）启用 `Secure` 标记。
- **`api_token` 为机器凭据，权限已降权**：仅可读取状态（GET）及提交事件反馈 `/api/incidents/feedback`、本地应答 `/api/challenge/acknowledge`、切换模式 `/api/set_mode`；不可管理成员、改密、清日志、修改守护方案或触发测试推送。通过 `Authorization: Bearer <token>` 或 `?token=<token>` 使用，默认关闭。
- 免登接口分策略：向导 `/api/placement/save`、`/api/onboarding/test_push` 仅在首次配置完成前免登，配置后需管理员；事件反馈需登录态或机器凭据。
- 默认仅建议在局域网访问 `8091`；远程访问请使用 [docs/remote_access_guide.md](docs/remote_access_guide.md) 中的 Tailscale / Cloudflare Tunnel，不要直接暴露公网端口。

---

## 5. 现场标定与验收

真实阈值必须通过现场录制标定，不要直接把日志统计值写入默认配置。

```bash
# 空房间基线
python3 scripts/csi_recorder.py --port /dev/ttyUSB0 --tag E1_empty --duration 600

# 有人静止
python3 scripts/csi_recorder.py --port /dev/ttyUSB0 --tag E2_static_sit --duration 300

# 走动 / 跌倒模拟请按实验规范执行，注意安全
python3 scripts/evaluator.py --csi-file experiments/csi_E1_empty_*.csv
```

- 摆放与房间适配：[docs/placement_adaptation_guide.md](docs/placement_adaptation_guide.md)
- 多链路 / Mesh 调优：[docs/multilink_mesh_tuning.md](docs/multilink_mesh_tuning.md)
- 第二阶段 OBJECT 确认默认关闭；启用前需双引擎等价验证与现场数据确认。

---

## 6. 常见问题

| 现象 | 排查 |
|---|---|
| 串口无数据 | 确认波特率 `921600`；关闭 `idf.py monitor`；检查 USB 线是否支持数据；macOS 使用 `/dev/cu.*` |
| 状态一直 `CALIBRATING` | 等待开机校准窗口；确认 BSSID 白名单正确；确认路由器固定 2.4GHz / HT20 |
| `ROUTER_OFFLINE` | 检查串口是否松动、ESP32 是否掉电、路由器是否重启；服务会自动重连 |
| 误报多 | 检查摆放位置、金属/水体/微波炉干扰；使用现场录制重新标定；不要直接套用他人阈值 |
| OpenWrt 安装后无法启动 | 确认 ARCH 与二进制匹配；`file /usr/bin/safehalo-daemon`；查看 `logread` |
| HTTPS 通知失败 | 确认路由器存在可用 `curl` / `wget` / `uclient-fetch` 与 CA 证书 |
| 端口被占用 | Python 默认 `8080`，Rust 默认 `8091`；修改监听端口或停止冲突进程 |

---

## 7. 安全与数据约束

- 首次部署后必须修改默认管理员密码。
- 不要把控制台端口直接暴露到公网。
- `logs/` 中的真实家庭数据只用于本地分析，不得修改、删除或未经脱敏提交。
- 真实录制数据提交前必须脱敏，确认不含可识别家庭环境信息。
- 日志推得的统计值只能标注“仅供参考”，不得直接写入默认阈值。
- 测试与验证必须使用临时数据目录，不得覆盖仓库 `data/users.json`。
- 任何检测算法改动必须同步 Python 生产引擎与 Rust daemon，并全量重跑等价验证。

---

## 8. Agent 一键安装

将 [docs/agent_install_prompt.md](docs/agent_install_prompt.md) 中的提示词复制给 coding agent，并填写目标环境、路由器地址、串口、BSSID、房间与通知配置。提示词会引导 agent 完成环境探测、构建、部署、配置、验证与交付报告，同时遵守上述安全约束。

---

## 目录结构

```text
SafeHalo/
├── README.md               # 项目概览、部署流程与快速上手
├── docs/                   # 阶段规划、架构设计、部署与集成文档
├── experiments/            # 实验方案、记录表、Rust daemon 与验证套件
├── scripts/                # 采集、打标、评估与系统服务安装脚本
├── src/safehalo/           # Python 检测引擎、融合、通知与 Web 控制台
├── firmware/               # ESP32-S3 探针固件配置与串口协议
├── tests/                  # Python 单元测试
└── data/                   # 本地用户数据库（运行时生成）
```

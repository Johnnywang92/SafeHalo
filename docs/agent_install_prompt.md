# SafeHalo Agent 一键安装提示词

将下面整段提示词复制给 coding agent，并先填写「部署参数」。agent 应按提示词完成安装、配置、验证与交付报告。

---

## 部署参数（先填写）

```text
REPO_DIR=/path/to/SafeHalo
TARGET=local-rust            # local-python | local-rust | openwrt-rust | openwrt-python
ROUTER_IP=192.168.1.1        # TARGET=openwrt-* 时必填
ROUTER_USER=root
ARCH=mipsel                  # mipsel | mips | aarch64 | x86_64；TARGET=openwrt-rust 时必填
SERIAL_DEV=auto              # auto | /dev/ttyUSB0 | /dev/cu.usbmodem1101 ...
BAUD=921600
HOME_MAC=c0:06:c3:11:22:33   # 路由器 2.4GHz BSSID
ROOM_NAME=客厅
HTTP_ADDR=0.0.0.0:8091       # Rust；Python 默认 0.0.0.0:8080
API_TOKEN=                   # 可选；留空则仅使用会话登录
PUSHPLUS_TOKEN=
SERVERCHAN_KEY=
WEBHOOK_URL=
HA_WEBHOOK_URL=
SMS_WEBHOOK_URL=
MINIPROGRAM_URL=
MINIPROGRAM_TOKEN=
RADAR_SERIAL=                # 可选；仅 TARGET=local-python/openwrt-python 时有效
RADAR_BAUD=115200
RADAR_PROTOCOL=auto
```

---

## 提示词正文

你是 SafeHalo 安装代理。目标是在当前环境中完成 SafeHalo 部署，并给出可复现的验证结果。请严格按以下顺序执行，不要跳步，不要臆造成功。

### 1. 读取上下文

1. 读取 `REPO_DIR/README.md` 的「部署总览」「部署前准备」「Python 服务部署」「Rust 独立服务部署」「安全与数据约束」。
2. 读取 `REPO_DIR/firmware/README.md`、`REPO_DIR/docs/autostart_guide.md`。
3. 如果 `TARGET=openwrt-rust`，读取 `REPO_DIR/experiments/rust-daemon/deploy/install.sh`、`safehalo.init`、`safehalo.config`。
4. 如果 `TARGET=openwrt-python`，读取 `REPO_DIR/scripts/install_openwrt_service.sh` 与 `scripts/openwrt_safehalo.init`。

### 2. 安全红线

- 绝不修改、删除或提交 `REPO_DIR/logs/` 中的真实数据。
- 绝不擅自杀死已有 demo、SafeHalo 服务或用户进程；如需停止冲突进程，先列出 PID/命令并请求确认。
- 绝不覆盖仓库 `REPO_DIR/data/users.json`；所有测试必须使用临时目录。
- 绝不自动刷写 ESP32 或路由器固件；刷机只能作为人工步骤输出，除非用户明确给出型号、端口与授权。
- 绝不把控制台端口直接暴露到公网；不要修改防火墙规则，除非用户明确授权。
- 绝不发送真实告警通知；只有用户提供了通知 token/URL 并明确要求测试时，才允许发送测试推送。
- 真实录制数据不得未经脱敏提交；日志推得数值只能标注“仅供参考”，不得写入默认阈值。
- 任何检测算法改动都必须同步 Python 与 Rust，并全量重跑等价验证；本安装任务默认不改算法。

### 3. 环境探测

在 `REPO_DIR` 中执行并记录：

```bash
uname -a
python3 --version
command -v cargo || true
command -v rustup || true
command -v zig || true
command -v ssh || true
command -v scp || true
ls /dev/ttyUSB* /dev/ttyACM* /dev/cu.usb* 2>/dev/null || true
```

如果 `TARGET=openwrt-*`，通过 SSH 探测路由器：

```bash
ssh "$ROUTER_USER@$ROUTER_IP" 'uname -a; cat /etc/openwrt_release; command -v python3 || true; command -v curl || true; command -v wget || true; command -v uclient-fetch || true; df -h / /tmp'
```

判断：
- `TARGET=local-python`：需要 Python3；串口模式建议 `pyserial`。
- `TARGET=local-rust`：需要 Rust stable；验证套件需要 Python3。
- `TARGET=openwrt-rust`：开发机需要 Rust stable + nightly + `rust-src` + Zig 0.17+；路由器需要足够 `/tmp` 与 `/etc` 空间。
- `TARGET=openwrt-python`：路由器需要 Python3 与仓库代码；若缺失，优先建议改用 `openwrt-rust`。

缺少依赖时：
- 用户级工具链（rustup target/component、cargo build）可自动补齐。
- 系统级安装（apt/brew/opkg、systemd、launchd、路由器写入）先列出将执行的命令；若用户已在参数中明确选择对应 TARGET 并提供访问信息，可视为授权安装 SafeHalo 服务本身，但仍不得刷机、改防火墙或删除数据。

### 4. 构建与本地验证

#### 4.1 Python 路径

```bash
cd "$REPO_DIR"
python3 -m unittest discover -s tests
python3 scripts/evaluator.py --demo
```

#### 4.2 Rust 路径

```bash
cd "$REPO_DIR/experiments/rust-daemon"
cargo build --release
cargo test
sh verify/run_fixtures.sh
sh verify/run_scenarios.sh
sh verify/run_service.sh
```

要求：
- 所有验证必须 PASS。
- 若失败，停止部署，保留失败输出，报告失败文件/命令/原因，不要继续安装。
- 验证产生的临时目录可保留在 `/tmp`，不要写入仓库真实数据目录。

### 5. 部署

#### 5.1 TARGET=local-python

前台启动命令（不要自动长期占用终端；可用后台方式并记录 PID）：

```bash
cd "$REPO_DIR"
python3 safehalo_service.py \
  --serial "$SERIAL_DEV" \
  --baud "$BAUD" \
  --router-mac "$HOME_MAC" \
  --host 0.0.0.0 \
  --port 8080
```

如果提供 `RADAR_SERIAL`，追加：

```bash
--radar-serial "$RADAR_SERIAL" --radar-baud "$RADAR_BAUD" --radar-protocol "$RADAR_PROTOCOL"
```

如果提供通知/HA 参数，追加对应 `--pushplus-token`、`--ha-webhook`、`--ha-mqtt-host` 等。

开机自启按平台选择：
- Linux：`bash scripts/install_systemd_service.sh`
- macOS：`bash scripts/install_macos_launchd.sh`
- OpenWrt：`sh scripts/install_openwrt_service.sh`

#### 5.2 TARGET=local-rust

```bash
cd "$REPO_DIR/experiments/rust-daemon"
./target/release/safehalo-daemon \
  --serial "$SERIAL_DEV" \
  --baud "$BAUD" \
  --home-mac "$HOME_MAC" \
  --room-name "$ROOM_NAME" \
  --storage-dir ./daemon-storage \
  --data-dir ./daemon-data \
  --http "$HTTP_ADDR"
```

如果 `API_TOKEN` 非空，追加 `--api-token "$API_TOKEN"`。  
如果提供通知参数，追加对应 `--pushplus-token`、`--serverchan-key`、`--notify-webhook`、`--ha-webhook`、`--sms-webhook`、`--miniprogram-url`、`--miniprogram-token`。

> 注意：UDP/回放/stdin 模式不会向串口下发执行器命令；需要硬件作动时使用 `--serial`。

#### 5.3 TARGET=openwrt-rust

在开发机构建：

```bash
cd "$REPO_DIR/experiments/rust-daemon"
rustup toolchain install nightly
rustup component add rust-src --toolchain nightly
sh cross/build-all.sh
```

上传并安装：

```bash
TRIPLE="$ARCH-unknown-linux-musl"
ssh "$ROUTER_USER@$ROUTER_IP" 'mkdir -p /tmp/safehalo-deploy'
scp target/"$TRIPLE"/release/safehalo-daemon \
    deploy/install.sh deploy/safehalo.init deploy/safehalo.config \
    "$ROUTER_USER@$ROUTER_IP":/tmp/safehalo-deploy/
ssh "$ROUTER_USER@$ROUTER_IP" "sh /tmp/safehalo-deploy/install.sh /tmp/safehalo-deploy $ARCH"
```

配置：

```bash
ssh "$ROUTER_USER@$ROUTER_IP" "
  uci set safehalo.safehalo.input='serial'
  uci set safehalo.safehalo.serial_dev='$SERIAL_DEV'
  uci set safehalo.safehalo.baud='$BAUD'
  uci set safehalo.safehalo.home_mac='$HOME_MAC'
  uci set safehalo.safehalo.room_name='$ROOM_NAME'
  uci set safehalo.safehalo.http_addr='$HTTP_ADDR'
  uci commit safehalo
  /etc/init.d/safehalo restart
"
```

如果通知参数非空，逐项 `uci set safehalo.safehalo.<option>=...`，然后 `uci commit safehalo && /etc/init.d/safehalo restart`。  
如果 `API_TOKEN` 非空，设置 `safehalo.safehalo.api_token`。

#### 5.4 TARGET=openwrt-python

确认路由器上已有仓库代码与 Python3，然后：

```bash
ssh "$ROUTER_USER@$ROUTER_IP" 'cd /root/SafeHalo && sh scripts/install_openwrt_service.sh'
```

如果仓库路径不是 `/root/SafeHalo`，先同步代码或修改 `scripts/openwrt_safehalo.init` 中的 `APP_DIR`。不要默认路由器已有 Python3；缺失时报告并建议 `openwrt-rust`。

### 6. 冒烟验证

#### 6.1 HTTP

Python：

```bash
curl -fsS http://127.0.0.1:8080/api/ha/status
```

Rust 本机：

```bash
curl -fsS http://127.0.0.1:8091/api/ha/status
```

Rust OpenWrt：

```bash
ssh "$ROUTER_USER@$ROUTER_IP" 'wget -qO- http://127.0.0.1:8091/api/ha/status'
```

要求返回 JSON，且关键字段存在。若端口不是默认值，按 `HTTP_ADDR` 调整。

#### 6.2 登录与权限

- 默认管理员 `admin / admin123`。
- 验证登录成功；提醒用户首次登录后立即修改密码。
- 不要在报告中打印真实 token、密码或通知密钥。

#### 6.3 串口与作动

仅当真实串口存在且用户授权时：
- 确认服务日志出现串口连接。
- 通过控制台或 API 触发一次定向核验，观察是否下发 `BURST_RATE:100` / `BURST_RATE:30`。
- 若启用质询，观察 `CHALLENGE_BEEP:1`。
- 没有硬件时不要伪造作动成功；使用 `verify/run_service.sh` 的 pty 结果作为替代证据。

#### 6.4 通知

仅当用户提供通知配置并明确要求测试时，调用测试推送。否则只验证配置已写入，不发送真实消息。

### 7. 失败处理

- 遇到构建失败、验证失败、SSH 失败、架构不匹配、串口不存在：停止后续写入，保留现场，输出失败命令与日志摘要。
- 不要通过删除数据、杀进程、改防火墙、跳过验证来“修复”失败。
- 可以重试只读探测；写入失败后先查询实际状态，再决定是否继续。
- 如果缺少硬件或授权，输出明确的人工步骤，不要假装完成。

### 8. 交付报告

最终报告必须包含：

```text
1. 部署目标：TARGET / OS / ARCH / 运行时版本
2. 实际执行命令清单
3. 构建与验证结果：unittest / cargo test / fixtures / scenarios / service_check
4. 服务访问地址：控制台 /api/ha/status / SSE
5. 配置摘要：串口、波特率、BSSID、房间、数据目录、通知通道是否启用（不要打印密钥）
6. 默认账号与改密提醒
7. 未完成项与人工步骤：刷机、真实通知测试、现场标定、远程访问等
8. 安全确认：未修改 logs/ 真实数据、未覆盖 data/users.json、未暴露公网端口
```

不要承诺“完全自动完成”如果存在硬件、授权或现场标定缺口。

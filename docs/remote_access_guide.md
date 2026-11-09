# SafeHalo 远程公网访问与安全管理指南

SafeHalo Web 控制台默认监听 `0.0.0.0:8080`。当您在外上班、出差或使用手机 4G/5G 网络时，可以通过内网穿透或虚拟专网随时随地查看家中的长辈安全状态。

为防止公网扫描与未授权访问，SafeHalo 内置了基于 PBKDF2-HMAC-SHA256 的加盐身份鉴权与细粒度角色权限系统。

---

## 一、方案对比与推荐

| 方案 | 成本 | 复杂度 | 安全性 | 是否需公网 IP | 适用场景 |
|---|---|---|---|---|---|
| **方案 A：Cloudflare Tunnel（首推）** | **免费** | **极低**（1 条命令） | **高**（自动 HTTPS 绿锁） | **不需要** | **所有普通家庭、手机直接点击使用** |
| **方案 B：Tailscale 虚拟专网** | **免费** | **低**（两端装 App） | **极高**（WireGuard加密） | **不需要** | 极度注重隐私、不想暴露任何公网端口 |
| **方案 C：自建 FRP 穿透** | 需要 VPS | 中等 | 高（需自行配证书） | 需要一台 VPS | 已有云服务器的开发者 |
| **方案 D：IPv6 直连 + DDNS** | 免费 | 中等 | 中等（需开防火墙端口） | 需宽带有 IPv6 | 宽带有动态 IPv6 且不想走第三方服务 |

---

## 二、方案 A：Cloudflare Tunnel（最推荐，零门槛）

Cloudflare 官方提供的免费反向代理穿透，自带全球 CDN 加速和免费的 SSL 证书（HTTPS），**手机打开完全免证书告警**。

### 1. 安装 Cloudflared 客户端

- **macOS（Homebrew）**：
  ```bash
  brew install cloudflared
  ```
- **OpenWrt 路由器**：
  ```bash
  opkg update
  opkg install cloudflared
  ```
- **Linux / 树莓派**：
  ```bash
  curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
  sudo dpkg -i cloudflared.deb
  ```

### 2. 一键启动公网访问

确保本地已启动 SafeHalo 控制台（`python3 web_server.py --port 8080`），然后在终端执行：

```bash
cloudflared tunnel --url http://localhost:8080
```

终端将立即输出如下公网链接：
```text
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
|  https://safehalo-care-family.trycloudflare.com                                            |
+--------------------------------------------------------------------------------------------+
```

将该 `https://` 链接发送给家庭成员，子女在公司手机上直接点击即可安全登录。

---

## 三、方案 B：Tailscale 虚拟私有专网（最高隐私）

Tailscale 基于底层 WireGuard 协议，让手机与家庭路由器形成加密点对点局域网。

1. **注册与安装**：在 [Tailscale 官网](https://tailscale.com) 注册免费账号。
2. **手机端**：在 iPhone (App Store) 或 Android 安装 Tailscale，登录您的账号。
3. **路由器/电脑端**：
   ```bash
   # OpenWrt 安装
   opkg update && opkg install tailscale
   /etc/init.d/tailscale start
   tailscale up
   ```
4. **远程访问**：在手机浏览器输入路由器的 Tailscale IP（如 `http://100.86.12.34:8080`），即可直接访问，免受任何公网恶意扫描。

---

## 四、方案 C：自建 FRP 穿透（适用于有 VPS 用户）

在家庭路由器配置 `frpc.ini`：
```ini
[common]
server_addr = <你的云服务器公网IP>
server_port = 7000
token = <你的认证密钥>

[safehalo_web]
type = tcp
local_ip = 127.0.0.1
local_port = 8080
remote_port = 8088
```
启动客户端：`frpc -c frpc.ini`，即可通过 `http://<云服务器IP>:8088` 访问。

---

## 五、网页端用户管理与角色权限（RBAC）

系统默认包含两种角色：

1. **超级管理员（`admin`）**：
   - 默认账号：`admin`，初始密码：`admin123`（建议首次登录后立即修改）。
   - 权限：添加/删除家庭成员账号、修改安防模式、清空系统日志、配置微信推送 Token。
2. **家庭普通成员（`family`）**：
   - 权限：查看实时生理遥测（走动/静止/心率呼吸微动）、查看历史事件与跌倒报警记录、手动触发定向关心排查。
   - 限制：不可修改系统阈值、不可删除其他成员或清空审计日志。

### 常用操作：
1. **添加家庭成员**：管理员登录后进入顶部 **「👤 成员与用户管理」** 标签，点击「➕ 添加新家庭成员」，输入子女姓名与账号。
2. **修改密码**：在成员管理页面下方输入原密码与新密码（至少 6 位），点击确认即时生效。

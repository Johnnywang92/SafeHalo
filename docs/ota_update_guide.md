# SafeHalo 远程 OTA 与守护进程无感原子热升级指南

## 1. 设计目标与背景

SafeHalo 守护进程与推理引擎主要运行在边缘嵌入式设备上（如家庭 Wi-Fi 路由器、MT7621/MT7981、RK3566、树莓派等）。
在弱算力、受限闪存以及 Linux / OpenWrt 系统的生产环境中，远程升级通常面临以下严苛工程挑战：

1. **Linux `ETXTBSY: Text file busy` 写入冲突**：直接向正在运行的守护进程二进制文件写入数据会被 Linux 内核直接拒绝。
2. **CPU 架构与 ELF 端序错配风险**：例如 MIPS 架构存在 Big-Endian (`mips`) 与 Little-Endian (`mipsel`) 差异。一旦将错端序的二进制烧录进 OpenWrt，`procd` 会陷入高速崩溃死循环导致路由器失联。
3. **网络抖动与闪存损坏风险**：升级过程中若因网络中断导致二进制截断，残缺文件直接执行会导致系统瘫痪。
4. **启动崩溃缺少保底机制**：新版本若存在配置不兼容或动态链接缺失，服务重启失败后无人可现场恢复。

`scripts/ota_updater.py` 专为解决上述痛点设计，具备纯标准库零外部依赖、架构强校验、POSIX 原子热替换与看门狗自动回滚能力。

---

## 2. 核心架构与可靠性机制

```
  [云端 / 运维推送新二进制]
            │
            ▼
┌───────────────────────────────────────┐
│ 1. 完整性与架构校验 (Hash & ELF Header) │
│    - SHA-256 密码学防篡改与完整性校验   │
│    - 提取 ELF Header: 严密区分 mipsel /│
│      mips / aarch64 / x86_64          │
│    - 拒绝不匹配架构，杜绝 procd 死循环   │
└──────────────────┬────────────────────┘
                   │ 验证通过
                   ▼
┌───────────────────────────────────────┐
│ 2. POSIX 原子热替换 (Atomic Replacer)  │
│    - 目标同目录暂存 (.safehalo_new.pid)│
│    - 备份原版本 -> safehalo-daemon.old │
│    - os.replace 原子 rename 解绑 inode │
│    - 规避 ETXTBSY，在跑进程平滑退出     │
└──────────────────┬────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────┐
│ 3. 跨平台服务平滑重启 (ServiceManager) │
│    - OpenWrt: /etc/init.d/safehalo    │
│    - Linux: systemctl restart safehalo│
└──────────────────┬────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────┐
│ 4. 看门狗存活轮询 (Watchdog Health)    │
│    - 连续 12s 轮询 /api/health 状态    │
│    - 状态正常 ("status": "healthy"):   │
│      ==> 升级圆满成功完成                │
│    - 超时未启动 / 进程崩溃 / HTTP 500:  │
│      ==> 自动触发原子回滚至 .old 备份!  │
└───────────────────────────────────────┘
```

---

## 3. CLI 工具使用命令清单

### 3.1 探测当前宿主系统架构与字节序
```bash
python3 scripts/ota_updater.py --check-arch
```
*示例输出：*
```text
[ArchitectureDetector] 当前宿主架构: mipsel
```

### 3.2 深度检查待升级二进制文件
检查目标二进制的真实 ELF 编译架构、文件字节数与 SHA-256：
```bash
python3 scripts/ota_updater.py --inspect /tmp/safehalo-daemon
```
*示例输出：*
```text
[Inspect] 文件: /tmp/safehalo-daemon
  ├─ 识别架构: mipsel
  ├─ 文件大小: 2154320 字节 (2103.8 KB)
  └─ SHA-256:  a1b2c3d4e5f6...
```

### 3.3 执行安全无感原子热升级（带看门狗守护）
```bash
python3 scripts/ota_updater.py \
    --apply /tmp/safehalo-daemon \
    --sha256 <SHA256_HASH> \
    --target-path /usr/bin/safehalo-daemon \
    --backup-path /usr/bin/safehalo-daemon.old \
    --health-url http://127.0.0.1:8091/api/health
```

### 3.4 手动紧急一键回滚
当需要降级或人工回滚时：
```bash
python3 scripts/ota_updater.py --rollback \
    --target-path /usr/bin/safehalo-daemon \
    --backup-path /usr/bin/safehalo-daemon.old
```

### 3.5 自动化发布流水线：生成版本 manifest.json
```bash
python3 scripts/ota_updater.py \
    --generate-manifest dist/binaries/ \
    --firmware-dir dist/firmware/ \
    --version 1.0.0 > manifest.json
```

### 3.6 ESP32-S3 探针固件 OTA 指令生成
校验 ESP32 固件魔数（`0xE9`）并生成串口/MQTT 下行指令：
```bash
python3 scripts/ota_updater.py --probe-ota dist/firmware/safehalo_esp32_s3_v1.bin
```
*示例输出：*
```text
[ProbeOTA] 探针固件预检通过:
  ├─ 文件大小: 487296 字节
  ├─ SHA-256:  8f92b...
  └─ 下行作动指令: OTA_BEGIN:487296:8f92b...
```

---

## 4. 故障自动恢复与自愈验证

在升级过程中，若发生以下情况，系统将无感自愈：

| 异常场景 | 防护机制 | 最终结果 |
| :--- | :--- | :--- |
| 下载网络闪断导致文件不完整 | 文件大小下限 (<100KB) + SHA-256 强校验 | 拒绝写入，原服务不受任何影响 |
| 架构误下发 (如将 x86_64 下发到 mipsel 路由器) | ELF Magic & `e_machine` 识别拦截 | 拒绝写入，避免 procd 死循环崩溃 |
| 新版存在 bug 启动后崩溃退出 | 看门狗 12s 内未探测到 `/api/health` 正常返回 | 立即自动还原 `.old` 二进制并拉起服务 |
| 人工调试需要快速切换版本 | 一键 `--rollback` 指令 | 毫秒级原子切换回上一版本 |

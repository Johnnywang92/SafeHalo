# SafeHalo 纯路由器方案 (csi_recv_router) 落地实操指南

## 1. 纯路由器方案原理与硬件架构

- **硬件需求**: 仅需 **1 块 ESP32-S3** + **1 台现有 2.4GHz 旧路由器**。
- **工作机制**:
  1. ESP32-S3 作为 Wi-Fi Station (STA) 连接到旧路由器的 Wi-Fi 热点。
  2. ESP32 内部启动定时 Ping 任务，以固定频率 (建议 30Hz ~ 80Hz) 持续向路由器网关 IP 发送 Ping 报文。
  3. 路由器在收到 Ping 后自动回包（ICMP Echo Reply / 802.11 ACK）。
  4. ESP32-S3 底层 Wi-Fi MAC 硬件在接收到路由器的回包时，即时解调并输出该报文的信道状态信息 (CSI)。
  5. 计算机/上位机通过串口读取 CSI 数据，运行本算法进行去噪、特征提取与四态识别。

```
                  2.4GHz 无线多径信号 (CSI)
 [旧路由器 (AP)] <~~~~~~~~~~~~~~~~~~~~~~~~~~> [ESP32-S3 接收端]
 (网关 192.168.x.1)       Ping / 回包              │ USB 串口 (波特率 921600)
                                                    ▼
                                           [SafeHalo 检测算法]
                                       (EMPTY / STATIC / MOVING / FALL)
```

---

## 2. 旧路由器配置建议（关键避坑）

1. **频段与信道固定**:
   - 必须使用 **2.4GHz** 频段（ESP32-S3 仅支持 2.4G）。
   - 在路由器后台将 Wi-Fi 信道从「自动切换」改为**固定信道**（推荐 1、6 或 11 中周围邻居干扰最少的一个）。若信道自动跳变会导致 CSI 幅值突变。
   - 频宽设置为 **20MHz** (HT20)，不要开 40MHz。
2. **专属测试组网**:
   - 建议专门作为测试网络，不要把手机、平板、电视等高吞吐设备挂在此网络下，以防带宽竞争造成 Ping 丢包。
3. **获取路由器 MAC 地址 (BSSID)**:
   - 登录路由器管理后台（或查看路由器底部贴纸），记录 2.4G 频段的无线 MAC 地址（例如 `c0:06:c3:11:22:33`）。
   - 后续算法会根据此 MAC 地址进行严格白名单过滤，彻底排除空中其他设备的干扰。

---

## 3. ESP32-S3 固件编译与烧录

克隆 `esp-csi` 仓库后，进入路由器接收端示例：

```bash
cd esp-csi/examples/get-started/csi_recv_router
idf.py set-target esp32s3
idf.py menuconfig
```

在交互式配置菜单中设置：
- `Example Connection Configuration`:
  - `WiFi SSID`: 你的旧路由器热点名称
  - `WiFi Password`: 路由器密码
- `Example Configuration`:
  - `Ping target IP`: 路由器网关 IP (如 `192.168.1.1` 或 `192.168.0.1`)
  - `Ping interval (ms)`: 设置为 `20` 或 `30` (对应 30Hz ~ 50Hz 采样率)

配置保存后编译并烧录（macOS 示例）：
```bash
idf.py build
idf.py -b 921600 -p /dev/cu.usbmodem1101 flash
```
> **注意**: 烧录完成后请**关闭** `idf.py monitor`，避免占用串口。

---

## 4. 运行上位机检测算法

回到 SafeHalo 根目录：

### 模式 A：无硬件快速验证算法 (仿真模式)
```bash
python3 router_detector.py --demo
```
该模式会生成高拟真室内相干多径 CSI 信号，直观演示：基线自适应校准 -> 空房 -> 静止呼吸微动 -> 走动活动 -> 跌倒冲击 -> 倒地超时报警。

### 模式 B：真实硬件串口实时检测
```bash
# 自动监听串口并检测
python3 router_detector.py --port /dev/cu.usbmodem1101 --baud 921600

# 推荐：加入路由器 MAC 白名单过滤（抗干扰）
python3 router_detector.py --port /dev/cu.usbmodem1101 --baud 921600 --router-mac c0:06:c3:xx:xx:xx
```

### 模式 C：离线日志重放测试
```bash
python3 router_detector.py --file experiments/my_router_csi.csv --router-mac c0:06:c3:xx:xx:xx
```

---

## 5. 核心算法设计与路由器专属优化

针对纯路由器方案的固有缺陷，算法专门做了以下处理：

1. **路由器 AGC / 发包功率跳变抑制 (L2 归一化)**:
   - 路由器动态调整 MCS 速率和发射功率时，会导致原始幅值全局上下跳动，造成传统算法误判。
   - 本算法通过全子载波能量 L2 范数归一化，彻底消除全局功率波动。
2. **多载波移动方差 (MVS) & 帧间一阶差分能量**:
   - 灵敏感知人体各躯干反射径的多普勒频移，稳定分离「无人 vs 静止呼吸 vs 走动」。
3. **跌倒完整时序状态机**:
   - 绝非单帧瞬态分类，而是严格匹配物理过程：`[冲击突变尖峰 (Impact Spike)] -> [骤然跌入低能区 (Sudden Quiescence)] -> [持续倒地不起超时 (Stillness Timeout)]`。
   - 若短时间内起身走动，系统自动平滑解除疑似告警，有效规避弯腰系鞋带、快速坐下等日常动作的误报。
4. **路由器通信健康度监控**:
   - 实时计算路由器发包抖动 (Jitter) 与实际采样率，当网络拥塞或断联时自动提示，不触发虚假跌倒。

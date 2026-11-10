# SafeHalo ESP32-S3 边缘探针固件配置与通信协议 (`firmware/`)

本目录存放 **ESP32-S3 CSI 接收探针 (`csi_recv_router` / `csi_recv`)** 的定制编译配置与双向串口指令规范。

---

## 1. 固件编译配置 (`sdkconfig.defaults`)

在编译乐鑫官方 `esp-csi/examples/get-started/csi_recv_router` 时，可将本目录下的 [`sdkconfig.defaults`](file:///Users/yifanwang/workspace/SafeHalo/firmware/sdkconfig.defaults) 复制到固件工程根目录，确保开启 Wi-Fi CSI、固定 HT20 频宽并配置 921600 高速串口输出：

```bash
cp firmware/sdkconfig.defaults /path/to/esp-csi/examples/get-started/csi_recv_router/
cd /path/to/esp-csi/examples/get-started/csi_recv_router/
idf.py set-target esp32s3
idf.py build flash -b 921600
```

---

## 2. 上下行串口通信协议

### 2.1 上行数据流（ESP32-S3 ➔ SafeHalo 主机）
波特率 `921600`，每收到一帧路由器回包输出一行标准 CSV：
```text
CSI_DATA,<type>,<seq>,<mac>,<rssi>,<rate>,<sig_mode>,<mcs>,<bw>,...,<noise_floor>,<ampdu>,<channel>,<sec_ch>,<timestamp>,...,<len>,0,"[imag0, real0, imag1, real1, ...]"
```

### 2.2 下行控制指令（SafeHalo 主机 ➔ ESP32-S3）
当 [`TargetedFallInspector`](file:///Users/yifanwang/workspace/SafeHalo/src/safehalo/targeted_inspector.py) 初筛检测到疑似跌倒冲击（或用户在 Web 控制台 / Home Assistant 点击【启动定向微动核验】）时，[`safehalo_service.py`](file:///Users/yifanwang/workspace/SafeHalo/safehalo_service.py) 会通过串口向探针发送动态提频指令：

| 下行指令格式 | 触发场景 | 探针动作 |
| :--- | :--- | :--- |
| `BURST_RATE:100\n` | 进入 `FALL_SUSPECT` 疑似跌倒定向复查窗口 | 将 ICMP Ping 触发频率从常规 30Hz (33ms) 提频至 **100Hz (10ms)**，聚焦地面胸腔呼吸微多普勒 |
| `BURST_RATE:30\n` | 定向复查结束（排除误报或确认恢复） | 恢复常规 **30Hz (33ms)** 低功耗巡检发包率 |

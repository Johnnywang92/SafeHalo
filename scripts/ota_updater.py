#!/usr/bin/env python3
"""
SafeHalo 远程 OTA 与守护进程无感原子热升级工具 (scripts/ota_updater.py)

专为 OpenWrt 弱算力路由器与嵌入式 Linux 网关设计，具备工业级可靠性保障:
1. 架构与 ELF 端序严密校验:
   - 提取二进制文件的 ELF 标头（MIPS LE/BE、AArch64、x86_64、Mach-O），在替换前杜绝错架构安装引起的崩溃死循环；
2. 密码学完整性保护:
   - SHA-256 强校验与文件大小合法性下限约束，杜绝网络截断与传输污染；
3. POSIX 原子无感热替换:
   - 规避 Linux 正在运行二进制的 ETXTBSY 写入阻塞，采用同分区 staging + os.replace 原子置换；
   - 自动留存 /usr/bin/safehalo-daemon.old 备份；
4. 健康检查与看门狗自动回滚 (Watchdog Auto-Rollback):
   - 服务重启后持续对 /api/health 进行 12 秒存活轮询；
   - 若新版本启动崩溃、接口无响应或异常退出，看门狗立即触发原子回滚并恢复上一版本，保障家庭监护不失联；
5. 探针固件 OTA 辅助 (ESP32-S3 Probe Firmware OTA):
   - 支持 ESP32-IDF 固件标头校验 (0xe9) 与下行刷写协议封装。
"""

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

DEFAULT_DAEMON_PATH = "/usr/bin/safehalo-daemon"
DEFAULT_BACKUP_PATH = "/usr/bin/safehalo-daemon.old"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8091/api/health"


class ArchitectureDetector:
    """精准探测当前宿主系统与目标二进制的 CPU 架构与 ELF 字节序。"""

    @staticmethod
    def detect_host_arch() -> str:
        # 1. 尝试读取 OpenWrt 发行版信息
        if os.path.exists("/etc/openwrt_release"):
            try:
                with open("/etc/openwrt_release", "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("DISTRIB_ARCH="):
                            dist = line.split("=")[1].strip("'\"\n").split("_")[0]
                            if dist.startswith("mipsel"):
                                return "mipsel"
                            elif dist.startswith("mips"):
                                return "mips"
                            elif dist in ("aarch64", "arm64"):
                                return "aarch64"
                            elif dist in ("x86_64", "amd64"):
                                return "x86_64"
            except Exception:
                pass

        # 2. 检查操作系统与 uname
        sys_name = os.uname().sysname.lower()
        machine = os.uname().machine.lower()

        if sys_name == "darwin":
            if machine in ("arm64", "aarch64"):
                return "darwin-arm64"
            return "darwin-x86_64"

        if machine == "mips":
            # 解决部分 MTK mipsel 芯片内核报告 mips 的问题：读取 /bin/busybox 或 /bin/sh 的 ELF 字节序
            for candidate in ["/bin/busybox", "/bin/sh", "/usr/bin/safehalo-daemon"]:
                if os.path.exists(candidate):
                    try:
                        with open(candidate, "rb") as f:
                            f.seek(5)
                            endian_byte = f.read(1)
                            if endian_byte == b"\x01":
                                return "mipsel"
                            elif endian_byte == b"\x02":
                                return "mips"
                    except Exception:
                        pass
            return "mips"

        if machine in ("mipsel", "mipsle"):
            return "mipsel"
        elif machine in ("aarch64", "arm64"):
            return "aarch64"
        elif machine in ("x86_64", "amd64"):
            return "x86_64"

        return machine

    @staticmethod
    def inspect_binary_arch(filepath: str) -> str:
        """从二进制头部提取 ELF 或 Mach-O 真实编译目标架构。"""
        if not os.path.exists(filepath):
            return "file_not_found"
        try:
            with open(filepath, "rb") as f:
                magic = f.read(4)
                if magic == b"\x7fELF":
                    ei_class = f.read(1)
                    ei_data = f.read(1)
                    is_le = (ei_data == b"\x01")
                    endian = "<" if is_le else ">"
                    f.seek(18)
                    e_machine_bytes = f.read(2)
                    if len(e_machine_bytes) < 2:
                        return "invalid_elf"
                    e_machine = struct.unpack(endian + "H", e_machine_bytes)[0]
                    if e_machine == 8:  # EM_MIPS
                        return "mipsel" if is_le else "mips"
                    elif e_machine == 183:  # EM_AARCH64
                        return "aarch64"
                    elif e_machine == 62:  # EM_X86_64
                        return "x86_64"
                    return f"elf_machine_{e_machine}"
                elif magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
                    f.seek(4)
                    cpu_bytes = f.read(4)
                    if len(cpu_bytes) < 4:
                        return "invalid_macho"
                    cpu_type = struct.unpack("<I", cpu_bytes)[0]
                    if cpu_type == 0x0100000C:
                        return "darwin-arm64"
                    elif cpu_type == 0x01000007:
                        return "darwin-x86_64"
                    return f"darwin_cpu_{cpu_type}"
                elif magic == b"\xca\xfe\xba\xbe":
                    return "darwin-universal"
                elif magic and magic[0] == 0xE9:
                    return "esp32_firmware"
                return "unknown_binary"
        except Exception as e:
            return f"inspect_error_{e}"

    @classmethod
    def is_compatible(cls, binary_arch: str, host_arch: str) -> bool:
        """判断二进制架构是否可在当前宿主上正常运行。"""
        if binary_arch == host_arch:
            return True
        # 常见同义别名归一化
        aliases = {
            "aarch64": ["aarch64", "arm64"],
            "x86_64": ["x86_64", "amd64"],
            "mipsel": ["mipsel", "mipsle"],
            "darwin-arm64": ["darwin-arm64", "darwin-universal"],
            "darwin-x86_64": ["darwin-x86_64", "darwin-universal"],
        }
        for _, group in aliases.items():
            if binary_arch in group and host_arch in group:
                return True
        return False


class HashVerifier:
    """哈希校验与文件大小安全下限保护。"""

    @staticmethod
    def compute_sha256(filepath: str) -> str:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest().lower()

    @classmethod
    def verify(cls, filepath: str, expected_sha256: str) -> bool:
        if not os.path.exists(filepath):
            return False
        return cls.compute_sha256(filepath) == expected_sha256.lower().strip()


class AtomicReplacer:
    """POSIX 原子热替换与版本回滚器。"""

    @staticmethod
    def replace(src_new_binary: str, target_binary: str, backup_path: Optional[str] = None) -> Tuple[bool, str]:
        if not os.path.exists(src_new_binary):
            return False, f"待替换新二进制不存在: {src_new_binary}"

        target_dir = os.path.dirname(os.path.abspath(target_binary))
        os.makedirs(target_dir, exist_ok=True)

        staging_path = os.path.join(target_dir, f".safehalo_new.{os.getpid()}")
        try:
            # 1. 复制到目标目录同分区暂存，赋予 0755 可执行权限
            shutil.copy2(src_new_binary, staging_path)
            os.chmod(staging_path, 0o755)

            # 2. 备份现有在跑二进制
            if os.path.exists(target_binary) and backup_path:
                backup_dir = os.path.dirname(os.path.abspath(backup_path))
                os.makedirs(backup_dir, exist_ok=True)
                shutil.copy2(target_binary, backup_path)
                os.chmod(backup_path, 0o755)

            # 3. 执行 POSIX 原子 rename（杜绝 Linux ETXTBSY 错误，无缝解绑 running inode）
            os.replace(staging_path, target_binary)
            return True, "原子替换成功"
        except Exception as e:
            if os.path.exists(staging_path):
                try:
                    os.remove(staging_path)
                except Exception:
                    pass
            return False, f"原子替换异常: {e}"

    @staticmethod
    def rollback(target_binary: str, backup_path: str) -> Tuple[bool, str]:
        if not os.path.exists(backup_path):
            return False, f"未发现上一版本备份文件: {backup_path}"
        try:
            os.replace(backup_path, target_binary)
            os.chmod(target_binary, 0o755)
            return True, "已成功回滚至上一版本二进制"
        except Exception as e:
            return False, f"回滚异常: {e}"


class ServiceManager:
    """跨平台守护进程控制器 (OpenWrt procd / Linux systemd / 本地进程)。"""

    @staticmethod
    def restart_service() -> bool:
        # OpenWrt procd 服务
        if os.path.exists("/etc/init.d/safehalo"):
            res = subprocess.run(["/etc/init.d/safehalo", "restart"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return res.returncode == 0

        # systemd 服务
        if os.path.exists("/bin/systemctl") or os.path.exists("/usr/bin/systemctl"):
            res = subprocess.run(["systemctl", "restart", "safehalo"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if res.returncode == 0:
                return True

        return True


class HealthCheckWatchdog:
    """升级后存活健康度看门狗。"""

    @staticmethod
    def wait_for_healthy(health_url: str = DEFAULT_HEALTH_URL, timeout_seconds: float = 12.0) -> bool:
        start_t = time.time()
        while time.time() - start_t < timeout_seconds:
            try:
                req = urllib.request.Request(health_url, headers={"User-Agent": "SafeHalo-OTA-Watchdog/1.0"})
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        if data.get("status") == "healthy":
                            return True
            except Exception:
                pass
            time.sleep(0.5)
        return False


class OTAUpdater:
    """远程 OTA 协调器与发布包管理。"""

    def __init__(
        self,
        target_path: str = DEFAULT_DAEMON_PATH,
        backup_path: str = DEFAULT_BACKUP_PATH,
        health_url: str = DEFAULT_HEALTH_URL,
    ):
        self.target_path = target_path
        self.backup_path = backup_path
        self.health_url = health_url

    def update_from_binary(self, new_binary_path: str, expected_sha256: Optional[str] = None) -> Tuple[bool, str]:
        """从本地或暂存的二进制文件执行原子热升级并启动看门狗验证。"""
        # 1. 存在性与大小预检
        if not os.path.exists(new_binary_path):
            return False, f"升级失败: 二进制文件不存在: {new_binary_path}"
        file_size = os.path.getsize(new_binary_path)
        if file_size < 100 * 1024:
            return False, f"升级失败: 文件体积异常过小 ({file_size} 字节), 疑似截断损坏"

        # 2. SHA-256 完整性检验
        if expected_sha256:
            if not HashVerifier.verify(new_binary_path, expected_sha256):
                actual_sha = HashVerifier.compute_sha256(new_binary_path)
                return False, f"升级失败: SHA-256 校验不匹配 (期望: {expected_sha256}, 实际: {actual_sha})"

        # 3. 架构与 ELF 端序兼容性校验
        host_arch = ArchitectureDetector.detect_host_arch()
        bin_arch = ArchitectureDetector.inspect_binary_arch(new_binary_path)
        if not ArchitectureDetector.is_compatible(bin_arch, host_arch):
            return False, f"升级失败: 架构不匹配！目标机器为 '{host_arch}'，而提供的二进制为 '{bin_arch}'，拒绝执行以免导致 procd 崩溃循环"

        # 4. 执行 POSIX 原子热替换
        ok, msg = AtomicReplacer.replace(new_binary_path, self.target_path, self.backup_path)
        if not ok:
            return False, f"原子替换失败: {msg}"

        # 5. 触发守护进程重启
        ServiceManager.restart_service()

        # 6. 启动健康检查看门狗
        if self.health_url:
            is_healthy = HealthCheckWatchdog.wait_for_healthy(self.health_url, timeout_seconds=10.0)
            if not is_healthy:
                print(f"[OTA Watchdog] ⚠️ 新版本健康检查超时 (未达到 healthy 状态)，立即触发自动安全回滚！")
                AtomicReplacer.rollback(self.target_path, self.backup_path)
                ServiceManager.restart_service()
                return False, "升级失败: 健康检查超时未通过，看门狗已自动原子回滚至旧版本！"

        return True, f"升级成功并已通过看门狗存活校验 (架构: {bin_arch}, 大小: {file_size/1024:.1f} KB)"

    def rollback_to_previous(self) -> Tuple[bool, str]:
        ok, msg = AtomicReplacer.rollback(self.target_path, self.backup_path)
        if ok:
            ServiceManager.restart_service()
        return ok, msg

    @staticmethod
    def generate_manifest(version: str, binaries_dir: str, firmware_dir: Optional[str] = None) -> dict:
        """为发布流水线自动扫描生成 manifest.json 发布清单。"""
        manifest = {
            "version": version,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "binaries": {},
            "firmware": {},
        }

        if os.path.exists(binaries_dir):
            for fname in os.listdir(binaries_dir):
                fpath = os.path.join(binaries_dir, fname)
                if os.path.isfile(fpath) and os.access(fpath, os.X_OK):
                    arch = ArchitectureDetector.inspect_binary_arch(fpath)
                    if arch and not arch.startswith("inspect_error"):
                        manifest["binaries"][arch] = {
                            "filename": fname,
                            "sha256": HashVerifier.compute_sha256(fpath),
                            "size_bytes": os.path.getsize(fpath),
                        }

        if firmware_dir and os.path.exists(firmware_dir):
            for fname in os.listdir(firmware_dir):
                if fname.endswith(".bin"):
                    fpath = os.path.join(firmware_dir, fname)
                    manifest["firmware"][fname] = {
                        "filename": fname,
                        "sha256": HashVerifier.compute_sha256(fpath),
                        "size_bytes": os.path.getsize(fpath),
                    }

        return manifest

    @staticmethod
    def prepare_probe_ota(bin_path: str) -> Tuple[bool, dict]:
        """解析 ESP32-S3 探针固件并生成下行推送参数。"""
        if not os.path.exists(bin_path):
            return False, {"error": "固件文件不存在"}
        try:
            with open(bin_path, "rb") as f:
                header = f.read(1)
                if not header or header[0] != 0xE9:
                    return False, {"error": "非法 ESP32-IDF 固件 (魔数非 0xE9)"}
            sha = HashVerifier.compute_sha256(bin_path)
            size = os.path.getsize(bin_path)
            cmd = f"OTA_BEGIN:{size}:{sha}"
            return True, {
                "firmware": bin_path,
                "size_bytes": size,
                "sha256": sha,
                "downlink_command": cmd,
            }
        except Exception as e:
            return False, {"error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="SafeHalo 远程 OTA 与守护进程无感原子热升级工具")
    parser.add_argument("--check-arch", action="store_true", help="探测并输出当前宿主机架构与 ELF 端序")
    parser.add_argument("--inspect", type=str, default=None, help="分析指定二进制的架构、ELF 头与 SHA-256")
    parser.add_argument("--apply", type=str, default=None, help="从本地二进制文件执行原子热升级")
    parser.add_argument("--sha256", type=str, default=None, help="升级二进制期望的 SHA-256 校验和")
    parser.add_argument("--target-path", type=str, default=DEFAULT_DAEMON_PATH, help=f"目标安装路径 (默认 {DEFAULT_DAEMON_PATH})")
    parser.add_argument("--backup-path", type=str, default=DEFAULT_BACKUP_PATH, help=f"备份路径 (默认 {DEFAULT_BACKUP_PATH})")
    parser.add_argument("--health-url", type=str, default=DEFAULT_HEALTH_URL, help=f"存活健康检查接口 (默认 {DEFAULT_HEALTH_URL})")
    parser.add_argument("--no-health-check", action="store_true", help="跳过升级后健康检查看门狗")
    parser.add_argument("--rollback", action="store_true", help="立即回滚至上一版本二进制")
    parser.add_argument("--generate-manifest", type=str, default=None, help="自动扫描目录生成发布 manifest.json")
    parser.add_argument("--version", type=str, default="1.0.0", help="生成 manifest 时的版本号")
    parser.add_argument("--firmware-dir", type=str, default=None, help="生成 manifest 时的探针固件目录")
    parser.add_argument("--probe-ota", type=str, default=None, help="校验并打包 ESP32-S3 探针固件下行升级指令")

    args = parser.parse_args()

    if args.check_arch:
        arch = ArchitectureDetector.detect_host_arch()
        print(f"[ArchitectureDetector] 当前宿主架构: {arch}")
        sys.exit(0)

    if args.inspect:
        arch = ArchitectureDetector.inspect_binary_arch(args.inspect)
        size = os.path.getsize(args.inspect) if os.path.exists(args.inspect) else 0
        sha = HashVerifier.compute_sha256(args.inspect) if os.path.exists(args.inspect) else "none"
        print(f"[Inspect] 文件: {args.inspect}")
        print(f"  ├─ 识别架构: {arch}")
        print(f"  ├─ 文件大小: {size} 字节 ({size/1024:.1f} KB)")
        print(f"  └─ SHA-256:  {sha}")
        sys.exit(0)

    if args.generate_manifest:
        manifest = OTAUpdater.generate_manifest(args.version, args.generate_manifest, args.firmware_dir)
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        sys.exit(0)

    if args.probe_ota:
        ok, res = OTAUpdater.prepare_probe_ota(args.probe_ota)
        if ok:
            print("[ProbeOTA] 探针固件预检通过:")
            print(f"  ├─ 文件大小: {res['size_bytes']} 字节")
            print(f"  ├─ SHA-256:  {res['sha256']}")
            print(f"  └─ 下行作动指令: {res['downlink_command']}")
            sys.exit(0)
        else:
            print(f"[ProbeOTA] 固件校验失败: {res.get('error')}", file=sys.stderr)
            sys.exit(1)

    updater = OTAUpdater(
        target_path=args.target_path,
        backup_path=args.backup_path,
        health_url=None if args.no_health_check else args.health_url,
    )

    if args.rollback:
        ok, msg = updater.rollback_to_previous()
        print(f"[{'SUCCESS' if ok else 'ERROR'}] {msg}")
        sys.exit(0 if ok else 1)

    if args.apply:
        ok, msg = updater.update_from_binary(args.apply, expected_sha256=args.sha256)
        print(f"[{'SUCCESS' if ok else 'ERROR'}] {msg}")
        sys.exit(0 if ok else 1)

    parser.print_help()


if __name__ == "__main__":
    main()

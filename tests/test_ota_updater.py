"""
SafeHalo OTA 远程热升级与回滚测试用例 (tests/test_ota_updater.py)
"""

import http.server
import json
import os
import socketserver
import struct
import tempfile
import threading
import time
import pytest

from scripts.ota_updater import (
    ArchitectureDetector,
    HashVerifier,
    AtomicReplacer,
    HealthCheckWatchdog,
    OTAUpdater,
)


def create_mock_elf(path: str, is_64bit: bool, is_little_endian: bool, machine_id: int):
    """构建极简合法的 ELF 标头用于架构测试"""
    magic = b"\x7fELF"
    ei_class = b"\x02" if is_64bit else b"\x01"
    ei_data = b"\x01" if is_little_endian else b"\x02"
    ei_version = b"\x01"
    ei_osabi = b"\x00"
    ei_pad = b"\x00" * 8
    
    e_type = b"\x02\x00" if is_little_endian else b"\x00\x02" # ET_EXEC
    endian_fmt = "<" if is_little_endian else ">"
    e_machine = struct.pack(endian_fmt + "H", machine_id)
    
    header = magic + ei_class + ei_data + ei_version + ei_osabi + ei_pad + e_type + e_machine
    padding = b"\x00" * (120 * 1024)  # 填充至 >100KB 符合大小限制
    with open(path, "wb") as f:
        f.write(header + padding)
    os.chmod(path, 0o755)


def test_architecture_detection_mock_elfs():
    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. MIPS Little-Endian (mipsel)
        p_mipsel = os.path.join(tmpdir, "test_mipsel")
        create_mock_elf(p_mipsel, is_64bit=False, is_little_endian=True, machine_id=8)
        assert ArchitectureDetector.inspect_binary_arch(p_mipsel) == "mipsel"

        # 2. MIPS Big-Endian (mips)
        p_mips = os.path.join(tmpdir, "test_mips")
        create_mock_elf(p_mips, is_64bit=False, is_little_endian=False, machine_id=8)
        assert ArchitectureDetector.inspect_binary_arch(p_mips) == "mips"

        # 3. AArch64 (ARM64)
        p_aarch64 = os.path.join(tmpdir, "test_aarch64")
        create_mock_elf(p_aarch64, is_64bit=True, is_little_endian=True, machine_id=183)
        assert ArchitectureDetector.inspect_binary_arch(p_aarch64) == "aarch64"

        # 4. x86_64
        p_x86_64 = os.path.join(tmpdir, "test_x86_64")
        create_mock_elf(p_x86_64, is_64bit=True, is_little_endian=True, machine_id=62)
        assert ArchitectureDetector.inspect_binary_arch(p_x86_64) == "x86_64"

        # 5. Non-existent file
        assert ArchitectureDetector.inspect_binary_arch(os.path.join(tmpdir, "no_file")) == "file_not_found"


def test_architecture_compatibility():
    assert ArchitectureDetector.is_compatible("aarch64", "arm64") is True
    assert ArchitectureDetector.is_compatible("darwin-arm64", "darwin-arm64") is True
    assert ArchitectureDetector.is_compatible("darwin-arm64", "darwin-universal") is True
    assert ArchitectureDetector.is_compatible("mipsel", "mipsel") is True
    assert ArchitectureDetector.is_compatible("mipsel", "mips") is False
    assert ArchitectureDetector.is_compatible("x86_64", "aarch64") is False


def test_hash_verifier():
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "payload.bin")
        with open(test_file, "wb") as f:
            f.write(b"SAFEHALO_VERIFICATION_PAYLOAD_TEST_12345")

        sha = HashVerifier.compute_sha256(test_file)
        assert len(sha) == 64
        assert HashVerifier.verify(test_file, sha) is True
        assert HashVerifier.verify(test_file, "0000000000000000000000000000000000000000000000000000000000000000") is False


def test_atomic_replacer_and_rollback():
    with tempfile.TemporaryDirectory() as tmpdir:
        target = os.path.join(tmpdir, "safehalo-daemon")
        backup = os.path.join(tmpdir, "safehalo-daemon.old")
        new_bin = os.path.join(tmpdir, "safehalo-daemon.v2")

        with open(target, "w") as f:
            f.write("BINARY_V1")
        with open(new_bin, "w") as f:
            f.write("BINARY_V2")

        # 1. 替换
        ok, msg = AtomicReplacer.replace(new_bin, target, backup)
        assert ok is True
        assert os.path.exists(target)
        assert os.path.exists(backup)
        with open(target, "r") as f:
            assert f.read() == "BINARY_V2"
        with open(backup, "r") as f:
            assert f.read() == "BINARY_V1"

        # 2. 回滚
        ok_rb, msg_rb = AtomicReplacer.rollback(target, backup)
        assert ok_rb is True
        with open(target, "r") as f:
            assert f.read() == "BINARY_V1"


def test_ota_updater_rejection_conditions():
    with tempfile.TemporaryDirectory() as tmpdir:
        target = os.path.join(tmpdir, "target_bin")
        backup = os.path.join(tmpdir, "backup_bin")
        updater = OTAUpdater(target_path=target, backup_path=backup, health_url=None)

        # 1. 文件不存在
        ok, msg = updater.update_from_binary(os.path.join(tmpdir, "nonexistent"))
        assert ok is False
        assert "不存在" in msg

        # 2. 文件体积过小
        tiny = os.path.join(tmpdir, "tiny_bin")
        with open(tiny, "wb") as f:
            f.write(b"short")
        ok, msg = updater.update_from_binary(tiny)
        assert ok is False
        assert "体积异常过小" in msg

        # 3. SHA-256 不匹配
        large_bad = os.path.join(tmpdir, "large_bad")
        with open(large_bad, "wb") as f:
            f.write(b"\x00" * (120 * 1024))
        ok, msg = updater.update_from_binary(large_bad, expected_sha256="badhash123")
        assert ok is False
        assert "SHA-256 校验不匹配" in msg

        # 4. 架构不匹配 (制造 mipsel 尝试在当前宿主更新)
        current_host = ArchitectureDetector.detect_host_arch()
        other_arch_machine = 8 if current_host != "mips" else 183
        mock_mismatch = os.path.join(tmpdir, "mock_mismatch")
        create_mock_elf(mock_mismatch, is_64bit=False, is_little_endian=True, machine_id=other_arch_machine)
        ok, msg = updater.update_from_binary(mock_mismatch)
        assert ok is False
        assert "架构不匹配" in msg


def test_ota_watchdog_auto_rollback():
    """验证健康检查未通过时看门狗自动原子回滚至老版本"""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = os.path.join(tmpdir, "safehalo-daemon")
        backup = os.path.join(tmpdir, "safehalo-daemon.old")
        
        # 初始版本 V1
        with open(target, "wb") as f:
            f.write(b"ORIGINAL_WORKING_DAEMON_V1" + b"\x00" * (120 * 1024))
        os.chmod(target, 0o755)

        # 新版本 V2 (复制真实的本地 safehalo-daemon 保证架构兼容性)
        real_daemon = os.path.join(os.path.dirname(__file__), "../experiments/rust-daemon/target/debug/safehalo-daemon")
        if not os.path.exists(real_daemon):
            pytest.skip("safehalo-daemon not built")

        # 启动 OTAUpdater，指向一个无法连通的端口 (127.0.0.1:59999)
        updater = OTAUpdater(target_path=target, backup_path=backup, health_url="http://127.0.0.1:59999/api/health")

        # Mock HealthCheckWatchdog timeout to 0.5s for fast test
        orig_wait = HealthCheckWatchdog.wait_for_healthy
        try:
            HealthCheckWatchdog.wait_for_healthy = lambda url, timeout_seconds=10.0: False
            ok, msg = updater.update_from_binary(real_daemon)
            
            # 应该返回失败并触发自动回滚
            assert ok is False
            assert "看门狗已自动原子回滚至旧版本" in msg
            
            # target 应该被恢复为 V1 内容！
            with open(target, "rb") as f:
                content = f.read()
                assert content.startswith(b"ORIGINAL_WORKING_DAEMON_V1")
        finally:
            HealthCheckWatchdog.wait_for_healthy = orig_wait


def test_probe_ota_preparation():
    with tempfile.TemporaryDirectory() as tmpdir:
        firmware_path = os.path.join(tmpdir, "esp32_firmware.bin")
        # ESP32 固件标头魔数为 0xE9
        with open(firmware_path, "wb") as f:
            f.write(b"\xE9\x03\x02\x20" + b"\xFF" * 1024)

        ok, res = OTAUpdater.prepare_probe_ota(firmware_path)
        assert ok is True
        assert res["size_bytes"] == 1028
        assert res["downlink_command"].startswith("OTA_BEGIN:1028:")

        # 错误魔数
        bad_firmware = os.path.join(tmpdir, "bad_firmware.bin")
        with open(bad_firmware, "wb") as f:
            f.write(b"\x00\x00\x00\x00")
        ok_bad, res_bad = OTAUpdater.prepare_probe_ota(bad_firmware)
        assert ok_bad is False
        assert "非法 ESP32-IDF 固件" in res_bad["error"]


def test_generate_manifest():
    with tempfile.TemporaryDirectory() as tmpdir:
        bin_dir = os.path.join(tmpdir, "bins")
        fw_dir = os.path.join(tmpdir, "fws")
        os.makedirs(bin_dir)
        os.makedirs(fw_dir)

        p1 = os.path.join(bin_dir, "safehalo-mipsel")
        create_mock_elf(p1, is_64bit=False, is_little_endian=True, machine_id=8)

        fw1 = os.path.join(fw_dir, "probe_v1.bin")
        with open(fw1, "wb") as f:
            f.write(b"\xE9" + b"\x00" * 200)

        manifest = OTAUpdater.generate_manifest(version="2.0.0", binaries_dir=bin_dir, firmware_dir=fw_dir)
        assert manifest["version"] == "2.0.0"
        assert "mipsel" in manifest["binaries"]
        assert manifest["binaries"]["mipsel"]["filename"] == "safehalo-mipsel"
        assert "probe_v1.bin" in manifest["firmware"]

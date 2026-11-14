#!/bin/sh
# SafeHalo daemon installer for OpenWrt (musl static binary, mipsel/mips/aarch64/x86_64).
# Usage: ./install.sh [SRC_DIR] [ARCH]   (defaults: script dir, auto-detect)
set -e

if [ "$1" = "--rollback" ]; then
	if [ -f /usr/bin/safehalo-daemon.old ]; then
		echo "[safehalo] rolling back /usr/bin/safehalo-daemon from /usr/bin/safehalo-daemon.old..."
		cp -p /usr/bin/safehalo-daemon.old /usr/bin/safehalo-daemon
		chmod 0755 /usr/bin/safehalo-daemon
		if [ -x /etc/init.d/safehalo ]; then
			/etc/init.d/safehalo restart || true
		fi
		echo "[safehalo] rollback completed successfully."
		exit 0
	else
		echo "error: no previous binary /usr/bin/safehalo-daemon.old found to rollback" >&2
		exit 1
	fi
fi

SRC="${1:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
ARCH_INPUT="$2"

# 1. 架构探测：优先读取 OpenWrt 发行版信息，其次检查 ELF 字节序，解决部分 MTK mipsel 内核报告 mips 的问题
detect_arch() {
	if [ -n "$ARCH_INPUT" ]; then
		echo "$ARCH_INPUT"
		return
	fi

	# 尝试读取 OpenWrt release 信息
	if [ -f /etc/openwrt_release ]; then
		dist_arch=$(grep "DISTRIB_ARCH=" /etc/openwrt_release 2>/dev/null | cut -d"'" -f2 | cut -d'_' -f1)
		case "$dist_arch" in
			mipsel*) echo "mipsel"; return ;;
			mips*) echo "mips"; return ;;
			aarch64*|arm64*) echo "aarch64"; return ;;
			x86_64*) echo "x86_64"; return ;;
		esac
	fi

	raw_arch=$(uname -m)
	case "$raw_arch" in
		mipsel) echo "mipsel" ;;
		mips)
			# 检查系统自身二进制的 ELF 字节序 (第 6 字节: 1 = LE, 2 = BE)
			target="/bin/busybox"
			[ -f "$target" ] || target="/bin/sh"
			if [ -f "$target" ]; then
				endian_byte=$(dd if="$target" bs=1 skip=5 count=1 2>/dev/null | od -An -t u1 2>/dev/null | tr -d ' ' || true)
				if [ "$endian_byte" = "1" ]; then
					echo "mipsel"
					return
				fi
			fi
			echo "mips"
			;;
		aarch64|arm64) echo "aarch64" ;;
		x86_64|amd64) echo "x86_64" ;;
		*) echo "$raw_arch" ;;
	esac
}

ARCH=$(detect_arch)
echo "[safehalo] detected target architecture: $ARCH"

# 2. 预检磁盘空间 (至少需要 3MB 空间)
check_disk_space() {
	dest_dir="${1:-/usr/bin}"
	if command -v df >/dev/null 2>&1; then
		avail_kb=$(df -k "$dest_dir" 2>/dev/null | awk 'NR==2 {print $4}')
		if [ -n "$avail_kb" ] && [ "$avail_kb" -lt 3000 ]; then
			echo "error: insufficient disk space on $dest_dir (${avail_kb}KB available, require at least 3000KB)" >&2
			exit 1
		fi
	fi
}
check_disk_space "/usr/bin"

# 3. 检查 HTTPS Webhook 工具链支持
if command -v curl >/dev/null 2>&1; then
	echo "[safehalo] found HTTPS helper: curl"
elif command -v uclient-fetch >/dev/null 2>&1; then
	echo "[safehalo] found HTTPS helper: uclient-fetch"
elif command -v wget >/dev/null 2>&1; then
	echo "[safehalo] found HTTPS helper: wget"
else
	echo "[safehalo] warning: no curl/uclient-fetch/wget found. Webhook notifications over HTTPS may fail." >&2
fi

# 4. 二进制查找与架构匹配
TRIPLE=""
case "$ARCH" in
	mipsel) TRIPLE="mipsel-unknown-linux-musl" ;;
	mips) TRIPLE="mips-unknown-linux-musl" ;;
	aarch64|arm64) TRIPLE="aarch64-unknown-linux-musl" ;;
	x86_64|amd64) TRIPLE="x86_64-unknown-linux-musl" ;;
esac

BIN=""
if [ -f "$SRC/safehalo-daemon" ]; then
	BIN="$SRC/safehalo-daemon"
elif [ -n "$TRIPLE" ] && [ -f "$SRC/../target/$TRIPLE/release/safehalo-daemon" ]; then
	BIN="$SRC/../target/$TRIPLE/release/safehalo-daemon"
fi
if [ -z "$BIN" ]; then
	echo "error: no safehalo-daemon binary for '$ARCH' (run cross/build-all.sh first or pass SRC_DIR)" >&2
	exit 1
fi
echo "[safehalo] using binary: $BIN ($ARCH)"

[ -f "$SRC/safehalo.init" ] || { echo "error: safehalo.init not found in $SRC" >&2; exit 1; }
[ -f "$SRC/safehalo.config" ] || { echo "error: safehalo.config not found in $SRC" >&2; exit 1; }

install_file() {
	if command -v install >/dev/null 2>&1; then
		install -m "$2" "$1" "$3"
	else
		cp "$1" "$3"
		chmod "$2" "$3"
	fi
}

mkdir -p /usr/bin /etc/init.d /etc/config /etc/safehalo/data

# 5. 备份现有二进制与配置
if [ -f /usr/bin/safehalo-daemon ]; then
	cp -p /usr/bin/safehalo-daemon /usr/bin/safehalo-daemon.old
	echo "[safehalo] backed up previous binary to /usr/bin/safehalo-daemon.old"
fi
if [ -f /etc/config/safehalo ]; then
	bak_path="/etc/config/safehalo.bak.$(date +%Y%m%d%H%M%S)"
	cp -p /etc/config/safehalo "$bak_path"
	echo "[safehalo] backed up existing config to $bak_path"
fi

# 6. 安装文件
install_file "$BIN" 0755 /usr/bin/safehalo-daemon
install_file "$SRC/safehalo.init" 0755 /etc/init.d/safehalo
if [ ! -f /etc/config/safehalo ]; then
	install_file "$SRC/safehalo.config" 0644 /etc/config/safehalo
	echo "[safehalo] wrote default /etc/config/safehalo (edit input/serial_dev/home_mac/room_name for your site)"
fi

# 7. 试运行测试二进制兼容性 (防止错架构二进制导致 procd 崩溃死循环)
if ! /usr/bin/safehalo-daemon --help >/dev/null 2>&1; then
	test_rc=$?
	if [ "$test_rc" -eq 126 ] || [ "$test_rc" -eq 127 ]; then
		echo "error: binary execution failed (exit code $test_rc). Architecture mismatch for '$ARCH'!" >&2
		if [ -f /usr/bin/safehalo-daemon.old ]; then
			echo "[safehalo] auto-rolling back to /usr/bin/safehalo-daemon.old..."
			cp -p /usr/bin/safehalo-daemon.old /usr/bin/safehalo-daemon
		fi
		exit 1
	fi
fi

/etc/init.d/safehalo enable
echo "=================================================================="
echo "SafeHalo Daemon 安装成功并已加入自启！"
echo "  启动命令: /etc/init.d/safehalo start"
echo "  状态查看: /etc/init.d/safehalo status"
echo "  实时日志: logread -e safehalo -f"
echo "  控制台:   http://<router-ip>:8091/"
echo "  HA遥测:   http://<router-ip>:8091/api/ha/status"
echo "  配置文件: /etc/config/safehalo"
echo "  数据目录: /etc/safehalo (0600 权限保护)"
echo "=================================================================="

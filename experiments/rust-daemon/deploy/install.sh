#!/bin/sh
# SafeHalo daemon installer for OpenWrt (musl static binary, mipsel/mips/aarch64/x86_64).
# Usage: ./install.sh [SRC_DIR] [ARCH]   (defaults: script dir, auto-detect)
set -e

SRC="${1:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
ARCH="${2:-$(uname -m)}"

# Binaries built by ../cross/build-all.sh live in ../target/<triple>/release/.
# Selection priority: explicit SRC_DIR first, then the target dir matching this
# machine's architecture. Never glob all targets - that would install an
# arbitrary-architecture binary.
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
echo "installing binary: $BIN ($ARCH)"

[ -f "$SRC/safehalo.init" ] || { echo "error: safehalo.init not found in $SRC" >&2; exit 1; }
[ -f "$SRC/safehalo.config" ] || { echo "error: safehalo.config not found in $SRC" >&2; exit 1; }

# busybox images may lack the `install` applet, so fall back to cp+chmod
install_file() {
	if command -v install >/dev/null 2>&1; then
		install -m "$2" "$1" "$3"
	else
		cp "$1" "$3"
		chmod "$2" "$3"
	fi
}

mkdir -p /usr/bin /etc/init.d /etc/config /etc/safehalo/data
install_file "$BIN" 0755 /usr/bin/safehalo-daemon
install_file "$SRC/safehalo.init" 0755 /etc/init.d/safehalo
if [ ! -f /etc/config/safehalo ]; then
	install_file "$SRC/safehalo.config" 0644 /etc/config/safehalo
	echo "wrote default /etc/config/safehalo (edit input/serial_dev/home_mac/room_name for your site)"
fi

/etc/init.d/safehalo enable
echo "installed and enabled. Start with: /etc/init.d/safehalo start"
echo "console: http://<router-ip>:8091/   status: http://<router-ip>:8091/api/ha/status"
echo "config: /etc/config/safehalo   data: /etc/safehalo (users.json, incidents, events, model)"

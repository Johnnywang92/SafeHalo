#!/bin/sh
# Reproducible musl static cross-builds for the SafeHalo daemon prototype.
# Requires: rustup (stable + nightly with rust-src), zig 0.17+ (brew install zig).
#
# Outputs (all statically linked, stripped):
#   target/mipsel-unknown-linux-musl/release/safehalo-daemon   OpenWrt ramips (MT7621 etc.)
#   target/mips-unknown-linux-musl/release/safehalo-daemon     OpenWrt ar71xx (QCA953x etc.)
#   target/aarch64-unknown-linux-musl/release/safehalo-daemon  OpenWrt Filogic/IPQ807x, RPi
#   target/x86_64-unknown-linux-musl/release/safehalo-daemon   x86 soft routers / containers
set -e
cd "$(dirname "$0")/.."

rustup target add aarch64-unknown-linux-musl x86_64-unknown-linux-musl

export RUSTFLAGS="-C link-self-contained=no"
CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER="$PWD/cross/zigcc-aarch64-musl.sh" \
  cargo build --release --target aarch64-unknown-linux-musl
CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER="$PWD/cross/zigcc-x86_64-musl.sh" \
  cargo build --release --target x86_64-unknown-linux-musl

# MIPS: no prebuilt std on stable -> nightly build-std.
# panic=abort + empty std features drop _Unwind_* refs; wrappers strip -pie/-lgcc_s
# because zig 0.17's mips musl static-PIE/compiler-rt variants emit unresolved
# zig-std symbols (heap.*, mem.eqlBytes).
export RUSTFLAGS="-C link-self-contained=no -C panic=abort"
for pair in "mipsel:mipsel" "mips:mips"; do
  name="${pair%%:*}"
  target="${pair##*:}-unknown-linux-musl"
  env_name=$(echo "$target" | tr '[:lower:]-' '[:upper:]_')
  CARGO_TARGET_${env_name}_LINKER="$PWD/cross/zigcc-${name}-musl.sh" \
    cargo +nightly build --release \
      -Z build-std=std,panic_abort -Z build-std-features="" \
      --target "$target"
done

echo "---"
for t in mipsel-unknown-linux-musl mips-unknown-linux-musl aarch64-unknown-linux-musl x86_64-unknown-linux-musl; do
  file "target/$t/release/safehalo-daemon"
done
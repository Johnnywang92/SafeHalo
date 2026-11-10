#!/bin/sh
# Branch-coverage equivalence: VERDICT_OBJECT / VERDICT_RECOVERED /
# ROUTER_OFFLINE / contaminated boot (cold fallback + warm hourly fallback).
# Runs the Python reference engine and the Rust daemon on synthetic scenario
# replays and compares traces + snapshots event-by-event.
#
# Usage: sh verify/run_scenarios.sh [SCN_DIR]   (default /tmp/fh_bench/scn)
set -e
cd "$(dirname "$0")/.."
DAEMON=./target/release/safehalo-daemon
[ -x "$DAEMON" ] || { echo "missing $DAEMON - run: cargo build --release"; exit 1; }

SCN_DIR="${1:-/tmp/fh_bench/scn}"
rm -rf "$SCN_DIR"
python3 verify/gen_scenarios.py "$SCN_DIR"

run_pair() {
    name="$1" warm="$2"
    rm -rf "$SCN_DIR/py_$name" "$SCN_DIR/rust_$name"
    mkdir -p "$SCN_DIR/rust_$name/storage"
    if [ "$warm" = "warm" ]; then
        mkdir -p "$SCN_DIR/py_$name/storage"
        cp "$SCN_DIR/py_offline/storage/baseline_model.json" "$SCN_DIR/py_$name/storage/"
        cp "$SCN_DIR/py_offline/storage/baseline_model.json" "$SCN_DIR/rust_$name/storage/"
        TZ=UTC WARM=1 python3 verify/ref_python.py "$SCN_DIR/$name.txt" "$SCN_DIR/py_$name" >/dev/null 2>&1
    else
        TZ=UTC python3 verify/ref_python.py "$SCN_DIR/$name.txt" "$SCN_DIR/py_$name" >/dev/null 2>&1
    fi
    "$DAEMON" --replay "$SCN_DIR/$name.txt" --no-http \
        --storage-dir "$SCN_DIR/rust_$name/storage" \
        --trace "$SCN_DIR/rust_$name/trace.jsonl" \
        --snapshot "$SCN_DIR/rust_$name/snapshot.json" >/dev/null 2>&1
    if python3 verify/compare.py "$SCN_DIR/rust_$name" "$SCN_DIR/py_$name" \
        "$SCN_DIR/rust_$name/trace.jsonl" "$SCN_DIR/rust_$name/snapshot.json" >/dev/null 2>&1; then
        echo "PASS  $name"
    else
        echo "FAIL  $name (see $SCN_DIR/{py,rust}_$name)"
        return 1
    fi
}

status=0
run_pair object     cold || status=1
run_pair recovered  cold || status=1
run_pair offline    cold || status=1
run_pair contaminated cold || status=1
run_pair contaminated_warm warm || status=1

if [ "$status" -eq 0 ]; then
    echo "all scenario comparisons PASS"
fi
exit $status

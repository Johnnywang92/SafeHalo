#!/bin/sh
# Branch-coverage equivalence: VERDICT_OBJECT / VERDICT_RECOVERED /
# ROUTER_OFFLINE / contaminated boot (cold fallback + warm hourly fallback),
# plus stage-2 OBJECT confirmation (flag off and flag on).
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

# Stage-2 OBJECT confirmation: the shared replay reaches stage-1 OBJECT at
# ~13 s, then the 60 s watch runs over regular / irregular / flat ground
# signals. secs=0 must stay byte-identical to the pre-stage-2 engine.
run_stage2() {
    name="$1" secs="$2" want="$3"
    rm -rf "$SCN_DIR/py2_$name" "$SCN_DIR/rust2_$name"
    mkdir -p "$SCN_DIR/py2_$name" "$SCN_DIR/rust2_$name/storage"
    TZ=UTC OBJECT_CONFIRM_SECS="$secs" python3 verify/ref_python.py \
        "$SCN_DIR/object_confirm_$name.txt" "$SCN_DIR/py2_$name" >/dev/null 2>&1
    "$DAEMON" --replay "$SCN_DIR/object_confirm_$name.txt" --no-http \
        --storage-dir "$SCN_DIR/rust2_$name/storage" \
        --trace "$SCN_DIR/rust2_$name/trace.jsonl" \
        --snapshot "$SCN_DIR/rust2_$name/snapshot.json" \
        --object-confirm-secs "$secs" >/dev/null 2>&1
    if ! python3 verify/compare.py "$SCN_DIR/rust2_$name" "$SCN_DIR/py2_$name" \
        "$SCN_DIR/rust2_$name/trace.jsonl" "$SCN_DIR/rust2_$name/snapshot.json" >/dev/null 2>&1; then
        echo "FAIL  stage2_$name (see $SCN_DIR/{py2,rust2}_$name)"
        return 1
    fi
    if [ -n "$want" ] && ! grep -q "\"phase\": \"$want\"" "$SCN_DIR/py2_$name/trace.jsonl"; then
        echo "FAIL  stage2_$name: expected object_watch phase '$want'"
        return 1
    fi
    echo "PASS  stage2_$name${want:+ ($want)}"
}

run_stage2 regular   0  ""           || status=1   # flag off, legacy path
run_stage2 irregular 0  ""           || status=1
run_stage2 flat      0  ""           || status=1
run_stage2 abort     0  ""           || status=1
run_stage2 regular   60 escalate     || status=1
run_stage2 irregular 60 confirmed    || status=1
run_stage2 flat      60 insufficient || status=1
run_stage2 abort     60 aborted      || status=1

if [ "$status" -eq 0 ]; then
    echo "all scenario comparisons PASS"
fi
exit $status

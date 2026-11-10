#!/bin/sh
# Golden-fixture regression: regenerate the synthetic replay, verify every hash
# in fixtures/CHECKSUMS, then run the Python production engine and the Rust
# daemon with the feature flag OFF and compare each of them against the
# committed golden outputs as well as against each other.
#
# A PASS here means "flag off is still bit-identical to the behaviour the
# golden files were captured from".
#
# Usage: sh verify/run_fixtures.sh [WORK_DIR]   (default /tmp/safehalo-fixtures)
set -e
cd "$(dirname "$0")/.."
DAEMON=./target/release/safehalo-daemon
[ -x "$DAEMON" ] || { echo "missing $DAEMON - run: cargo build --release"; exit 1; }

WORK="${1:-/tmp/safehalo-fixtures}"
GOLDEN=fixtures/golden/flag_off
rm -rf "$WORK"
mkdir -p "$WORK/py" "$WORK/rust/storage"

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

expected() {
    awk -v p="$1" '$1 !~ /^#/ && $2 == p { print $1 }' fixtures/CHECKSUMS
}

check_hash() {
    got=$(sha256 "$1")
    rel="${1#fixtures/}"
    want=$(expected "$rel")
    if [ -z "$want" ]; then
        echo "FAIL  $rel has no entry in fixtures/CHECKSUMS"
        return 1
    fi
    if [ "$got" != "$want" ]; then
        echo "FAIL  $rel hash $got != recorded $want"
        return 1
    fi
    echo "ok    $rel"
}

status=0

python3 fixtures/gen_fixture.py fixtures/generated >/dev/null
check_hash fixtures/generated/main.txt || status=1
check_hash "$GOLDEN/trace.jsonl" || status=1
check_hash "$GOLDEN/snapshot.json" || status=1
[ "$status" -eq 0 ] || { echo "fixture hashes diverged - see fixtures/README.md"; exit 1; }

TZ=UTC python3 verify/ref_python.py fixtures/generated/main.txt "$WORK/py" >/dev/null
"$DAEMON" --replay fixtures/generated/main.txt --no-http \
    --storage-dir "$WORK/rust/storage" \
    --trace "$WORK/rust/trace.jsonl" \
    --snapshot "$WORK/rust/snapshot.json" >/dev/null

cmp_golden() {
    label="$1" trace="$2" snap="$3"
    if python3 verify/compare.py "$WORK" "$GOLDEN" "$trace" "$snap" >/dev/null 2>&1; then
        echo "PASS  $label vs golden(flag_off)"
    else
        echo "FAIL  $label vs golden(flag_off) - behaviour changed with the flag off"
        python3 verify/compare.py "$WORK" "$GOLDEN" "$trace" "$snap" | tail -12
        status=1
    fi
}

cmp_golden python "$WORK/py/trace.jsonl" "$WORK/py/snapshot.json"
cmp_golden rust "$WORK/rust/trace.jsonl" "$WORK/rust/snapshot.json"

if python3 verify/compare.py "$WORK/rust" "$WORK/py" \
    "$WORK/rust/trace.jsonl" "$WORK/rust/snapshot.json" >/dev/null 2>&1; then
    echo "PASS  rust vs python (same run)"
else
    echo "FAIL  rust vs python (same run)"
    python3 verify/compare.py "$WORK/rust" "$WORK/py" \
        "$WORK/rust/trace.jsonl" "$WORK/rust/snapshot.json" | tail -12
    status=1
fi

[ "$status" -eq 0 ] && echo "all fixture checks PASS"
exit $status

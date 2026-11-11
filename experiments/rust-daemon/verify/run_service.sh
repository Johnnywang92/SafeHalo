#!/bin/sh
# Standalone-service regression: HTTP auth/RBAC/static console/SSE/backfill,
# incident persistence and notification fan-out, serial actuation over a pty,
# and serial-loss detection. Detection equivalence lives in run_scenarios.sh
# and run_fixtures.sh; this suite only exercises the service layer.
#
# Usage: sh verify/run_service.sh [WORK_DIR]   (default /tmp/safehalo-svc)
set -e
cd "$(dirname "$0")/.."
[ -x ./target/release/safehalo-daemon ] || { echo "missing ./target/release/safehalo-daemon - run: cargo build --release"; exit 1; }

WORK="${1:-/tmp/safehalo-svc}"
exec python3 verify/service_check.py "$WORK"

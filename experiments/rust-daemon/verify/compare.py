#!/usr/bin/env python3
"""Compare Rust daemon vs Python reference trace + snapshot."""
import json
import sys

RUST_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fh_bench/eq"
PY_DIR = sys.argv[2] if len(sys.argv) > 2 else "/tmp/fh_bench/eq/py"
RUST_TRACE = sys.argv[3] if len(sys.argv) > 3 else RUST_DIR + "/rust_trace.jsonl"
RUST_SNAP = sys.argv[4] if len(sys.argv) > 4 else RUST_DIR + "/rust_snapshot.json"


def close(a, b):
    return a == b or abs(a - b) <= 1e-9 * max(abs(a), abs(b), 1e-12)


def cmp_node(a, b, path, diffs):
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            diffs.append(f"{path}: key mismatch {sorted(set(a) ^ set(b))}")
            return
        for k in a:
            if k == "last_updated":
                continue  # wall-clock save timestamp, not behavioral
            cmp_node(a[k], b[k], f"{path}.{k}", diffs)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append(f"{path}: length {len(a)} vs {len(b)}")
            return
        for i, (x, y) in enumerate(zip(a, b)):
            cmp_node(x, y, f"{path}[{i}]", diffs)
    elif isinstance(a, bool) or isinstance(b, bool):
        if a is not b and a != b:
            diffs.append(f"{path}: {a!r} vs {b!r}")
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if not close(a, b):
            diffs.append(f"{path}: {a!r} vs {b!r}")
    else:
        if a != b:
            diffs.append(f"{path}: {a!r} vs {b!r}")


rt = [json.loads(l) for l in open(RUST_TRACE, encoding="utf-8") if l.strip()]
pt = [json.loads(l) for l in open(PY_DIR + "/trace.jsonl", encoding="utf-8") if l.strip()]
diffs = []
if len(rt) != len(pt):
    diffs.append(f"trace length: rust={len(rt)} python={len(pt)}")
for i, (a, b) in enumerate(zip(rt, pt)):
    if a.get("type") != b.get("type"):
        diffs.append(f"trace[{i}]: type {a.get('type')} vs {b.get('type')}")
        continue
    if a.get("ts") != b.get("ts"):
        diffs.append(f"trace[{i}] ({a['type']}): ts {a.get('ts')} vs {b.get('ts')}")
    cmp_node(a, b, f"trace[{i}]", diffs)

rs = json.load(open(RUST_SNAP, encoding="utf-8"))
ps = json.load(open(PY_DIR + "/snapshot.json", encoding="utf-8"))
cmp_node(rs, ps, "snapshot", diffs)

print(f"trace events: rust={len(rt)} python={len(pt)}")
print(f"state transitions: rust={sum(1 for e in rt if e['type']=='state_change')} "
      f"python={sum(1 for e in pt if e['type']=='state_change')}")
if diffs:
    print(f"FAIL: {len(diffs)} differences")
    for d in diffs[:20]:
        print("  " + d)
    sys.exit(1)
print("PASS: trace and snapshot are behaviorally identical")
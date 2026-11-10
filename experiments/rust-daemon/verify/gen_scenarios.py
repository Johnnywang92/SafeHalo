#!/usr/bin/env python3
"""Deterministic scenario replays covering detector branches left untested by
the main replay: VERDICT_OBJECT, VERDICT_RECOVERED, ROUTER_OFFLINE and
contaminated boot calibration (cold + warm fallback).

Writes <out_dir>/{object,recovered,offline,contaminated,contaminated_warm}.txt
(contaminated_warm.txt is byte-identical to contaminated.txt; only storage differs).

Note: VERDICT_OBJECT is only reachable when the post-impact signal is almost
perfectly frozen - the ground-respiration peak detector flags 1-6 peaks in the
last 150 frames, and quantization noise alone produces ~6, so any jittery
signal reads as "vital signs". The frozen phase uses time-invariant CSI values.
"""
import math
import random
import sys

HOME = "c0:06:c3:11:22:33"
BASE_US = 1_700_000_000_000_000


def emit(out, rng, ts_us, walk=0.0, spike=0.0, breath=0.8, noise=0.6, frozen=False):
    vals = []
    for k in range(52):
        base = 24.0 + 9.0 * math.sin(k / 8.0)
        if frozen:
            amp = base
            phase = 0.3 * math.sin(k / 6.0)
        else:
            motion = walk * 6.0 * math.sin(ts_us / 140000.0 + k / 5.0)
            breath_term = breath * 0.8 * math.sin(ts_us / 1250000.0 + k / 9.0)
            amp = base + motion + breath_term + spike * math.sin(k / 3.0) + rng.gauss(0, noise)
            amp = max(2.0, min(120.0, amp))
            phase = 0.3 * math.sin(k / 6.0) + ts_us / 50000000.0 + rng.gauss(0, 0.05)
        vals.append(int(amp * math.sin(phase)))
        vals.append(int(amp * math.cos(phase)))
    fields = ["CSI_DATA", "0", "0", HOME, "-48", "11", "1", "7", "1", "1",
              "0", "0", "0", "0", "1", "-95", "0", "6", "0", str(ts_us),
              "0", "104", "0", "104", "0"]
    out.write(",".join(fields) + ",[" + ", ".join(str(v) for v in vals) + "]\n")


class Line:
    def __init__(self):
        self.ts_us = BASE_US

    def phase(self, out, rng, n, interval_us=20_000, **kw):
        for _ in range(n):
            emit(out, rng, self.ts_us, **kw)
            self.ts_us += interval_us


def gen_object(out_dir):
    rng = random.Random(101)
    with open(f"{out_dir}/object.txt", "w") as out:
        ln = Line()
        ln.phase(out, rng, 260)                                   # clean calibration 5.2s
        ln.phase(out, rng, 110, walk=6.0)                         # person walks -> MOVING
        ln.phase(out, rng, 12, walk=6.0, spike=6.0, noise=1.5)    # violent impact
        ln.phase(out, rng, 300, frozen=True)                      # dead-still floor: no vitals
        ln.phase(out, rng, 120)                                   # quiet room again


def gen_recovered(out_dir):
    rng = random.Random(202)
    with open(f"{out_dir}/recovered.txt", "w") as out:
        ln = Line()
        ln.phase(out, rng, 260)                                   # clean calibration
        ln.phase(out, rng, 110, walk=6.0)                         # walking -> MOVING
        ln.phase(out, rng, 12, walk=6.0, spike=6.0, noise=1.5)    # impact
        ln.phase(out, rng, 60, frozen=True)                       # brief still -> FALL_SUSPECT
        ln.phase(out, rng, 150, walk=6.0)                         # gets up and walks -> RECOVERED
        ln.phase(out, rng, 120)                                   # quiet again


def gen_offline(out_dir):
    rng = random.Random(303)
    with open(f"{out_dir}/offline.txt", "w") as out:
        ln = Line()
        ln.phase(out, rng, 260)                                   # clean calibration @50Hz
        ln.phase(out, rng, 24, interval_us=500_000)               # 2Hz -> ROUTER_OFFLINE
        ln.phase(out, rng, 200)                                   # stream resumes @50Hz
        ln.phase(out, rng, 24, interval_us=500_000)               # drops out again
        ln.phase(out, rng, 200)                                   # and recovers


def gen_contaminated(out_dir):
    # strong walking during the boot calibration window -> contaminated baseline rejection
    rng = random.Random(404)
    with open(f"{out_dir}/contaminated.txt", "w") as out:
        ln = Line()
        ln.phase(out, rng, 260, walk=6.0)                         # person present during calibration
        ln.phase(out, rng, 260)                                   # room goes quiet


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fh_bench/scn"
    import os
    os.makedirs(out_dir, exist_ok=True)
    gen_object(out_dir)
    gen_recovered(out_dir)
    gen_offline(out_dir)
    gen_contaminated(out_dir)
    # byte-identical copy: warm-boot variant only differs by persisted storage
    with open(f"{out_dir}/contaminated.txt") as f, open(f"{out_dir}/contaminated_warm.txt", "w") as g:
        g.write(f.read())
    for name in ("object", "recovered", "offline", "contaminated", "contaminated_warm"):
        n = sum(1 for _ in open(f"{out_dir}/{name}.txt"))
        print(f"{name}: {n} lines")


if __name__ == "__main__":
    main()

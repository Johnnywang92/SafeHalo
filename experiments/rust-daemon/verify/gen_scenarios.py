#!/usr/bin/env python3
"""Deterministic scenario replays covering detector branches left untested by
the main replay: VERDICT_OBJECT, VERDICT_RECOVERED, ROUTER_OFFLINE and
contaminated boot calibration (cold + warm fallback), plus stage-2 OBJECT
confirmation (regular / irregular / flat ground signal).

Writes <out_dir>/{object,recovered,offline,contaminated,contaminated_warm}.txt
and <out_dir>/object_confirm_{regular,irregular,flat}.txt
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


def emit(out, rng, ts_us, walk=0.0, spike=0.0, breath=0.8, noise=0.6, frozen=False,
         breath_hz=0.0, breath_mod_depth=0.0, breath_mod_hz=0.0,
         breath_t0_us=0, breath_phase0=0.0):
    vals = []
    for k in range(52):
        base = 24.0 + 9.0 * math.sin(k / 8.0)
        if frozen:
            amp = base
            phase = 0.3 * math.sin(k / 6.0)
        else:
            motion = walk * 6.0 * math.sin(ts_us / 140000.0 + k / 5.0)
            if breath_hz > 0.0:
                # Stage-2 场景: 可控频率/规整度的慢速呼吸包络。
                t_s = (ts_us - breath_t0_us) / 1e6
                theta = 2.0 * math.pi * breath_hz * t_s + breath_phase0
                if breath_mod_depth:
                    theta += breath_mod_depth * math.sin(2.0 * math.pi * breath_mod_hz * t_s)
                breath_term = breath * 0.8 * math.sin(theta + k / 9.0)
            else:
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


# --- Stage-2 OBJECT-confirm scenarios (flag on) -----------------------------
# The shared prefix ends with a frozen floor, which makes stage 1 return
# VERDICT_OBJECT at ~13 s (the last 150 frames hold no resolvable peak).
# The observation window then runs for 60 s over three different signals:
#   regular   -> 0.2 Hz breathing, intervals regular  -> escalate to FALL
#   irregular -> phase-jittered modulation             -> keep OBJECT
#   flat      -> no modulation at all                  -> insufficient peaks
STAGE2_TAIL_FRAMES = 2960      # ~59 s of modulation
STAGE2_FLAT_TAIL_FRAMES = 50   # flat tail, lets the 60 s window close cleanly


def _stage2_prefix(out, rng):
    ln = Line()
    ln.phase(out, rng, 260)                                   # clean calibration
    ln.phase(out, rng, 110, walk=6.0)                         # walking -> MOVING
    ln.phase(out, rng, 12, walk=6.0, spike=6.0, noise=1.5)    # impact
    ln.phase(out, rng, 300, frozen=True)                      # dead-still -> OBJECT
    return ln


def _stage2_tail(out, ln, rng, **kw):
    start = ln.ts_us
    ln.phase(out, rng, STAGE2_TAIL_FRAMES, breath=1.0,
             breath_t0_us=start, breath_phase0=math.pi, **kw)
    ln.phase(out, rng, STAGE2_FLAT_TAIL_FRAMES, frozen=True)


def gen_object_confirm_regular(out_dir):
    rng = random.Random(511)
    with open(f"{out_dir}/object_confirm_regular.txt", "w") as out:
        ln = _stage2_prefix(out, rng)
        _stage2_tail(out, ln, rng, breath_hz=0.2)


def gen_object_confirm_irregular(out_dir):
    rng = random.Random(512)
    with open(f"{out_dir}/object_confirm_irregular.txt", "w") as out:
        ln = _stage2_prefix(out, rng)
        _stage2_tail(out, ln, rng, breath_hz=0.2,
                     breath_mod_depth=2.4, breath_mod_hz=0.12)


def gen_object_confirm_flat(out_dir):
    rng = random.Random(513)
    with open(f"{out_dir}/object_confirm_flat.txt", "w") as out:
        ln = _stage2_prefix(out, rng)
        ln.phase(out, rng, STAGE2_TAIL_FRAMES + STAGE2_FLAT_TAIL_FRAMES, frozen=True)


def gen_object_confirm_abort(out_dir):
    # Router drops below the healthy rate mid-watch: the observation must be
    # abandoned (object_watch aborted) and the state goes ROUTER_OFFLINE.
    rng = random.Random(514)
    with open(f"{out_dir}/object_confirm_abort.txt", "w") as out:
        ln = _stage2_prefix(out, rng)
        start = ln.ts_us
        ln.phase(out, rng, 500, breath=1.0, breath_t0_us=start, breath_phase0=math.pi)
        ln.phase(out, rng, 24, interval_us=500_000)           # 12 s at 2 Hz (< 3 Hz)
        ln.phase(out, rng, 2800, breath=1.0, breath_t0_us=start, breath_phase0=math.pi)
        ln.phase(out, rng, 50, frozen=True)


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
    gen_object_confirm_regular(out_dir)
    gen_object_confirm_irregular(out_dir)
    gen_object_confirm_flat(out_dir)
    gen_object_confirm_abort(out_dir)
    for name in ("object", "recovered", "offline", "contaminated", "contaminated_warm",
                 "object_confirm_regular", "object_confirm_irregular", "object_confirm_flat",
                 "object_confirm_abort"):
        n = sum(1 for _ in open(f"{out_dir}/{name}.txt"))
        print(f"{name}: {n} lines")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Reference run of the PRODUCTION Python engine over the same replay input.

Composes the real src/safehalo modules (parser -> neighbor filter -> signal
filter -> extractor -> detector + auto-calibrator + targeted inspector) with a
recording logger stub and an isolated storage dir, emitting the same trace
JSONL + snapshot JSON schema as the Rust daemon. Run with TZ=UTC.

Usage: TZ=UTC python3 ref_python.py [REPLAY_FILE] [OUT_DIR]
Set WARM=1 to keep an existing baseline_model.json in OUT_DIR/storage (warm boot).
"""
import importlib
import json
import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DSP_DIR = str(REPO_ROOT / "src" / "safehalo")
REPLAY = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fh_bench/replay.txt"
OUT_DIR = sys.argv[2] if len(sys.argv) > 2 else "/tmp/fh_bench/eq/py"
HOME = "c0:06:c3:11:22:33"

os.makedirs(OUT_DIR, exist_ok=True)
storage = os.path.join(OUT_DIR, "storage")
os.makedirs(storage, exist_ok=True)
model = os.path.join(storage, "baseline_model.json")
if os.path.exists(model) and not os.environ.get("WARM"):
    os.remove(model)  # fresh cold boot, same as the Rust run

TRACE = []
CURRENT_TS = 0.0
FALL_ALARMS = 0


class RecordingLogger:
    def log_fall_suspect(self, spike_ratio, diff_energy, details=""):
        TRACE.append({"type": "impact", "ts": CURRENT_TS,
                      "spike_ratio": spike_ratio, "diff_energy": diff_energy})

    def log_inspection_progress(self, stage, details, bpm=None):
        TRACE.append({"type": "inspection", "ts": CURRENT_TS, "state": stage})

    def log_fall_confirmed(self, details, bpm, confidence):
        TRACE.append({"type": "fall_alarm", "ts": CURRENT_TS})

    def log_state_change(self, *a, **k):
        pass  # recorded via on_state_change callback instead

    def log_event(self, *a, **k):
        pass

    def log_pet_activity(self, *a, **k):
        pass

    def log_security_intrusion(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    warning = error = debug = critical = info


pkg = types.ModuleType("fhref")
pkg.__path__ = [DSP_DIR]
sys.modules["fhref"] = pkg
logger_mod = types.ModuleType("fhref.logger")
logger_mod.logger = RecordingLogger()
sys.modules["fhref.logger"] = logger_mod

csi_parser = importlib.import_module("fhref.csi_parser")
neighbor_filter = importlib.import_module("fhref.neighbor_filter")
signal_filter = importlib.import_module("fhref.signal_filter")
feature_extractor = importlib.import_module("fhref.feature_extractor")
auto_calibrator = importlib.import_module("fhref.auto_calibrator")
detector_mod = importlib.import_module("fhref.detector")

parser = csi_parser.CSIParser()
nf = neighbor_filter.NeighborSignalFilter(home_bssids=[HOME], rssi_floor=-72)
sf = signal_filter.RouterSignalFilter()
ex = feature_extractor.RouterFeatureExtractor(window_size=30)
cal = auto_calibrator.ContinuousAutoCalibrator(storage_dir=storage)


def on_state_change(old, new, feats):
    TRACE.append({
        "type": "state_change", "ts": feats.timestamp,
        "from": old.name, "to": new.name,
        "variance": round(feats.mean_variance, 6),
        "diff_energy": round(feats.diff_energy, 5),
    })


def on_fall_alarm(feats):
    global FALL_ALARMS
    FALL_ALARMS += 1


def on_burst(rate):
    TRACE.append({"type": "burst", "ts": CURRENT_TS, "rate": rate})


def on_challenge(msg):
    TRACE.append({"type": "challenge", "ts": CURRENT_TS})


def on_intercepted(details):
    TRACE.append({"type": "challenge_intercepted", "ts": CURRENT_TS})


det = detector_mod.RouterCSIDetector(
    calibration_seconds=4.0,
    fall_stillness_seconds=5.0,
    on_state_change=on_state_change,
    on_fall_alarm=on_fall_alarm,
    on_request_burst_rate=on_burst,
    on_trigger_challenge=on_challenge,
    on_challenge_intercepted=on_intercepted,
    auto_calibrator=cal,
)

packets = parse_dropped = neighbor_rejected = accepted = features_count = 0
with open(REPLAY, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        packets += 1
        pkt = parser.parse_line(line)
        if not pkt:
            parse_dropped += 1
            continue
        ts = pkt.local_timestamp / 1e6 if pkt.local_timestamp > 0 else __import__("time").time()
        res = nf.filter_packet(pkt, timestamp=ts)
        if not res.allowed:
            neighbor_rejected += 1
            continue
        accepted += 1
        clean = sf.process(pkt.amplitudes)
        feats = ex.push_frame(timestamp=ts, amplitudes=clean)
        if not feats:
            continue
        features_count += 1
        CURRENT_TS = feats.timestamp
        det.update(feats)

snapshot = {
    "final_state": det.current_state.name,
    "counters": {
        "packets": packets,
        "parse_dropped": parse_dropped,
        "neighbor_rejected": neighbor_rejected,
        "accepted": accepted,
        "features": features_count,
        "fall_alarms": FALL_ALARMS,
        "transitions": sum(1 for e in TRACE if e["type"] == "state_change"),
    },
    "base_variance": det.base_variance,
    "base_diff": det.base_diff,
    "auto_calibration": cal.get_status_summary(),
    "neighbor_filter": nf.get_spectrum_analysis(),
}

with open(os.path.join(OUT_DIR, "trace.jsonl"), "w", encoding="utf-8") as f:
    for ev in TRACE:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
with open(os.path.join(OUT_DIR, "snapshot.json"), "w", encoding="utf-8") as f:
    json.dump(snapshot, f, ensure_ascii=False, indent=2)

print(f"packets={packets} accepted={accepted} features={features_count} "
      f"final_state={det.current_state.name} fall_alarms={FALL_ALARMS} "
      f"transitions={snapshot['counters']['transitions']}")
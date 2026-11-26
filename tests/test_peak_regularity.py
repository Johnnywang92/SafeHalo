"""
Tests for P0-3: Respiration Peak Regularity & Pseudo-Periodicity Robustness.
Verifies that TargetedFallInspector accurately distinguishes between real human rhythmic
respiration and random CSI noise ripples from fallen inanimate objects.
"""

import math
import random
import pytest
from src.safehalo.targeted_inspector import (
    TargetedFallInspector,
    TargetedInspectionState,
)


def generate_still_noisy_frame(base_val=30.0, noise_std=0.08, num_subcarriers=52, rng=None):
    """Generate subcarrier amplitudes for still floor with quantization / RF noise."""
    if rng is None:
        rng = random.Random(42)
    return [base_val + rng.gauss(0, noise_std) for _ in range(num_subcarriers)]


def generate_human_breathing_frame(t, bpm=16.0, base_val=30.0, amp=0.5, noise_std=0.04, num_subcarriers=52, rng=None):
    """Generate subcarrier amplitudes for fallen human with rhythmic respiration."""
    if rng is None:
        rng = random.Random(42)
    omega = 2.0 * math.pi * (bpm / 60.0)
    breath = amp * math.sin(omega * t)
    return [base_val + breath + rng.gauss(0, noise_std) for _ in range(num_subcarriers)]


def test_legacy_vulnerability_on_noisy_object():
    """
    Demonstrates the baseline flaw in legacy mode (enable_peak_regularity=False):
    Random noise ripples easily produce 1-3 peaks > 0.000015, causing an inanimate
    fallen object to be mistakenly diagnosed as VERDICT_FALL!
    """
    inspector = TargetedFallInspector(
        inspection_duration_seconds=5.0,
        enable_peak_regularity=False,
    )
    inspector.trigger_inspection(timestamp=0.0, trigger_reason="物品掉落冲击")

    # Feed 150 frames of still floor with noise over 5 seconds (30Hz)
    rng = random.Random(1234)
    dt = 5.0 / 150.0
    res = None
    for i in range(150):
        t = (i + 1) * dt
        # Still floor has base_diff ~ 1.0, diff_energy ~ 1.05 (calm)
        amps = generate_still_noisy_frame(noise_std=0.15, rng=rng)
        res = inspector.update_frame(
            timestamp=t,
            diff_energy=1.05,
            base_diff=1.0,
            subcarrier_amplitudes=amps,
        )

    assert res is not None
    # In legacy mode, random noise ripples lead to false alarm VERDICT_FALL!
    assert res.state == TargetedInspectionState.VERDICT_FALL


def test_peak_regularity_fixes_noisy_object_to_verdict_object():
    """
    Verifies that with P0-3 enable_peak_regularity=True:
    Random noise ripples fail the regularity and interval tests, so the inanimate
    object is correctly identified as VERDICT_OBJECT!
    """
    inspector = TargetedFallInspector(
        inspection_duration_seconds=5.0,
        enable_peak_regularity=True,
        peak_regularity_cv=0.35,
    )
    inspector.trigger_inspection(timestamp=0.0, trigger_reason="物品掉落冲击")

    rng = random.Random(1234)
    dt = 5.0 / 150.0
    res = None
    for i in range(150):
        t = (i + 1) * dt
        amps = generate_still_noisy_frame(noise_std=0.15, rng=rng)
        res = inspector.update_frame(
            timestamp=t,
            diff_energy=1.05,
            base_diff=1.0,
            subcarrier_amplitudes=amps,
        )

    assert res is not None
    # Correctly eliminates false alarm and classifies as inanimate object!
    assert res.state == TargetedInspectionState.VERDICT_OBJECT
    assert res.is_inanimate_object is True


def test_real_human_rhythmic_respiration_confirmed():
    """
    Verifies that real human breathing (e.g. 16 bpm) passes regularity check
    and confirms VERDICT_FALL with accurate respiration rate.
    """
    inspector = TargetedFallInspector(
        inspection_duration_seconds=5.0,
        enable_peak_regularity=True,
        peak_regularity_cv=0.35,
    )
    inspector.trigger_inspection(timestamp=0.0, trigger_reason="老人倒地")

    rng = random.Random(5678)
    dt = 5.0 / 150.0
    res = None
    for i in range(150):
        t = (i + 1) * dt
        # Human chest movement imparts ~16 bpm oscillation
        amps = generate_human_breathing_frame(t=t, bpm=16.0, amp=0.8, noise_std=0.03, rng=rng)
        res = inspector.update_frame(
            timestamp=t,
            diff_energy=1.1,
            base_diff=1.0,
            subcarrier_amplitudes=amps,
        )

    assert res is not None
    assert res.state == TargetedInspectionState.VERDICT_FALL
    assert res.has_respiration is True
    assert 12.0 <= res.respiration_bpm <= 26.0


def test_single_isolated_noise_peak_rejected():
    """
    Verifies that a single isolated noise ripple (peaks=1, e.g. baseline wander)
    cannot be accepted as human respiration when regularity is enabled.
    """
    inspector = TargetedFallInspector(
        inspection_duration_seconds=5.0,
        enable_peak_regularity=True,
    )
    # Manually populate wave buffer with a subtle noise ripple (amplitude 0.08)
    n = 100
    inspector.respiration_wave_buffer = [
        30.0 + (0.08 * math.exp(-((i - 50) ** 2) / 30.0)) for i in range(n)
    ]
    has_resp, bpm = inspector._evaluate_ground_respiration()
    assert has_resp is False
    assert bpm == 0.0

    # Whereas in legacy mode (flag=False), tiny noise ripple (amplitude 0.08)
    # easily crosses 0.000015 and mistakenly claims human respiration:
    inspector.enable_peak_regularity = False
    has_resp_legacy, bpm_legacy = inspector._evaluate_ground_respiration()
    assert has_resp_legacy is True
    assert bpm_legacy > 0.0


def test_irregular_peaks_rejected_by_cv():
    """
    Verifies that multiple peaks with irregular spacing (CV > 0.35)
    are rejected as noise under enable_peak_regularity=True.
    """
    inspector = TargetedFallInspector(
        inspection_duration_seconds=6.0,
        enable_peak_regularity=True,
        peak_regularity_cv=0.35,
    )
    # 150 frames with 3 peaks at indices 20, 50, 140 (intervals 30, 90 -> highly irregular, CV > 0.5)
    wave = [30.0] * 150
    for p in [20, 50, 140]:
        for di in range(-4, 5):
            if 0 <= p + di < 150:
                wave[p + di] += 0.8 * (1.0 - abs(di) / 5.0)

    inspector.respiration_wave_buffer = wave
    has_resp, _ = inspector._evaluate_ground_respiration()
    assert has_resp is False


def test_100hz_burst_rate_respiration_confirmed():
    """
    Verifies that at 100Hz burst rate (500 frames over 5s), the time-aware window
    retains full respiration cycles and confirms VERDICT_FALL accurately.
    """
    inspector = TargetedFallInspector(
        inspection_duration_seconds=5.0,
        enable_peak_regularity=True,
        peak_regularity_cv=0.35,
    )
    inspector.trigger_inspection(timestamp=0.0, trigger_reason="100Hz提频测试")

    rng = random.Random(2026)
    dt = 0.01  # 100Hz
    res = None
    for i in range(500):
        t = (i + 1) * dt
        amps = generate_human_breathing_frame(t=t, bpm=18.0, amp=0.8, noise_std=0.02, rng=rng)
        res = inspector.update_frame(
            timestamp=t,
            diff_energy=1.1,
            base_diff=1.0,
            subcarrier_amplitudes=amps,
        )

    assert res is not None
    assert res.state == TargetedInspectionState.VERDICT_FALL
    assert res.has_respiration is True
    assert 12.0 <= res.respiration_bpm <= 26.0

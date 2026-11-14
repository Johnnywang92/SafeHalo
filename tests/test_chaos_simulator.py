"""
Tests for Chaos & Fault Injection Simulator (scripts/chaos_simulator.py).
Ensures that chaos injection scenarios can be automatically invoked and validated
within pytest CI/CD pipelines.
"""

import socket
import pytest
from scripts.chaos_simulator import ChaosSimulator


def is_loopback_permitted() -> bool:
    """Test whether sandbox allows loopback TCP connect."""
    try:
        import http.server
        import threading
        server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        port = server.server_port
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
        server.shutdown()
        return True
    except Exception:
        return False


def test_chaos_corrupt_stream_rust():
    sim = ChaosSimulator(target="rust")
    res = sim.run_corrupt_stream(total_packets=300, corrupt_rate=0.30)
    assert res.passed is True
    assert res.metrics.get("corrupt", 0) > 0


def test_chaos_corrupt_stream_python():
    sim = ChaosSimulator(target="python")
    res = sim.run_corrupt_stream(total_packets=300, corrupt_rate=0.30)
    assert res.passed is True
    assert res.metrics.get("corrupt", 0) > 0


def test_chaos_jitter_and_loss_rust():
    sim = ChaosSimulator(target="rust")
    res = sim.run_jitter_and_loss(total_packets=400, loss_rate=0.35)
    assert res.passed is True
    assert res.metrics.get("delivered_packets", 0) > 0


def test_chaos_jitter_and_loss_python():
    sim = ChaosSimulator(target="python")
    res = sim.run_jitter_and_loss(total_packets=400, loss_rate=0.35)
    assert res.passed is True


def test_chaos_serial_hot_unplug_rust():
    sim = ChaosSimulator(target="rust")
    res = sim.run_serial_hot_unplug()
    assert res.passed is True


def test_chaos_serial_hot_unplug_python():
    sim = ChaosSimulator(target="python")
    res = sim.run_serial_hot_unplug()
    assert res.passed is True


def test_chaos_notify_outage_rust():
    if not is_loopback_permitted():
        pytest.skip("Loopback TCP connection not permitted in current sandbox environment")
    sim = ChaosSimulator(target="rust")
    res = sim.run_notify_outage()
    assert res.passed is True


def test_chaos_notify_outage_python():
    if not is_loopback_permitted():
        pytest.skip("Loopback TCP connection not permitted in current sandbox environment")
    sim = ChaosSimulator(target="python")
    res = sim.run_notify_outage()
    assert res.passed is True

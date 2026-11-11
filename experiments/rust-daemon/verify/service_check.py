#!/usr/bin/env python3
"""Standalone-service verification for the Rust daemon.

Covers the service layer added on top of the deterministic detection pipeline:
static console + session/RBAC auth, SSE event push, placement persistence,
incident storage + feedback, notification fan-out (webhook/HA/SMS/mini),
serial actuation (BURST_RATE / CHALLENGE_BEEP), and serial self-healing.

All state lives under a temp directory; the repository's real data/ and
logs/ are never touched.

Usage: python3 verify/service_check.py [WORK_DIR]
"""
import json
import os
import pty
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DAEMON = ROOT / "target" / "release" / "safehalo-daemon"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + ((" - " + detail) if detail else ""))
    return ok


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def request(method, url, data=None, headers=None, timeout=5):
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode()), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), e.headers
        except Exception:
            return e.code, raw.decode(), e.headers


def wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


class Sink:
    def __init__(self):
        self.items = []
        self.lock = threading.Lock()
        self.srv = HTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def _handler(self):
        sink = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(n)
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {"_raw": raw.decode("utf-8", "replace")}
                with sink.lock:
                    sink.items.append((self.path, self.headers.get("Authorization", ""), payload))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        return H

    def paths(self):
        with self.lock:
            return sorted(p for p, _, _ in self.items)

    def find(self, path):
        with self.lock:
            for p, auth, payload in self.items:
                if p == path:
                    return auth, payload
        return None, None

    def close(self):
        self.srv.shutdown()


def launch(args):
    return subprocess.Popen([str(DAEMON)] + args,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_http(work):
    sink = Sink()
    store = work / "http_store"
    data = work / "http_data"
    port = free_port()
    udp = free_port()
    proc = launch([
        "--udp", f"127.0.0.1:{udp}",
        "--http", f"127.0.0.1:{port}",
        "--storage-dir", str(store),
        "--data-dir", str(data),
        "--notify-webhook", f"http://127.0.0.1:{sink.port}/hook",
        "--run-secs", "60",
    ])
    base = f"http://127.0.0.1:{port}"
    try:
        ok = check("http service starts", wait_port(port))
        if not ok:
            return

        status, body, _ = request("GET", base + "/")
        check("static console served", status == 200 and "SafeHalo" in body)

        status, body, _ = request("GET", base + "/api/ha/status")
        check("public HA status", status == 200 and body.get("human_state") == "CALIBRATING")

        status, _, _ = request("GET", base + "/api/state")
        check("protected endpoint rejects anonymous", status == 401)

        status, body, _ = request("POST", base + "/api/auth/login",
                                  {"username": "admin", "password": "admin123"})
        token = body.get("token", "") if isinstance(body, dict) else ""
        check("admin login", status == 200 and body.get("success") is True and len(token) == 64)
        auth = {"Authorization": "Bearer " + token}

        status, body, _ = request("GET", base + "/api/status", headers=auth)
        keys = body if isinstance(body, dict) else {}
        check("console status contract",
              status == 200 and {"state", "placement", "routine", "recent_events"} <= set(keys))

        status, body, _ = request("GET", base + "/api/users",
                                  headers={"Cookie": "safehalo_session=" + token})
        names = [u["username"] for u in body.get("users", [])] if isinstance(body, dict) else []
        check("cookie session + user list", status == 200 and names == ["admin"])

        status, body, _ = request("POST", base + "/api/placement/save", {
            "room_type": "BEDROOM", "room_name": "卧室", "mount_height": "HIGH_WALL",
            "room_area_sqm": 18, "has_pets": True, "care_nickname": "奶奶",
            "living_arrangement": "SOLO", "mobility_pace": "STEADY_SLOW",
            "wake_habit": "EARLY", "enable_gentle_challenge": False,
        })
        saved = (store / "placement_config.json").exists()
        check("placement save persists", status == 200 and body.get("success") is True and saved)
        check("placement room visible", request("GET", base + "/api/placement")[1]
              .get("config", {}).get("room_name") == "卧室")

        status, _, _ = request("POST", base + "/api/users/create", {
            "username": "family", "password": "secret123", "role": "family",
            "display_name": "家属",
        }, headers=auth)
        check("admin creates family user", status == 200)

        status, body, _ = request("POST", base + "/api/auth/login",
                                  {"username": "family", "password": "secret123"})
        fam = {"Authorization": "Bearer " + body.get("token", "")} if isinstance(body, dict) else {}
        check("family login", status == 200 and bool(fam))
        check("family cannot list users", request("GET", base + "/api/users", headers=fam)[0] == 403)
        check("family cannot push test alarm",
              request("POST", base + "/api/test_push", {}, headers=fam)[0] == 403)

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/events/stream?since=0", headers=auth)
        resp = conn.getresponse()
        check("SSE stream opens", resp.status == 200)

        status, body, _ = request("POST", base + "/api/trigger_inspect", {}, headers=auth)
        check("manual inspection accepted", status == 200 and body.get("success") is True)
        seen = b""
        deadline = time.time() + 5
        while time.time() < deadline and b"INSPECTION" not in seen:
            seen += resp.fp.readline()
        check("SSE pushes inspection event", b"INSPECTION" in seen)
        conn.close()

        status, body, _ = request("GET", base + "/api/events?since=0", headers=auth)
        check("event backfill", status == 200 and body.get("latest_seq", 0) >= 2)

        status, body, _ = request("POST", base + "/api/test_push", {}, headers=auth)
        time.sleep(0.6)
        auth_hdr, payload = sink.find("/hook")
        check("test alarm fan-out", status == 200 and payload is not None
              and "markdown" in payload)
    finally:
        stop(proc)
        sink.close()


def alarm_replay(work):
    replay = work / "replay.txt"
    if not replay.exists():
        subprocess.run([sys.executable, str(HERE / "gen_replay.py"), str(replay)],
                       check=True, stdout=subprocess.DEVNULL)
    sink = Sink()
    store = work / "alarm_store"
    proc = launch([
        "--replay", str(replay), "--no-http", "--storage-dir", str(store),
        "--notify-webhook", f"http://127.0.0.1:{sink.port}/hook",
        "--ha-webhook", f"http://127.0.0.1:{sink.port}/ha",
        "--sms-webhook", f"http://127.0.0.1:{sink.port}/sms",
        "--miniprogram-url", f"http://127.0.0.1:{sink.port}/mini",
        "--miniprogram-token", "mtok",
    ])
    try:
        out, err = proc.communicate(timeout=180)
        final = (out.strip().splitlines() or [""])[-1]
        check("replay reaches a confirmed fall", "fall_alarms=1" in final, final)
        check("four notification channels",
              sink.paths() == ["/ha", "/hook", "/mini", "/sms"], str(sink.paths()))
        mini_auth, mini = sink.find("/mini")
        check("mini-program payload", mini is not None
              and mini.get("event_type") == "FALL_ALARM"
              and mini.get("household_id") == "H01")
        check("mini-program bearer token", mini_auth == "Bearer mtok", mini_auth)
        _, ha = sink.find("/ha")
        check("HA webhook event", ha is not None and ha.get("event") == "fall_alarm")
        _, sms = sink.find("/sms")
        check("SMS webhook priority", sms is not None and sms.get("priority") == "CRITICAL")

        inc = store / "incidents.jsonl"
        lines = [l for l in inc.read_text(encoding="utf-8").splitlines() if l.strip()] if inc.exists() else []
        rec = json.loads(lines[-1]) if lines else {}
        check("incident persisted", len(lines) == 1 and rec.get("event_type") == "FALL_ALARM")
        check("incident marked pushed", rec.get("miniprogram_pushed") is True)
        check("event log written", (store / "events.jsonl").exists()
              and "FALL_ALARM" in (store / "events.jsonl").read_text(encoding="utf-8"))
    finally:
        stop(proc)
        sink.close()


def read_pty(master, proc, deadline):
    buf = b""
    while time.time() < deadline and proc.poll() is None:
        r, _, _ = select.select([master], [], [], 0.1)
        if r:
            try:
                buf += os.read(master, 65536)
            except OSError:
                break
    return buf.decode("utf-8", "replace")


def serial_actuation(work, scenarios):
    lines = [l for l in open(scenarios / "object.txt", encoding="utf-8") if l.strip()]
    for enabled, want in ((True, True), (False, False)):
        master, slave = pty.openpty()
        store = work / ("serial_on" if enabled else "serial_off")
        proc = launch([
            "--serial", os.ttyname(slave), "--baud", "921600", "--no-http",
            "--storage-dir", str(store), "--max-packets", str(len(lines)),
            "--run-secs", "20", "--challenge-enabled", "1" if enabled else "0",
        ])
        time.sleep(0.6)
        try:
            for line in lines:
                os.write(master, line.encode())
            text = read_pty(master, proc, time.time() + 15)
            proc.wait(timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            stop(proc)
        finally:
            os.close(master)
            os.close(slave)
        label = f"serial actuation (challenge {'on' if enabled else 'off'})"
        ok = "BURST_RATE:100" in text and "BURST_RATE:30" in text
        ok = ok and (("CHALLENGE_BEEP:1" in text) == want)
        check(label, ok, repr([l for l in text.splitlines()
                               if l.startswith(("BURST_RATE", "CHALLENGE_BEEP"))]))


def serial_reconnect(work, scenarios):
    lines = [l for l in open(scenarios / "object.txt", encoding="utf-8") if l.strip()]
    master, slave = pty.openpty()
    port = free_port()
    store = work / "reconnect"
    proc = launch([
        "--serial", os.ttyname(slave), "--baud", "921600",
        "--http", f"127.0.0.1:{port}", "--data-dir", str(store / "data"),
        "--storage-dir", str(store), "--run-secs", "12",
    ])
    try:
        wait_port(port)
        for line in lines:
            os.write(master, line.encode())
        time.sleep(0.4)
        before = request("GET", f"http://127.0.0.1:{port}/api/ha/status")[1].get("human_state")
        os.close(master)
        os.close(slave)
        time.sleep(3.2)
        after = request("GET", f"http://127.0.0.1:{port}/api/ha/status")[1].get("human_state")
        check("serial input accepted", before in ("CALIBRATING", "EMPTY", "STATIC", "MOVING"), before)
        check("serial loss detected", after == "ROUTER_OFFLINE", after)
    finally:
        stop(proc)


def main():
    if not DAEMON.exists():
        print(f"error: missing {DAEMON} - run: cargo build --release", file=sys.stderr)
        return 1
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="safehalo-svc-"))
    work.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TZ", "UTC")
    scenarios = work / "scenarios"
    scenarios.mkdir(exist_ok=True)
    subprocess.run([sys.executable, str(HERE / "gen_scenarios.py"), str(scenarios)],
                   check=True, stdout=subprocess.DEVNULL)

    print(f"work dir: {work}")
    test_http(work)
    alarm_replay(work)
    serial_actuation(work, scenarios)
    serial_reconnect(work, scenarios)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    if len(sys.argv) <= 1:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

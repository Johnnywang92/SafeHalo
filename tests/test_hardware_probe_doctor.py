import os
import select
import threading
import time
import unittest
from scripts.hardware_probe_doctor import parse_csi_line, ProbeDoctor


class TestHardwareProbeDoctor(unittest.TestCase):
    def test_parse_csi_line_valid(self):
        line = (
            "CSI_DATA,0,123,c0:06:c3:11:22:33,-55,11,1,7,1,1,0,0,0,0,1,-95,0,6,0,1700000000,0,104,0,104,0,"
            "[" + ", ".join(str(i % 20) for i in range(104)) + "]\n"
        )
        parsed = parse_csi_line(line)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["mac"], "c0:06:c3:11:22:33")
        self.assertEqual(parsed["rssi"], -55)
        self.assertEqual(parsed["channel"], 6)
        self.assertEqual(parsed["subcarriers"], 52)

    def test_parse_csi_line_invalid(self):
        self.assertIsNone(parse_csi_line("LOG_INFO,system boot complete\n"))
        self.assertIsNone(parse_csi_line("CSI_DATA,incomplete,line\n"))
        self.assertIsNone(parse_csi_line(""))

    def test_probe_doctor_virtual_pipe(self):
        uplink_r, uplink_w = os.pipe()
        downlink_r, downlink_w = os.pipe()
        stop_evt = threading.Event()

        def virtual_probe():
            step = 0
            while not stop_evt.is_set():
                step += 1
                line = (
                    f"CSI_DATA,0,{step},c0:06:c3:11:22:33,-50,11,1,7,1,1,0,0,0,0,1,-95,0,6,0,1700000000,0,104,0,104,0,"
                    f"[{', '.join(str(i % 15) for i in range(104))}]\n"
                )
                try:
                    os.write(uplink_w, line.encode("utf-8"))
                except OSError:
                    break
                time.sleep(0.033)

        t = threading.Thread(target=virtual_probe, daemon=True)
        t.start()

        try:
            doctor = ProbeDoctor(
                port="test_pipe",
                read_fd=uplink_r,
                write_fd=downlink_w,
                expected_mac="c0:06:c3:11:22:33",
                duration=1.5,
            )
            report = doctor.diagnose()
            self.assertIn(report["verdict"], ("PASS", "WARN"))
            self.assertEqual(report["subcarriers"], 52)
            self.assertTrue(report["ht20_ok"])
            self.assertTrue(report["mac_ok"])
        finally:
            stop_evt.set()
            t.join(timeout=1)
            for fd in (uplink_r, uplink_w, downlink_r, downlink_w):
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()

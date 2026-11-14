import os
import unittest
from scripts.soak_test import CSISimulator, calculate_slope, ProcessMonitor


class TestSoakTestSuite(unittest.TestCase):
    def test_csi_simulator_line_format(self):
        sim = CSISimulator(home_mac="aa:bb:cc:11:22:33")
        line = sim.next_line("walk")
        self.assertTrue(line.startswith("CSI_DATA,"))
        self.assertIn("aa:bb:cc:11:22:33", line)
        self.assertTrue(line.endswith("]\n"))

        # Verify parsing of subcarrier array
        subcarrier_part = line[line.index("[") + 1 : line.index("]")]
        vals = [int(v.strip()) for v in subcarrier_part.split(",")]
        # 52 subcarriers * 2 (I and Q) = 104 values
        self.assertEqual(len(vals), 104)

    def test_calculate_slope(self):
        # Perfectly flat
        x = [1.0, 2.0, 3.0, 4.0]
        y_flat = [10.0, 10.0, 10.0, 10.0]
        self.assertAlmostEqual(calculate_slope(x, y_flat), 0.0)

        # Rising slope: +2 units per second
        y_rising = [2.0, 4.0, 6.0, 8.0]
        self.assertAlmostEqual(calculate_slope(x, y_rising), 2.0)

        # Falling slope: -1.5 units per second
        y_falling = [10.0, 8.5, 7.0, 5.5]
        self.assertAlmostEqual(calculate_slope(x, y_falling), -1.5)

    def test_process_monitor_self(self):
        monitor = ProcessMonitor(pid=os.getpid(), storage_dir="/tmp")
        rss_kb = monitor.get_rss_kb()
        self.assertGreater(rss_kb, 1000)  # Python runtime uses > 1MB RSS
        fd_count = monitor.get_fd_count()
        self.assertGreaterEqual(fd_count, 3)  # stdin, stdout, stderr at minimum


if __name__ == "__main__":
    unittest.main()

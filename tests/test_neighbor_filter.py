"""
SafeHalo 邻居 Wi-Fi 信号干扰隔离与空间排他算法单元测试 (test_neighbor_filter.py)
"""

import unittest
from src.safehalo.csi_parser import CSIPacket
from src.safehalo.neighbor_filter import NeighborSignalFilter, FilterResult


class TestNeighborSignalFilter(unittest.TestCase):
    def setUp(self):
        # 设定主路由 MAC 为 c0:06:c3:11:22:33，RSSI 门限为 -72 dBm
        self.filter = NeighborSignalFilter(
            home_bssids=["c0:06:c3:11:22:33"],
            rssi_floor=-72,
            spectral_tilt_thresh=0.45,
        )

    def test_home_router_packet_allowed(self):
        """测试室内主路由正常包放行"""
        pkt = CSIPacket(
            mac="c0:06:c3:11:22:33",
            rssi=-48,
            amplitudes=[10.0] * 64,  # 平坦无衰减
        )
        res = self.filter.filter_packet(pkt)
        self.assertTrue(res.allowed)
        self.assertEqual(res.reason, "MATCH_HOME_NETWORK")
        self.assertFalse(res.is_neighbor_packet)

    def test_neighbor_bssid_rejection(self):
        """测试隔壁邻居路由器同频报文隔离拦截"""
        pkt = CSIPacket(
            mac="34:ce:00:88:99:aa",  # 隔壁 TP-Link / 小米路由器
            rssi=-58,
            amplitudes=[10.0] * 64,
        )
        res = self.filter.filter_packet(pkt)
        self.assertFalse(res.allowed)
        self.assertTrue(res.is_neighbor_packet)
        self.assertIn("REJECT_NEIGHBOR_BSSID", res.reason)

        # 验证已将邻居收录至邻区统计表
        analysis = self.filter.get_spectrum_analysis()
        self.assertEqual(analysis["neighbor_nodes_detected"], 1)
        self.assertEqual(analysis["top_interfering_neighbors"][0]["bssid"], "34:ce:00:88:99:aa")

    def test_random_mac_probe_suppression(self):
        """测试楼道行人手机随机 MAC (Locally Administered Bit) 探针拦截"""
        # "da:a1:19:22:33:44": 第一个字节 0xda (二进制 11011010，次低位为 1，是标准随机 MAC)
        pkt = CSIPacket(
            mac="da:a1:19:22:33:44",
            rssi=-50,
            amplitudes=[10.0] * 64,
        )
        self.assertTrue(NeighborSignalFilter.is_random_mac("da:a1:19:22:33:44"))
        res = self.filter.filter_packet(pkt)
        self.assertFalse(res.allowed)
        self.assertIn("RANDOM_PROBE_MAC", res.reason)

    def test_weak_through_wall_rssi_floor(self):
        """测试穿墙衰减严重的邻居微弱信号拦截 (低于 -72 dBm)"""
        pkt = CSIPacket(
            mac="c0:06:c3:11:22:33",  # 即使伪造 MAC
            rssi=-84,                 # 经承重墙衰减后的微弱信号
            amplitudes=[10.0] * 64,
        )
        res = self.filter.filter_packet(pkt)
        self.assertFalse(res.allowed)
        self.assertIn("REJECT_WEAK_RSSI", res.reason)

    def test_party_wall_spectral_tilt_absorption(self):
        """测试钢筋混凝土分户承重墙高频剧烈吸收指纹拦截"""
        # 低频子载波 (0..31) 能量很高 (20.0)，高频端 (32..63) 经历实心墙吸收后急剧降至 (3.0)
        # tilt = 32 * 9 / (32 * 400) = 0.0225 << 0.45
        amplitudes = [20.0] * 32 + [3.0] * 32
        pkt = CSIPacket(
            mac="c0:06:c3:11:22:33",
            rssi=-68,  # 在边界弱信号区
            amplitudes=amplitudes,
        )
        res = self.filter.filter_packet(pkt)
        self.assertFalse(res.allowed)
        self.assertEqual(res.reason, "REJECT_THROUGH_WALL_MULTIPATH")

    def test_auto_learning_home_network(self):
        """测试未配置 MAC 时的冷启动家网自适应锁定"""
        auto_filter = NeighborSignalFilter(home_bssids=None, auto_bind_sample_count=10)
        self.assertTrue(auto_filter.auto_learning)

        # 模拟前 10 个包中，大部分为家网强信号 "aa:bb:cc:dd:ee:ff" (-45dBm)
        for _ in range(8):
            auto_filter.filter_packet(CSIPacket(mac="aa:bb:cc:dd:ee:ff", rssi=-45, amplitudes=[10.0]*64))
        # 夹杂 2 个邻居弱包 "11:22:33:44:55:66" (-78dBm)
        for _ in range(2):
            auto_filter.filter_packet(CSIPacket(mac="11:22:33:44:55:66", rssi=-78, amplitudes=[10.0]*64))

        # 自学习完成，锁定主路由为 aa:bb:cc:dd:ee:ff
        self.assertFalse(auto_filter.auto_learning)
        self.assertIn("aa:bb:cc:dd:ee:ff", auto_filter.home_bssids)

        # 随后邻居包即被拦截
        res = auto_filter.filter_packet(CSIPacket(mac="11:22:33:44:55:66", rssi=-78, amplitudes=[10.0]*64))
        self.assertFalse(res.allowed)


if __name__ == "__main__":
    unittest.main()

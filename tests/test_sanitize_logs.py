import os
import shutil
import tempfile
import unittest
from scripts.sanitize_logs import LogSanitizer


class TestLogSanitizer(unittest.TestCase):
    def setUp(self):
        self.sanitizer = LogSanitizer(salt="TestSalt2026", care_nickname="王奶奶")
        self.tmp = tempfile.mkdtemp(prefix="safehalo-sanitize-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mac_pseudonymization_and_consistency(self):
        mac1 = "c0:06:c3:11:22:33"
        mac2 = "c0-06-c3-11-22-33"
        mac3 = "aa:bb:cc:dd:ee:ff"

        pseudo1 = self.sanitizer.pseudonymize_mac(mac1)
        pseudo2 = self.sanitizer.pseudonymize_mac(mac2)
        pseudo3 = self.sanitizer.pseudonymize_mac(mac3)

        self.assertTrue(pseudo1.startswith("[MAC:"))
        # Consistency: colon vs dash with same address yield same pseudonym
        self.assertEqual(pseudo1, pseudo2)
        # Distinct addresses yield distinct pseudonyms
        self.assertNotEqual(pseudo1, pseudo3)

    def test_text_sanitization(self):
        raw_text = (
            "绑定主路由 c0:06:c3:11:22:33，紧急联系人手机 13812345678。\n"
            "正在监护王奶奶，推送目标 https://sctapi.ftqq.com/SCT1234567Key.send。\n"
            "Bark 推送地址: https://api.day.app/my_bark_secret_key。\n"
        )
        sanitized = self.sanitizer.sanitize_text(raw_text)

        self.assertNotIn("c0:06:c3:11:22:33", sanitized)
        self.assertIn("[MAC:", sanitized)
        self.assertNotIn("13812345678", sanitized)
        self.assertIn("138****5678", sanitized)
        self.assertNotIn("王奶奶", sanitized)
        self.assertIn("[长辈]", sanitized)
        self.assertNotIn("SCT1234567Key", sanitized)
        self.assertIn("[REDACTED_SERVERCHAN_KEY]", sanitized)
        self.assertNotIn("my_bark_secret_key", sanitized)
        self.assertIn("[REDACTED_BARK_KEY]", sanitized)

    def test_json_and_jsonl_sanitization(self):
        cfg_file = os.path.join(self.tmp, "notifier_config.json")
        out_cfg_file = os.path.join(self.tmp, "sanitized_config.json")

        sample_json = {
            "pushplus_token": "token_abc_123",
            "serverchan_key": "sct_key_456",
            "bark_key": "bark_secret_789",
            "password": "my_admin_pass",
            "household_id": "H01",
            "safe_url": "http://192.168.1.1:8091",
        }
        with open(cfg_file, "w", encoding="utf-8") as f:
            import json
            json.dump(sample_json, f)

        self.sanitizer.sanitize_file(cfg_file, out_cfg_file)

        with open(out_cfg_file, "r", encoding="utf-8") as f:
            import json
            res = json.load(f)

        self.assertEqual(res["pushplus_token"], "[REDACTED_PUSHPLUS_TOKEN]")
        self.assertEqual(res["serverchan_key"], "[REDACTED_SERVERCHAN_KEY]")
        self.assertEqual(res["bark_key"], "[REDACTED_BARK_KEY]")
        self.assertEqual(res["password"], "[REDACTED_PASSWORD]")
        self.assertEqual(res["household_id"], "H01")

        # Run compliance check
        issues = self.sanitizer.check_file_compliance(out_cfg_file)
        self.assertEqual(len(issues), 0)

        # Unsanitized file should fail compliance check
        raw_issues = self.sanitizer.check_file_compliance(cfg_file)
        self.assertGreater(len(raw_issues), 0)


if __name__ == "__main__":
    unittest.main()

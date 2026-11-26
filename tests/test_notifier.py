import unittest
from unittest.mock import MagicMock
from src.safehalo.notifier import AlertNotifier


class TestNotifier(unittest.TestCase):
    def test_send_fall_alarm_payload_and_channels(self):
        notifier = AlertNotifier(
            pushplus_token="mock_pushplus",
            serverchan_key="mock_serverchan",
            webhook_url="http://mock.webhook/hook",
            ha_webhook_url="http://mock.ha/webhook",
            sms_webhook_url="http://mock.sms/gateway",
            bark_key="mock_bark_device_key",
        )

        notifier._http_post_json = MagicMock(return_value=True)

        ok = notifier.send_fall_alarm(
            room_name="主卧卫生间",
            confidence="95%",
            details="剧烈冲击倒地静止",
            bpm=16.0,
            event_id="evt_test_123",
            household_id="H01",
        )
        self.assertTrue(ok)
        # 6 channels called
        self.assertEqual(notifier._http_post_json.call_count, 6)

        # Inspect HA payload
        ha_call = [c for c in notifier._http_post_json.call_args_list if c[0][0] == "http://mock.ha/webhook"][0]
        ha_payload = ha_call[0][1]
        self.assertEqual(ha_payload["event"], "fall_alarm")
        self.assertEqual(ha_payload["room"], "主卧卫生间")
        self.assertEqual(ha_payload["confidence"], "95%")
        self.assertEqual(ha_payload["respiration_bpm"], 16.0)
        self.assertEqual(ha_payload["event_id"], "evt_test_123")
        self.assertEqual(ha_payload["household_id"], "H01")

        # Inspect SMS payload
        sms_call = [c for c in notifier._http_post_json.call_args_list if c[0][0] == "http://mock.sms/gateway"][0]
        sms_payload = sms_call[0][1]
        self.assertEqual(sms_payload["priority"], "CRITICAL")
        self.assertEqual(sms_payload["respiration_bpm"], 16.0)
        self.assertEqual(sms_payload["event_id"], "evt_test_123")
        self.assertEqual(sms_payload["household_id"], "H01")

        # Inspect Bark payload (iOS critical alarm breakthrough)
        bark_call = [c for c in notifier._http_post_json.call_args_list if "api.day.app/mock_bark_device_key" in c[0][0]][0]
        bark_payload = bark_call[0][1]
        self.assertEqual(bark_payload["level"], "critical")
        self.assertEqual(bark_payload["volume"], 10)
        self.assertEqual(bark_payload["sound"], "alarm")
        self.assertEqual(bark_payload["group"], "SafeHalo")
        self.assertIn("主卧卫生间", bark_payload["body"])
        self.assertIn("16 bpm", bark_payload["body"])

    def test_custom_security_notification_ha_payload(self):
        notifier = AlertNotifier(ha_webhook_url="http://mock.ha/webhook")
        notifier._http_post_json = MagicMock(return_value=True)

        ok = notifier.send_custom_notification(
            title="【SafeHalo】防走失警报",
            content="深夜外门已推开",
            room_name="入户玄关",
            ha_event="curfew_wandering",
            event_id="evt_security_1",
            household_id="H02",
        )

        self.assertTrue(ok)
        url, payload = notifier._http_post_json.call_args[0]
        self.assertEqual(url, "http://mock.ha/webhook")
        self.assertEqual(payload["event"], "curfew_wandering")
        self.assertEqual(payload["room"], "入户玄关")
        self.assertEqual(payload["event_id"], "evt_security_1")
        self.assertEqual(payload["household_id"], "H02")

    def test_custom_notification_queue_uses_event_id(self):
        notifier = AlertNotifier()
        notifier.start_async_worker = MagicMock()

        notifier.enqueue_custom_notification(
            title="安防入侵警报",
            content="门磁触发",
            event_id="evt_security_2",
        )

        task = notifier._async_queue.get_nowait()
        self.assertEqual(task["id"], "evt_security_2")
        self.assertEqual(task["kind"], "custom")

    def test_deduplication(self):
        notifier = AlertNotifier(
            pushplus_token="mock_pushplus",
        )
        notifier._http_post_json = MagicMock(return_value=True)

        # First call
        ok1 = notifier.send_fall_alarm(room_name="客厅", event_id="evt_first")
        self.assertTrue(ok1)
        self.assertEqual(notifier._http_post_json.call_count, 1)

        # Immediate second call in same room should be deduplicated
        ok2 = notifier.send_fall_alarm(room_name="客厅", event_id="evt_second")
        self.assertTrue(ok2)
        # Call count remains 1 because of deduplication
        self.assertEqual(notifier._http_post_json.call_count, 1)

        # Manual test event should bypass deduplication
        ok3 = notifier.send_fall_alarm(room_name="客厅", event_id="evt_test")
        self.assertTrue(ok3)
        self.assertEqual(notifier._http_post_json.call_count, 2)


if __name__ == "__main__":
    unittest.main()

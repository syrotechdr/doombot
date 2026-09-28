import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import monitor as m


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(m.Config(), notifier="callmebot", data_dir=Path(self.temp.name))
        self.state = m.load_state(self.config)
        self.reader = Mock()
        self.sender = Mock()

    def snapshot(self, **changes):
        value = dict(title=self.config.title, cinema=self.config.cinema,
                     section=True, loading=False, text="Nothing Scheduled", buttons=[])
        value.update(changes)
        return value

    def cycle(self, status="available", now=1000):
        self.reader.read.return_value = m.Observation(status, "6:40 PM")
        return m.run_cycle(self.config, self.state, self.reader, self.sender, now)

    def test_actual_empty_message(self):
        self.assertEqual(m.classify(self.snapshot(text="Nothing\n Scheduled"), self.config).status, "unavailable")

    def test_purchase_and_scheduled_are_distinct(self):
        for enabled, status in [(True, "available"), (False, "scheduled")]:
            with self.subTest(enabled=enabled):
                result = m.classify(self.snapshot(text="6:40 PM", buttons=[{"enabled": enabled}]), self.config)
                self.assertEqual(result.status, status)

    def test_errors_loading_and_wrong_identity_never_signal_sale(self):
        cases = [dict(title="Access denied"), dict(cinema="Otro cine"),
                 dict(section=False), dict(loading=True), dict(text=""),
                 dict(text="Buy Tickets"), dict(text="Cloudflare error"),
                 dict(buttons=[{"enabled": True}])]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(m.classify(self.snapshot(**changes), self.config).status, "unknown")

    def test_requires_consecutive_confirmations(self):
        self.cycle()
        self.sender.assert_not_called()
        self.cycle("unavailable")
        self.cycle()
        self.sender.assert_not_called()
        self.cycle()
        self.sender.assert_called_once()

    def test_network_failure_resets_confirmations(self):
        self.cycle()
        self.reader.read.side_effect = TimeoutError()
        self.assertFalse(m.run_cycle(self.config, self.state, self.reader, self.sender))
        self.reader.read.side_effect = None
        self.cycle()
        self.sender.assert_not_called()
        self.cycle()
        self.sender.assert_called_once()

    def test_sent_state_survives_restart_without_duplicate(self):
        self.cycle()
        self.cycle()
        self.state = m.load_state(self.config)
        for status in ["available", "available", "unavailable", "available", "available"]:
            self.cycle(status)
        self.sender.assert_called_once()

    def test_scheduled_then_available_sends_two_distinct_alerts(self):
        for status in ["scheduled", "scheduled", "available", "available", "scheduled", "scheduled"]:
            self.cycle(status)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.state["sent"], ["scheduled", "available"])

    def test_retry_waits_and_does_not_mark_failed_delivery_sent(self):
        self.sender.side_effect = RuntimeError("temporary failure")
        self.cycle(now=1000)
        self.assertFalse(self.cycle(now=1060))
        self.assertEqual(self.state["sent"], [])
        self.sender.side_effect = None
        self.cycle(now=1120)
        self.assertEqual(self.sender.call_count, 1)
        self.cycle(now=1360)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.state["sent"], ["available"])

    def test_restart_discards_partial_confirmations(self):
        self.cycle()
        self.state = m.load_state(self.config)
        self.cycle()
        self.sender.assert_not_called()

    def test_once_resumes_recent_confirmations_but_discards_stale_ones(self):
        self.cycle(now=1000)
        self.assertEqual(m.load_state(self.config, resume_candidate=True, now=1060)["count"], 1)
        self.assertEqual(m.load_state(self.config, resume_candidate=True, now=2000)["count"], 0)

    def test_once_exits_after_one_cycle_and_closes_browser(self):
        for successful in [True, False]:
            with self.subTest(successful=successful), patch("sys.argv", ["monitor.py", "--once"]), \
                 patch.object(m.Config, "from_env", return_value=self.config), \
                 patch("monitor.validate_notifier"), patch("monitor.PageReader") as reader, \
                 patch("monitor.run_cycle", return_value=successful) as cycle:
                self.assertEqual(m.main(), 0 if successful else 1)
                cycle.assert_called_once()
                reader.return_value.close.assert_called_once()

    def test_healthcheck_reports_stale_or_failed_notification(self):
        with patch("monitor.time.time", return_value=1000):
            self.assertEqual(m.healthcheck(self.config), 1)
            self.cycle("unavailable", now=1000)
            self.assertEqual(m.healthcheck(self.config), 0)
            self.state["notification_error"] = True
            m.atomic_json(self.config.data_dir / "state.json", self.state)
            self.assertEqual(m.healthcheck(self.config), 1)
        with patch("monitor.time.time", return_value=2000):
            self.assertEqual(m.healthcheck(self.config), 1)

    def test_corrupted_state_fails_closed(self):
        (self.config.data_dir / "state.json").write_text("{broken")
        with self.assertRaises(ValueError):
            m.load_state(self.config)

    def test_wrong_url_cannot_reuse_state(self):
        self.cycle()
        with self.assertRaises(ValueError):
            m.load_state(replace(self.config, url="https://example.com"))

    @patch.dict("os.environ", {"CALLMEBOT_PHONE": "+18095550123", "CALLMEBOT_API_KEY": "secret"})
    def test_callmebot_checks_body_even_with_http_200(self):
        with patch("monitor.http_request", return_value="<p>Message queued.</p>") as request:
            m.send_notification(self.config, "¿Boletas? & horarios")
            self.assertIn("%26", request.call_args.args[0].full_url)
        for body in ["ERROR: invalid API key", "<html>maintenance</html>"]:
            with patch("monitor.http_request", return_value=body), self.assertRaises(RuntimeError):
                m.send_notification(self.config, "test")

    @patch.dict("os.environ", {"CALLMEBOT_PHONE": "12345678901234@lid", "CALLMEBOT_API_KEY": "test-key"})
    def test_callmebot_rejects_lid_with_actionable_error(self):
        with self.assertRaisesRegex(ValueError, "activación válida"):
            m.validate_notifier(self.config)

    @patch.dict("os.environ", {"CALLMEBOT_PHONE": "12345678901234@other", "CALLMEBOT_API_KEY": "test-key"})
    def test_callmebot_rejects_unknown_destination_format(self):
        with self.assertRaises(ValueError):
            m.validate_notifier(self.config)

    @patch.dict("os.environ", {
        "META_ACCESS_TOKEN": "secret", "META_PHONE_NUMBER_ID": "123", "META_TO": "18095550123",
        "META_GRAPH_VERSION": "v99.0", "META_TEMPLATE_NAME": "cinema_alert", "META_TEMPLATE_LANGUAGE": "es",
    })
    def test_meta_sends_approved_template_payload(self):
        with patch("monitor.http_request", return_value='{"messages":[{"id":"test"}]}') as request:
            m.send_notification(replace(self.config, notifier="meta"), "Alert\nMovie")
            payload = json.loads(request.call_args.args[0].data)
            self.assertEqual(payload["type"], "template")
            self.assertEqual(payload["template"]["components"][0]["parameters"][0]["text"], "Alert Movie")

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "123:fake_token", "TELEGRAM_CHAT_ID": "456"}, clear=True)
    def test_telegram_sends_to_one_private_chat_without_parsing_text(self):
        config = replace(self.config, notifier="telegram")
        m.validate_notifier(config)
        response = '{"ok":true,"result":{"message_id":7,"chat":{"id":456}}}'
        with patch("monitor.http_request", return_value=response) as request:
            m.send_notification(config, "¡Boletas! <sin formato> & horarios\nPelícula")
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_method(), "POST")
        self.assertTrue(sent.full_url.endswith("/sendMessage"))
        payload = json.loads(sent.data)
        self.assertEqual(payload["chat_id"], 456)
        self.assertEqual(payload["text"], "¡Boletas! <sin formato> & horarios\nPelícula")
        self.assertNotIn("parse_mode", payload)
        self.assertNotIn("fake_token", sent.data.decode())

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "123:fake_token", "TELEGRAM_CHAT_ID": "456"}, clear=True)
    def test_telegram_rejects_errors_malformed_and_wrong_recipient_responses(self):
        config = replace(self.config, notifier="telegram")
        bodies = ['{"ok":false,"description":"private fake_token"}', '<html>fake_token</html>',
                  '[]', '{"ok":true,"result":null}',
                  '{"ok":true,"result":{"message_id":7,"chat":{"id":999}}}',
                  '{"ok":true,"result":{"chat":{"id":456}}}']
        for body in bodies:
            with self.subTest(body=body), patch("monitor.http_request", return_value=body):
                with self.assertRaises(m.NotificationError) as caught:
                    m.send_notification(config, "test")
                self.assertNotIn("fake_token", str(caught.exception))

    def test_telegram_invalid_configuration_fails_before_network(self):
        cases = [{"TELEGRAM_BOT_TOKEN": "bad", "TELEGRAM_CHAT_ID": "456"},
                 {"TELEGRAM_BOT_TOKEN": "123:fake_token", "TELEGRAM_CHAT_ID": "-123"},
                 {"TELEGRAM_BOT_TOKEN": "123:fake_token", "TELEGRAM_CHAT_ID": "@someone"},
                 {"TELEGRAM_BOT_TOKEN": "123:fake_token", "TELEGRAM_CHAT_ID": ""}]
        for env in cases:
            with self.subTest(env=env), patch.dict("os.environ", env, clear=True):
                with self.assertRaises(ValueError):
                    m.validate_notifier(replace(self.config, notifier="telegram"))

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "123:fake_token", "TELEGRAM_CHAT_ID": "456"}, clear=True)
    def test_rejected_telegram_alert_remains_pending_for_retry(self):
        config = replace(self.config, notifier="telegram", confirmations=1)
        self.reader.read.return_value = m.Observation("available", "6:40 PM")
        with patch("monitor.http_request", return_value='{"ok":false}'):
            self.assertFalse(m.run_cycle(config, self.state, self.reader, now=1000))
        self.assertEqual(self.state["sent"], [])
        self.assertTrue(self.state["notification_error"])
        self.assertEqual(self.state["retry_after"], 1300)

    def test_http_errors_do_not_expose_keys(self):
        error = HTTPError("https://example.com/?apikey=secret", 403, "secret", {}, None)
        with patch("monitor.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                m.http_request("https://example.com")
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

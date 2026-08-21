import io
import json
import unittest
from unittest import mock

from agent_box.telegram.api import TelegramAPIError, TelegramClient


def _response(payload):
    body = json.dumps(payload).encode()
    fh = io.BytesIO(body)
    fh.getcode = lambda: 200
    return fh


class TelegramClientTests(unittest.TestCase):
    def test_requires_token(self):
        with self.assertRaises(ValueError):
            TelegramClient("")

    def test_send_message_success(self):
        client = TelegramClient("t123")
        with mock.patch("urllib.request.urlopen", return_value=_response({"ok": True, "result": {"message_id": 5}})) as m:
            result = client.send_message(123, "hi")
        self.assertEqual(result, {"message_id": 5})
        request = m.call_args[0][0]
        self.assertIn("bott123", request.full_url)

    def test_api_error_raised_on_not_ok(self):
        client = TelegramClient("t123")
        with mock.patch("urllib.request.urlopen", return_value=_response({"ok": False, "description": "bad request"})):
            with self.assertRaises(TelegramAPIError) as ctx:
                client.send_message(123, "hi")
        self.assertIn("bad request", str(ctx.exception))
        self.assertNotIn("t123", str(ctx.exception))

    def test_get_updates_parses_offset(self):
        client = TelegramClient("t123")
        with mock.patch("urllib.request.urlopen", return_value=_response({"ok": True, "result": []})) as m:
            client.get_updates(offset=42, timeout=5)
        body = json.loads(m.call_args[0][0].data)
        self.assertEqual(body["offset"], 42)


if __name__ == "__main__":
    unittest.main()

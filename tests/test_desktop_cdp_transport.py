from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_cdp_transport import (  # noqa: E402
    DesktopCdpClient,
    build_follow_up_expression,
    build_probe_expression,
    select_primary_codex_target,
    validate_loopback_http_url,
)


class DesktopCdpTransportTests(unittest.TestCase):
    def test_loopback_http_validation(self):
        self.assertEqual(
            validate_loopback_http_url("http://127.0.0.1:9335"),
            ("127.0.0.1", 9335),
        )
        with self.assertRaises(ValueError):
            validate_loopback_http_url("http://192.168.1.5:9335")
        with self.assertRaises(ValueError):
            validate_loopback_http_url("https://127.0.0.1:9335")

    def test_selects_primary_page_not_overlay_or_browser(self):
        target = select_primary_codex_target(
            [
                {
                    "type": "page",
                    "url": "https://example.com",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9335/devtools/page/a",
                },
                {
                    "type": "page",
                    "url": "app://-/index.html?initialRoute=%2Favatar-overlay",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9335/devtools/page/b",
                },
                {
                    "type": "page",
                    "url": "app://-/index.html",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9335/devtools/page/c",
                },
            ]
        )
        self.assertTrue(target["webSocketDebuggerUrl"].endswith("/c"))

    def test_expression_uses_json_payload_without_string_injection(self):
        expression = build_follow_up_expression(
            "thread-1",
            "引号'\"和换行\n测试",
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )
        self.assertIn("findDesktopRequestClient", expression)
        self.assertIn("thread/resume", expression)
        self.assertIn("turn/start", expression)
        self.assertIn(json.dumps("thread-1"), expression)
        self.assertIn("requestState", expression)
        self.assertIn('"model":"gpt-5.6-sol"', expression)
        self.assertIn('"effort":"high"', expression)
        self.assertNotIn("引号'\"和换行\n测试", expression)

    def test_expression_omits_unavailable_model_preferences(self):
        expression = build_follow_up_expression("thread-1", "继续")
        self.assertNotIn('\\"model\\":null', expression)
        self.assertNotIn('\\"effort\\":null', expression)

    def test_probe_expression_does_not_submit(self):
        expression = build_probe_expression()
        self.assertIn("requestExport", expression)
        self.assertIn("findDesktopRequestClient", expression)
        self.assertNotIn("turn/start", expression)

    def test_send_follow_up_returns_runtime_value(self):
        connection = mock.MagicMock()
        connection.iter_text.return_value = iter(
            [
                json.dumps(
                    {
                        "id": 1,
                        "result": {
                            "result": {
                                "type": "object",
                                "value": {"ok": True, "requestExport": "qTt"},
                            }
                        },
                    }
                )
            ]
        )
        client = DesktopCdpClient("http://127.0.0.1:9335")
        with (
            mock.patch.object(
                client,
                "list_targets",
                return_value=[
                    {
                        "type": "page",
                        "url": "app://-/index.html",
                        "webSocketDebuggerUrl": (
                            "ws://127.0.0.1:9335/devtools/page/test"
                        ),
                    }
                ],
            ),
            mock.patch(
                "desktop_cdp_transport.WebSocketConnection",
                return_value=connection,
            ),
        ):
            result = client.send_follow_up(
                "thread-1", "继续", model="gpt-5.6-sol", reasoning_effort="high"
            )
        self.assertEqual(result["requestExport"], "qTt")
        sent = connection.send_json.call_args.args[0]
        self.assertEqual(sent["method"], "Runtime.evaluate")
        self.assertIn('"effort":"high"', sent["params"]["expression"])
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
